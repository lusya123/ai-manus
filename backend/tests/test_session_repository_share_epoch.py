from types import SimpleNamespace

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.session import Session
from app.infrastructure.models.documents import (
    SessionDocument,
    TurnOutputEventDocument,
)
from app.infrastructure.repositories.mongo_session_repository import (
    MongoSessionRepository,
)
from app.interfaces.api.session_routes import _share_authorized_file_index


class _LegacySessionDocument:
    def __init__(self, document_id="mongo-id", share_epoch=None):
        self.id = document_id
        self.share_epoch = share_epoch

    def to_domain(self):
        return SimpleNamespace(share_epoch=self.share_epoch)


async def test_legacy_share_epoch_is_persisted_once_and_reused(monkeypatch):
    class Collection:
        def __init__(self):
            self.persisted = None
            self.updates = 0

        async def update_one(self, query, update):
            self.updates += 1
            self.persisted = update["$set"]["share_epoch"]
            return SimpleNamespace(modified_count=1)

        async def find_one(self, query, projection):
            return {"share_epoch": self.persisted}

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = MongoSessionRepository()
    first = _LegacySessionDocument()

    first_domain = await repository._to_domain_with_share_epoch(first)
    second = _LegacySessionDocument(share_epoch=collection.persisted)
    second_domain = await repository._to_domain_with_share_epoch(second)

    assert first_domain.share_epoch == second_domain.share_epoch
    assert len(first_domain.share_epoch) == 32
    assert collection.updates == 1


async def test_concurrent_legacy_backfill_uses_winning_persisted_epoch(
    monkeypatch,
):
    class Collection:
        async def update_one(self, query, update):
            return SimpleNamespace(modified_count=0)

        async def find_one(self, query, projection):
            return {"share_epoch": "winner-epoch"}

    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: Collection()),
    )

    domain = await MongoSessionRepository()._to_domain_with_share_epoch(
        _LegacySessionDocument()
    )

    assert domain.share_epoch == "winner-epoch"


async def test_runtime_ownership_cas_fences_generation_and_task(monkeypatch):
    class Collection:
        def __init__(self):
            self.query = None
            self.update = None

        async def update_one(self, query, update):
            self.query = query
            self.update = update
            return SimpleNamespace(matched_count=1)

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    updated = await MongoSessionRepository().compare_and_set_runtime_ownership(
        "session-1",
        "sandbox-generation-a",
        "task-a",
        "sandbox-generation-a",
        "docker",
        "sandbox-generation-b",
        "task-b",
        "docker",
        "sandbox-generation-b",
    )

    assert updated is True
    assert collection.query == {
        "$and": [
            {"session_id": "session-1"},
            {"sandbox_id": "sandbox-generation-a"},
            {"task_id": "task-a"},
            {"task_sandbox_id": "sandbox-generation-a"},
            {"sandbox_provider": "docker"},
            {"sandbox_destroying": {"$ne": True}},
            {"deleting": {"$ne": True}},
        ]
    }
    assert collection.update["$set"]["sandbox_id"] == "sandbox-generation-b"
    assert collection.update["$set"]["task_id"] == "task-b"
    assert (
        collection.update["$set"]["task_sandbox_id"]
        == "sandbox-generation-b"
    )
    assert collection.update["$set"]["sandbox_destroying"] is False


async def test_runtime_destroy_claim_and_finish_are_exact_projection_cas(
    monkeypatch,
):
    class Collection:
        def __init__(self):
            self.calls = []

        async def update_one(self, query, update):
            self.calls.append((query, update))
            return SimpleNamespace(matched_count=1)

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = MongoSessionRepository()

    claimed = await repository.claim_runtime_destroy(
        "session-1", "generation-a", "task-a", "generation-a", "docker"
    )
    finished = await repository.finish_runtime_destroy(
        "session-1",
        "generation-a",
        "task-a",
        "generation-a",
        "docker",
        None,
        None,
        None,
        None,
    )

    assert claimed is True
    assert finished is True
    claim_query, claim_update = collection.calls[0]
    assert claim_query == {
        "$and": [
            {"session_id": "session-1"},
            {"sandbox_id": "generation-a"},
            {"task_id": "task-a"},
            {"task_sandbox_id": "generation-a"},
            {"sandbox_provider": "docker"},
            {"sandbox_destroying": {"$ne": True}},
        ]
    }
    assert claim_update["$set"]["sandbox_destroying"] is True
    finish_query, finish_update = collection.calls[1]
    assert finish_query == {
        "$and": [
            {"session_id": "session-1"},
            {"sandbox_id": "generation-a"},
            {"task_id": "task-a"},
            {"task_sandbox_id": "generation-a"},
            {"sandbox_provider": "docker"},
            {"sandbox_destroying": True},
        ]
    }
    assert finish_update["$set"]["sandbox_id"] is None
    assert finish_update["$set"]["task_id"] is None
    assert finish_update["$set"]["task_sandbox_id"] is None
    assert finish_update["$set"]["sandbox_provider"] is None
    assert finish_update["$set"]["sandbox_destroying"] is False


