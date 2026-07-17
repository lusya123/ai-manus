import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.domain.models.claw import Claw, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
from app.domain.utils.claw_credentials import claw_api_key_digest
from app.infrastructure.models.documents import ClawDocument
from app.infrastructure.repositories.claw_repository import ClawRepository
import app.infrastructure.repositories.claw_repository as repository_module


@pytest.fixture(autouse=True)
def secure_settings(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "claw-repository-tests-only-secret-at-least-32-bytes",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _claw(**overrides):
    values = {
        "id": "claw-1",
        "user_id": "user-1",
        "api_key": "runtime-plaintext-secret",
        "status": ClawStatus.CREATING,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Claw(**values)


def test_claw_runtime_key_is_excluded_from_domain_serialization_and_storage(
    monkeypatch,
):
    claw = _claw()

    assert "runtime-plaintext-secret" not in repr(claw)
    assert "api_key" not in claw.model_dump()
    assert "runtime-plaintext-secret" not in claw.model_dump_json()

    # Beanie normally initializes this collection during application startup;
    # model conversion itself does not require a live database.
    monkeypatch.setattr(
        ClawDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: None),
    )
    doc = ClawDocument.from_domain(claw)
    expected_digest = claw_api_key_digest(
        "runtime-plaintext-secret",
        get_settings().jwt_secret_key,
    )

    assert doc.api_key is None
    assert doc.api_key_digest == expected_digest
    assert "api_key" not in doc.model_dump()
    assert doc.to_domain().api_key is None


@pytest.mark.asyncio
async def test_legacy_plaintext_lookup_lazily_migrates_to_digest(monkeypatch):
    operations = []

    class LegacyDocument:
        api_key = "legacy-runtime-secret"
        api_key_digest = None

        async def update(self, operation):
            operations.append(operation)

        def to_domain(self):
            return _claw(api_key=None, status=ClawStatus.RUNNING)

    legacy_doc = LegacyDocument()

    class FakeClawDocument:
        queries = []

        @classmethod
        async def find_one(cls, query):
            cls.queries.append(query)
            if "api_key_digest" in query:
                return None
            if query == {"api_key": "legacy-runtime-secret"}:
                return legacy_doc
            return None

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)
    result = await ClawRepository().get_by_api_key("legacy-runtime-secret")

    expected_digest = claw_api_key_digest(
        "legacy-runtime-secret",
        get_settings().jwt_secret_key,
    )
    assert FakeClawDocument.queries == [
        {"api_key_digest": expected_digest},
        {"api_key": "legacy-runtime-secret"},
    ]
    assert operations == [{
        "$set": {"api_key_digest": expected_digest},
        "$unset": {"api_key": ""},
    }]
    assert legacy_doc.api_key is None
    assert legacy_doc.api_key_digest == expected_digest
    assert result is not None and result.api_key is None


@pytest.mark.asyncio
async def test_runtime_key_lookup_uses_only_server_keyed_digest(monkeypatch):
    runtime_key = "fixed-runtime-secret"
    expected_digest = claw_api_key_digest(
        runtime_key,
        get_settings().jwt_secret_key,
    )

    class DigestDocument:
        api_key = None
        api_key_digest = expected_digest

        def to_domain(self):
            return _claw(api_key=None, status=ClawStatus.RUNNING)

    class FakeClawDocument:
        queries = []

        @classmethod
        async def find_one(cls, query):
            cls.queries.append(query)
            if query == {"api_key_digest": expected_digest}:
                return DigestDocument()
            return None

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)

    result = await ClawRepository().get_by_api_key(runtime_key)

    assert result is not None
    assert result.api_key is None
    assert FakeClawDocument.queries == [{"api_key_digest": expected_digest}]
    assert runtime_key not in str(FakeClawDocument.queries)


@pytest.mark.asyncio
async def test_previous_hmac_key_match_is_lazily_rehashed_to_current(monkeypatch):
    runtime_key = "runtime-secret-during-server-key-rotation"
    current_secret = "current-hmac-secret-at-least-32-bytes"
    previous_secret = "previous-hmac-secret-at-least-32-bytes"
    current_digest = claw_api_key_digest(runtime_key, current_secret)
    previous_digest = claw_api_key_digest(runtime_key, previous_secret)
    operations = []

    class PreviousKeyDocument:
        api_key = None
        api_key_digest = previous_digest

        async def update(self, operation):
            operations.append(operation)

        def to_domain(self):
            return _claw(api_key=None, status=ClawStatus.RUNNING)

    previous_doc = PreviousKeyDocument()

    class FakeClawDocument:
        queries = []

        @classmethod
        async def find_one(cls, query):
            cls.queries.append(query)
            if query == {"api_key_digest": previous_digest}:
                return previous_doc
            return None

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)
    monkeypatch.setattr(
        repository_module,
        "get_settings",
        lambda: SimpleNamespace(
            jwt_secret_key="unused-fallback-secret-at-least-32-bytes",
            claw_api_key_hmac_keys=f"{current_secret},{previous_secret}",
        ),
    )

    result = await ClawRepository().get_by_api_key(runtime_key)

    assert result is not None
    assert FakeClawDocument.queries == [
        {"api_key_digest": current_digest},
        {"api_key_digest": previous_digest},
    ]
    assert operations == [{
        "$set": {"api_key_digest": current_digest},
        "$unset": {"api_key": ""},
    }]
    assert previous_doc.api_key_digest == current_digest


@pytest.mark.asyncio
async def test_dynamic_runtime_rebuild_rotates_ephemeral_key(monkeypatch):
    existing = _claw(
        api_key=None,
        status=ClawStatus.STOPPED,
        container_name=None,
        container_ip=None,
    )

    class Repository:
        async def get_by_user_id(self, user_id):
            return existing

        async def count_by_statuses(self, statuses):
            return 0

        async def update(self, claw):
            return claw

    service = ClawDomainService(
        Repository(),
        claw_runtime=SimpleNamespace(),
        claw_client=SimpleNamespace(),
    )
    monkeypatch.setattr(service.settings, "claw_address", None)

    rebuilt = await service.prepare_claw_for_creation("user-1")

    assert rebuilt.id == existing.id
    assert rebuilt.api_key
    assert rebuilt.api_key.startswith("manus-")


@pytest.mark.asyncio
async def test_atomic_append_retains_bounded_concurrent_history(monkeypatch):
    class AtomicHistory:
        def __init__(self):
            self.messages = []
            self.last_activity_at = None
            self.lock = asyncio.Lock()

        async def update(self, operation):
            # Yield before the atomic section to force competing appends to
            # overlap.  A read/append/save implementation would lose writes.
            await asyncio.sleep(0)
            async with self.lock:
                push = operation["$push"]["messages"]
                self.messages.extend(push["$each"])
                self.messages = self.messages[push["$slice"]:]
                self.last_activity_at = operation["$set"]["last_activity_at"]

    history = AtomicHistory()

    class FakeClawDocument:
        @classmethod
        def find_one(cls, query):
            assert query == {"user_id": "user-1"}
            return history

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)
    monkeypatch.setattr(
        repository_module,
        "get_settings",
        lambda: SimpleNamespace(claw_history_max_messages=512),
    )
    repository = ClawRepository()

    await asyncio.gather(*(
        repository.append_message("user-1", "assistant", f"message-{index}")
        for index in range(200)
    ))

    assert len(history.messages) == 128
    assert len({message["content"] for message in history.messages}) == 128
    assert history.last_activity_at is not None
