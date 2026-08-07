import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.auth_session import AuthSession
from app.infrastructure.external.session_auth.redis_session_store import (
    RedisSessionStore,
)


def _session(session_id: str, generation: int = 0) -> AuthSession:
    now = datetime.now(UTC)
    return AuthSession(
        session_id=session_id,
        user_id="session-user",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        last_seen_at=now,
        revocation_generation=generation,
    )


def _store(client) -> RedisSessionStore:
    store = RedisSessionStore.__new__(RedisSessionStore)
    store._redis = SimpleNamespace(initialize=AsyncMock(), client=client)
    return store


@pytest.mark.asyncio
async def test_revoke_all_is_constant_time_and_leaves_session_keys_to_ttl():
    client = SimpleNamespace(eval=AsyncMock(return_value=17))
    store = _store(client)

    assert await store.delete_all_for_user("large-user") == 17

    script, num_keys, index_key, generation_key = client.eval.await_args.args
    normalized = script.upper()
    assert num_keys == 2
    assert index_key == "user_sessions:large-user"
    assert generation_key == "user_session_generation:large-user"
    assert "SCARD" in normalized
    assert "INCR" in normalized
    assert "UNLINK" in normalized
    assert "SMEMBERS" not in normalized
    assert "SESSION:" not in normalized


@pytest.mark.asyncio
async def test_touch_atomically_persists_sliding_timestamps():
    original = _session("touch-me")

    async def eval_script(script, num_keys, *items):
        assert "auth-session-touch-v2" in script
        args = items[num_keys:]
        payload = json.loads(original.model_dump_json())
        payload["last_seen_at"] = args[3]
        payload["expires_at"] = args[4]
        return json.dumps(payload)

    client = SimpleNamespace(eval=AsyncMock(side_effect=eval_script))
    store = _store(client)

    touched = await store.touch(original.session_id, 600)

    assert touched is not None
    assert touched.last_seen_at > original.last_seen_at
    assert touched.expires_at > original.expires_at
    script = client.eval.await_args.args[0]
    assert "payload['last_seen_at'] = ARGV[4]" in script
    assert "payload['expires_at'] = ARGV[5]" in script
    assert "cjson.encode(payload)" in script


@pytest.mark.asyncio
async def test_session_writes_bound_legacy_persistent_index_ttl():
    client = SimpleNamespace(eval=AsyncMock(return_value=1))
    store = _store(client)
    current = _session("current")
    replacement = _session("replacement")

    assert await store.create(current, 300, expected_generation=0)
    create_script = client.eval.await_args.args[0]
    assert "index_ttl == -1" in create_script

    client.eval.reset_mock()
    client.eval.return_value = current.model_dump_json()
    assert await store.touch(current.session_id, 300) is not None
    touch_script = client.eval.await_args.args[0]
    assert "index_ttl == -1" in touch_script

    client.eval.reset_mock()
    client.eval.return_value = 1
    assert await store.rotate(
        current.session_id,
        replacement,
        300,
        expected_generation=0,
    )
    rotate_script = client.eval.await_args.args[0]
    assert "index_ttl == -2 or index_ttl == -1" in rotate_script