async def test_unpublished_runtime_is_published_directly_as_destroy_claim(
    monkeypatch,
):
    class Collection:
        def __init__(self):
            self.query = None
            self.update = None

        async def update_one(self, query, update):
            self.query = query
            self.update = update
            return SimpleNamespace(matched_count=1)

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    published = await MongoSessionRepository().publish_runtime_destroy_claim(
        "session-1",
        "task-a",
        "generation-previous",
        None,
        "generation-late-create",
        "docker",
    )

    assert published is True
    assert collection.query == {
        "$and": [
            {"session_id": "session-1"},
            {"$or": [
                {"sandbox_id": None},
                {"sandbox_id": {"$exists": False}},
            ]},
            {"task_id": "task-a"},
            {"task_sandbox_id": "generation-previous"},
            {"$or": [
                {"sandbox_provider": None},
                {"sandbox_provider": {"$exists": False}},
            ]},
            {"sandbox_destroying": {"$ne": True}},
            {"deleting": {"$ne": True}},
        ]
    }
    assert collection.update["$set"]["sandbox_id"] == "generation-late-create"
    assert collection.update["$set"]["sandbox_provider"] == "docker"
    assert collection.update["$set"]["sandbox_destroying"] is True


async def test_session_delete_is_irreversibly_claimed_and_conditional(
    monkeypatch,
):
    class Collection:
        def __init__(self):
            self.update_query = None
            self.delete_query = None

        async def update_one(self, query, update):
            self.update_query = query
            assert update["$set"]["deleting"] is True
            return SimpleNamespace(matched_count=1)

        async def delete_one(self, query):
            self.delete_query = query
            return SimpleNamespace(deleted_count=1)

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = MongoSessionRepository()

    assert await repository.claim_session_delete("session-1", "user-1")
    assert await repository.delete_claimed("session-1", "user-1")
    assert collection.update_query == {
        "session_id": "session-1",
        "user_id": "user-1",
        "deleting": {"$ne": True},
    }
    assert collection.delete_query == {
        "session_id": "session-1",
        "user_id": "user-1",
        "deleting": True,
    }


async def test_file_path_upsert_is_one_atomic_mongo_update(monkeypatch):
    class Collection:
        def __init__(self):
            self.query = None
            self.pipeline = None

        async def find_one_and_update(
            self,
            query,
            pipeline,
            projection=None,
            return_document=None,
        ):
            self.query = query
            self.pipeline = pipeline
            return {
                "files": [
                    {
                        "file_id": "old-file",
                        "filename": "report.md",
                        "file_path": "/home/ubuntu/upload/report.md",
                    }
                ]
            }

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    file_info = FileInfo(
        file_id="new-file",
        filename="report.md",
        file_path="/home/ubuntu/upload/report.md",
    )

    previous = await MongoSessionRepository().upsert_file_by_path(
        "session-1",
        file_info,
    )

    assert collection.query == {
        "session_id": "session-1",
        "files": {
            "$not": {
                "$elemMatch": {"file_id": "new-file"}
            }
        },
    }
    files_expression = collection.pipeline[0]["$set"]["files"]
    map_expression = files_expression["$concatArrays"][0]["$map"]
    retire_expression = map_expression["in"]["$cond"]
    assert retire_expression[0]["$eq"] == [
        "$$file.file_path",
        {"$literal": "/home/ubuntu/upload/report.md"},
    ]
    assert retire_expression[1]["$mergeObjects"] == [
        "$$file",
        {"file_path": None},
    ]
    assert retire_expression[2] == "$$file"
    assert files_expression["$concatArrays"][1][0]["$literal"]["file_id"] == (
        "new-file"
    )
    assert previous.file_id == "old-file"


async def test_path_upsert_preserves_shared_history_and_selects_latest_version(
    monkeypatch,
):
    artifact_path = "/home/ubuntu/upload/report.md"

    class Collection:
        def __init__(self):
            self.files = [
                {
                    "file_id": "artifact-v1",
                    "filename": "report.md",
                    "file_path": artifact_path,
                }
            ]

        async def find_one_and_update(
            self,
            query,
            pipeline,
            projection=None,
            return_document=None,
        ):
            previous = {"files": [dict(item) for item in self.files]}
            files_expression = pipeline[0]["$set"]["files"]
            successor = files_expression["$concatArrays"][1][0]["$literal"]
            self.files = [
                {
                    **item,
                    "file_path": (
                        None
                        if item.get("file_path") == successor["file_path"]
                        else item.get("file_path")
                    ),
                }
                for item in self.files
            ] + [successor]
            return previous

    collection = Collection()

    async def find_one(cls, *args, **kwargs):
        return SimpleNamespace(
            files=[FileInfo.model_validate(item) for item in collection.files]
        )

    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    # Beanie exposes query fields only after document initialization.  This
    # focused repository test stubs the query execution, so a sentinel is
    # sufficient for constructing the ignored expression.
    monkeypatch.setattr(SessionDocument, "session_id", object(), raising=False)
    monkeypatch.setattr(SessionDocument, "find_one", classmethod(find_one))

    repository = MongoSessionRepository()
    previous = await repository.upsert_file_by_path(
        "session-1",
        FileInfo(
            file_id="artifact-v2",
            filename="report.md",
            file_path=artifact_path,
        ),
    )
    current = await repository.get_file_by_path("session-1", artifact_path)
    canonical_files = [
        FileInfo.model_validate(item) for item in collection.files
    ]
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        is_shared=True,
        files=canonical_files,
        events=[
            MessageEvent(
                role="assistant",
                message="first version",
                attachments=[FileInfo(file_id="artifact-v1")],
            ),
            MessageEvent(
                role="assistant",
                message="second version",
                attachments=[FileInfo(file_id="artifact-v2")],
            ),
        ],
    )

    authorized = _share_authorized_file_index(session)

    assert previous.file_id == "artifact-v1"
    assert canonical_files[0].file_path is None
    assert current.file_id == "artifact-v2"
    assert current.file_path == artifact_path
    assert set(authorized) == {"artifact-v1", "artifact-v2"}
    assert authorized["artifact-v1"].file_path is None
    assert authorized["artifact-v2"] == current


