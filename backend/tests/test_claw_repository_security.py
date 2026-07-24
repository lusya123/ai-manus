import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.domain.models.claw import Claw, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
from app.domain.repositories.claw_repository import ClawWriteConflictError
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
    class AtomicHistoryCollection:
        def __init__(self):
            self.messages = []
            self.last_activity_at = None
            self.revision = 0
            self.lock = asyncio.Lock()

        async def update_one(self, query, pipeline):
            assert query == {
                "user_id": "user-1",
                "status": ClawStatus.RUNNING.value,
            }
            # Yield before the atomic section to force competing appends to
            # overlap. A read/append/save implementation would lose writes.
            await asyncio.sleep(0)
            async with self.lock:
                update = pipeline[0]["$set"]
                message_expr = update["messages"]["$let"]
                candidate_expr = message_expr["vars"]["candidates"]["$slice"]
                max_messages = -candidate_expr[1]
                incoming = candidate_expr[0]["$concatArrays"][1]["$literal"]
                candidates = (self.messages + incoming)[-max_messages:]
                total_bytes = 0
                retained = []
                for message in reversed(candidates):
                    message_bytes = (
                        len(repository_module.BSON.encode(message))
                        + repository_module._HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
                    )
                    if total_bytes + message_bytes > 8 * 1024 * 1024:
                        break
                    retained.append(message)
                    total_bytes += message_bytes
                self.messages = list(reversed(retained))
                self.last_activity_at = update["last_activity_at"]
                self.revision += 1
            return SimpleNamespace(matched_count=1)

    history = AtomicHistoryCollection()

    class FakeClawDocument:
        @classmethod
        def get_pymongo_collection(cls):
            return history

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)
    monkeypatch.setattr(
        repository_module,
        "get_settings",
        lambda: SimpleNamespace(
            claw_history_max_messages=128,
            claw_history_max_bytes=8 * 1024 * 1024,
        ),
    )
    repository = ClawRepository()

    await asyncio.gather(*(
        repository.append_message("user-1", "assistant", f"message-{index}")
        for index in range(200)
    ))

    assert len(history.messages) == 128
    assert len({message["content"] for message in history.messages}) == 128
    assert history.last_activity_at is not None
    assert history.revision == 200


@pytest.mark.asyncio
async def test_atomic_append_trims_near_mongo_limit_and_keeps_recent_tail(
    monkeypatch,
):
    max_bytes = 12 * 1024 * 1024

    class NearLimitCollection:
        def __init__(self):
            # About 15 MiB of valid, individually bounded assistant records:
            # below MongoDB's document ceiling before the incoming append, but
            # above the configured safe history budget.
            self.messages = [
                {
                    "role": "assistant",
                    "content": f"old-{index:03d}|" + ("x" * (128 * 1024)),
                    "timestamp": index,
                    "attachments": None,
                }
                for index in range(120)
            ]
            self.pipeline = None

        async def update_one(self, query, pipeline):
            self.pipeline = pipeline
            message_expr = pipeline[0]["$set"]["messages"]["$let"]
            candidate_expr = message_expr["vars"]["candidates"]["$slice"]
            incoming = candidate_expr[0]["$concatArrays"][1]["$literal"]
            candidates = (self.messages + incoming)[-(-candidate_expr[1]):]

            retained = []
            total_bytes = 0
            for message in reversed(candidates):
                message_bytes = (
                    len(repository_module.BSON.encode(message))
                    + repository_module._HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
                )
                if total_bytes + message_bytes > max_bytes:
                    break
                retained.append(message)
                total_bytes += message_bytes
            self.messages = list(reversed(retained))
            return SimpleNamespace(matched_count=1)

    collection = NearLimitCollection()

    class FakeClawDocument:
        @classmethod
        def get_pymongo_collection(cls):
            return collection

    monkeypatch.setattr(repository_module, "ClawDocument", FakeClawDocument)
    monkeypatch.setattr(
        repository_module,
        "get_settings",
        lambda: SimpleNamespace(
            claw_history_max_messages=128,
            claw_history_max_bytes=max_bytes,
        ),
    )

    await ClawRepository().append_message(
        "user-1", "assistant", "$latest-message-must-stay"
    )

    retained_bytes = sum(
        len(repository_module.BSON.encode(message))
        + repository_module._HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
        for message in collection.messages
    )
    assert retained_bytes <= max_bytes
    assert len(collection.messages) < 121
    assert collection.messages[-1]["content"] == "$latest-message-must-stay"
    assert not collection.messages[0]["content"].startswith("old-000|")

    # The production update is a single atomic aggregation pipeline and user
    # content is wrapped in `$literal`, so leading `$` cannot become a field
    # reference. The byte budget is enforced server-side with `$bsonSize`.
    message_expr = collection.pipeline[0]["$set"]["messages"]["$let"]
    retained_expr = message_expr["in"]["$let"]["vars"]["retained"]
    assert "$reduce" in retained_expr
    assert (
        retained_expr["$reduce"]["in"]["$let"]["vars"]["message_bytes"]
        ["$add"][0]
        == {"$bsonSize": "$$this"}
    )