async def test_path_upsert_same_file_id_is_atomic_and_idempotent(monkeypatch):
    import asyncio

    artifact_path = "/home/ubuntu/upload/report.md"

    class Collection:
        def __init__(self):
            self.files = [
                {
                    "file_id": "artifact-v1",
                    "filename": "report.md",
                    "file_path": artifact_path,
                }
            ]
            self.lock = asyncio.Lock()

        async def find_one_and_update(
            self,
            query,
            pipeline,
            projection=None,
            return_document=None,
        ):
            async with self.lock:
                incoming_id = query["files"]["$not"]["$elemMatch"]["file_id"]
                if any(
                    item.get("file_id") == incoming_id
                    for item in self.files
                ):
                    return None
                previous = {"files": [dict(item) for item in self.files]}
                files_expression = pipeline[0]["$set"]["files"]
                successor = files_expression["$concatArrays"][1][0]["$literal"]
                self.files = [
                    {
                        **item,
                        "file_path": (
                            None
                            if item.get("file_path") == successor["file_path"]
                            else item.get("file_path")
                        ),
                    }
                    for item in self.files
                ] + [successor]
                return previous

        async def find_one(self, query, projection=None):
            return {
                "_id": "session-document",
                "files": [dict(item) for item in self.files],
            }

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = MongoSessionRepository()
    successor = FileInfo(
        file_id="artifact-v2",
        filename="report.md",
        file_path=artifact_path,
    )

    results = await asyncio.gather(*(
        repository.upsert_file_by_path("session-1", successor)
        for _ in range(20)
    ))
    retried = await repository.upsert_file_by_path("session-1", successor)

    assert [item["file_id"] for item in collection.files] == [
        "artifact-v1",
        "artifact-v2",
    ]
    assert collection.files[0]["file_path"] is None
    assert collection.files[1]["file_path"] == artifact_path
    assert sum(
        result is not None and result.file_id == "artifact-v1"
        for result in results
    ) == 1
    assert retried is None


async def test_save_existing_session_fails_closed_before_stale_overwrite(
    monkeypatch,
):
    class Collection:
        async def update_one(self, query, update):
            raise AssertionError("existing Session must not be updated by save")

    collection = Collection()

    async def find_one(cls, *args, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(SessionDocument, "session_id", object(), raising=False)
    monkeypatch.setattr(SessionDocument, "find_one", classmethod(find_one))
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        events=[MessageEvent(message="stale-event")],
        files=[FileInfo(file_id="stale-file", filename="stale.txt")],
    )

    with pytest.raises(RuntimeError, match="explicit atomic"):
        await MongoSessionRepository().save(session)


async def test_file_reference_check_includes_durable_turn_outbox(monkeypatch):
    class SessionCollection:
        async def count_documents(self, query, limit=None):
            assert query == {
                "$or": [
                    {"files.file_id": "artifact-file"},
                    {"events.attachments.file_id": "artifact-file"},
                ]
            }
            assert limit == 1
            return 0

    class OutboxCollection:
        async def count_documents(self, query, limit=None):
            assert query == {
                "event.attachments.file_id": "artifact-file"
            }
            assert limit == 1
            return 1

    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: SessionCollection()),
    )
    monkeypatch.setattr(
        TurnOutputEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: OutboxCollection()),
    )

    references = await MongoSessionRepository().count_file_references(
        "artifact-file"
    )

    assert references == 1


async def test_file_reference_check_short_circuits_after_session_hit(
    monkeypatch,
):
    class SessionCollection:
        async def count_documents(self, query, limit=None):
            return 1

    class OutboxCollection:
        async def count_documents(self, query, limit=None):
            raise AssertionError("outbox lookup should be skipped")

    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: SessionCollection()),
    )
    monkeypatch.setattr(
        TurnOutputEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: OutboxCollection()),
    )

    assert (
        await MongoSessionRepository().count_file_references("artifact-file")
        == 1
    )