@pytest.mark.asyncio
async def test_lifecycle_cas_never_promotes_a_stale_writer_token(monkeypatch):
    """A refetch after CAS must not hand B's newer revision back to stale A."""

    initial = _claw(api_key=None, revision=0).model_dump()
    initial["claw_id"] = initial.pop("id")
    initial.update({
        "_id": None,
        "api_key_digest": None,
        "messages": [],
    })

    class AtomicCollection:
        def __init__(self, state):
            self.state = dict(state)

        @staticmethod
        def _expected_revision(query):
            clause = query["$and"][1]
            return clause.get("revision", 0)

        async def find_one_and_update(
            self, query, operation, *, return_document
        ):
            if self.state["revision"] != self._expected_revision(query):
                return None
            self.state.update(operation.get("$set", {}))
            for field in operation.get("$unset", {}):
                self.state.pop(field, None)
            for field, amount in operation.get("$inc", {}).items():
                self.state[field] = self.state.get(field, 0) + amount
            return dict(self.state)

    collection = AtomicCollection(initial)
    monkeypatch.setattr(
        ClawDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    async def find_one(cls, query):
        return ClawDocument.model_validate(dict(collection.state))

    monkeypatch.setattr(ClawDocument, "find_one", classmethod(find_one))
    repository = ClawRepository()

    writer_a = _claw(api_key=None, revision=0, status=ClawStatus.STOPPED)
    await repository.update(writer_a)
    assert writer_a.revision == 1

    writer_b = writer_a.model_copy(deep=True)
    writer_b.status = ClawStatus.RUNNING
    await repository.update(writer_b)
    assert writer_b.revision == 2
    assert writer_a.revision == 1

    writer_a.error_message = "stale overwrite"
    with pytest.raises(ClawWriteConflictError):
        await repository.update(writer_a)
    assert collection.state["status"] == ClawStatus.RUNNING
    assert collection.state["error_message"] is None


@pytest.mark.asyncio
async def test_destroy_claim_and_delete_are_exact_generation_cas(monkeypatch):
    initial = _claw(
        api_key=None,
        revision=3,
        status=ClawStatus.RUNNING,
        container_name="claw-generation-a",
    ).model_dump()
    initial["claw_id"] = initial.pop("id")
    initial.update({
        "_id": None,
        "api_key_digest": None,
        "messages": [],
    })

    class LifecycleCollection:
        def __init__(self, state):
            self.state = dict(state)

        def _matches(self, query):
            if self.state is None:
                return False
            if "$and" in query:
                return all(self._matches(item) for item in query["$and"])
            if "$or" in query:
                return any(self._matches(item) for item in query["$or"])
            for field, expected in query.items():
                if isinstance(expected, dict) and "$exists" in expected:
                    if (field in self.state) != bool(expected["$exists"]):
                        return False
                elif self.state.get(field) != expected:
                    return False
            return True

        async def find_one_and_update(
            self, query, operation, *, return_document
        ):
            if not self._matches(query):
                return None
            self.state.update(operation.get("$set", {}))
            for field, amount in operation.get("$inc", {}).items():
                self.state[field] = self.state.get(field, 0) + amount
            return dict(self.state)

        async def delete_one(self, query):
            if not self._matches(query):
                return SimpleNamespace(deleted_count=0)
            self.state = None
            return SimpleNamespace(deleted_count=1)

    collection = LifecycleCollection(initial)
    monkeypatch.setattr(
        ClawDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = ClawRepository()

    stale = _claw(
        api_key=None,
        revision=2,
        container_name="claw-generation-a",
    )
    assert await repository.claim_runtime_destroy(stale) is None
    assert collection.state["status"] == ClawStatus.RUNNING

    current = _claw(
        api_key=None,
        revision=3,
        container_name="claw-generation-a",
    )
    claimed = await repository.claim_runtime_destroy(current)
    assert claimed is not None
    assert claimed.status == ClawStatus.DESTROYING
    assert claimed.revision == 4

    # The pre-claim snapshot cannot delete the claimed record.
    assert await repository.delete_if_matches(current) is False
    assert collection.state is not None
    assert await repository.delete_if_matches(claimed) is True
    assert collection.state is None
