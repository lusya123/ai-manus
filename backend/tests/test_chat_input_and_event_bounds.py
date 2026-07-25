import asyncio
from types import SimpleNamespace

import pytest
from bson import BSON
from pydantic import ValidationError

from app.domain.models.file import FileInfo
from app.domain.models.event import ErrorEvent, MessageEvent
from app.infrastructure.models.documents import SessionDocument
import app.infrastructure.repositories.mongo_session_repository as repository_module
from app.infrastructure.repositories.mongo_session_repository import (
    MongoSessionRepository,
)
from app.interfaces.schemas.session import ChatRequest


def test_chat_request_requires_uuid_and_enforces_decoded_limits():
    with pytest.raises(ValidationError, match="submission_id"):
        ChatRequest(message="hello")

    with pytest.raises(ValidationError, match="message is too large"):
        ChatRequest(
            message="界" * 22_000,
            submission_id="11111111-1111-4111-8111-111111111111",
        )

    with pytest.raises(ValidationError):
        ChatRequest(
            message="hello",
            submission_id="11111111-1111-4111-8111-111111111111",
            attachments=[
                {"file_id": f"file-{index}", "filename": "a.txt"}
                for index in range(11)
            ],
        )

    with pytest.raises(ValidationError):
        ChatRequest(
            message="hello",
            submission_id="11111111-1111-4111-8111-111111111111",
            attachments=[{"file_id": "x" * 257}],
        )

    with pytest.raises(ValidationError, match="submission_id"):
        ChatRequest(
            message="",
            attachments=[{"file_id": "attachment-only"}],
        )

    attachment_only = ChatRequest(
        message="",
        submission_id="22222222-2222-4222-8222-222222222222",
        attachments=[{"file_id": "attachment-only"}],
    )
    assert str(attachment_only.submission_id) == (
        "22222222-2222-4222-8222-222222222222"
    )

    # Reconnect-only requests intentionally do not need a submission UUID.
    assert ChatRequest(message="", event_id="1-0").submission_id is None


class _BoundedSessionCollection:
    def __init__(self, *, max_bytes=8 * 1024 * 1024):
        self.events = []
        self.lock = asyncio.Lock()
        self.max_bytes = max_bytes
        self.pipeline = None

    async def update_one(self, query, update, array_filters=None):
        async with self.lock:
            if array_filters:
                event_id = array_filters[0]["event.id"]
                for event in self.events:
                    if event["id"] == event_id:
                        event["transport_id"] = update["$set"][
                            "events.$[event].transport_id"
                        ]
                return SimpleNamespace(matched_count=1, modified_count=1)

            self.pipeline = update
            events_expr = update[0]["$set"]["events"]["$let"]
            candidate_expr = events_expr["vars"]["candidates"]["$slice"]
            max_events = -candidate_expr[1]
            incoming = candidate_expr[0]["$concatArrays"][1]["$literal"]
            candidate = incoming[0]
            if any(event["id"] == candidate["id"] for event in self.events):
                return SimpleNamespace(matched_count=0, modified_count=0)
            candidates = (self.events + incoming)[-max_events:]
            retained = []
            retained_bytes = 0
            for current in reversed(candidates):
                current_bytes = (
                    len(BSON.encode(current))
                    + repository_module._HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
                )
                if retained_bytes + current_bytes > self.max_bytes:
                    break
                retained.append(current)
                retained_bytes += current_bytes
            self.events = list(reversed(retained))
            return SimpleNamespace(matched_count=1, modified_count=1)

    async def find_one(self, query, projection=None):
        return {"_id": "session-document"}


async def test_session_event_append_is_atomic_bounded_and_idempotent(
    monkeypatch,
):
    collection = _BoundedSessionCollection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    monkeypatch.setattr(
        "app.infrastructure.repositories.mongo_session_repository.get_settings",
        lambda: SimpleNamespace(
            session_history_max_events=5,
            session_event_max_bytes=1024,
            session_history_max_bytes=4096,
        ),
    )
    repository = MongoSessionRepository()
    events = [MessageEvent(message=f"event-{index}") for index in range(20)]

    await asyncio.gather(
        *(repository.add_event_once("session-1", event) for event in events),
        repository.add_event_once("session-1", events[-1]),
    )

    assert len(collection.events) == 5
    assert len({event["id"] for event in collection.events}) == 5
    assert [event["message"] for event in collection.events] == [
        f"event-{index}" for index in range(15, 20)
    ]


async def test_oversized_single_event_is_replaced_with_safe_summary(monkeypatch):
    collection = _BoundedSessionCollection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    monkeypatch.setattr(
        "app.infrastructure.repositories.mongo_session_repository.get_settings",
        lambda: SimpleNamespace(
            session_history_max_events=5,
            session_event_max_bytes=1024,
            session_history_max_bytes=4096,
        ),
    )
    original = MessageEvent(
        id="stable-event",
        turn_id="turn-1",
        message="secret-output" * 500,
    )

    persisted = await MongoSessionRepository().add_event_once(
        "session-1", original
    )

    assert isinstance(persisted, ErrorEvent)
    assert persisted.id == "stable-event"
    assert persisted.turn_id == "turn-1"
    assert "secret-output" not in collection.events[0]["error"]


async def test_session_event_append_atomically_trims_bson_bytes_and_uses_literal(
    monkeypatch,
):
    max_bytes = 2300
    collection = _BoundedSessionCollection(max_bytes=max_bytes)
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    monkeypatch.setattr(
        "app.infrastructure.repositories.mongo_session_repository.get_settings",
        lambda: SimpleNamespace(
            session_history_max_events=20,
            session_event_max_bytes=2048,
            session_history_max_bytes=max_bytes,
        ),
    )
    repository = MongoSessionRepository()
    old_events = [
        MessageEvent(
            id=f"old-{index}",
            message=f"old-{index}|" + ("x" * 350),
            attachments=[
                FileInfo(
                    file_id=f"file-{index}",
                    filename="$attachment-" + ("y" * 350),
                )
            ],
        )
        for index in range(8)
    ]
    for event in old_events:
        await repository.add_event_once("session-1", event)

    incoming = MessageEvent(
        id="latest",
        turn_id="turn-1",
        message="$literal-message",
        attachments=[
            FileInfo(file_id="latest-file", filename="$literal-filename")
        ],
    )
    persisted = await repository.add_event_once("session-1", incoming)
    retained_after_first_append = list(collection.events)
    await repository.add_event_once("session-1", incoming)

    retained_bytes = sum(
        len(BSON.encode(event))
        + repository_module._HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
        for event in collection.events
    )
    assert persisted is incoming
    assert retained_bytes <= max_bytes
    assert len(collection.events) < len(old_events) + 1
    assert collection.events[-1]["message"] == "$literal-message"
    assert (
        collection.events[-1]["attachments"][0]["filename"]
        == "$literal-filename"
    )
    assert collection.events == retained_after_first_append

    events_expr = collection.pipeline[0]["$set"]["events"]["$let"]
    candidate_expr = events_expr["vars"]["candidates"]["$slice"]
    assert candidate_expr[0]["$concatArrays"][1] == {
        "$literal": [incoming.model_dump()]
    }
    retained_expr = events_expr["in"]["$let"]["vars"]["retained"]
    assert (
        retained_expr["$reduce"]["in"]["$let"]["vars"]["event_bytes"]
        ["$add"][0]
        == {"$bsonSize": "$$this"}
    )


async def test_session_file_projection_is_idempotent_by_canonical_file_id(
    monkeypatch,
):
    class FileCollection:
        def __init__(self):
            self.files = []
            self.lock = asyncio.Lock()

        async def update_one(self, query, update):
            async with self.lock:
                file_id = query["files"]["$not"]["$elemMatch"]["file_id"]
                if any(item["file_id"] == file_id for item in self.files):
                    return SimpleNamespace(matched_count=0, modified_count=0)
                self.files.append(update["$push"]["files"])
                return SimpleNamespace(matched_count=1, modified_count=1)

        async def find_one(self, query, projection=None):
            return {"_id": "session-document"}

    collection = FileCollection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    file_info = FileInfo(
        file_id="canonical-file",
        filename="$same-attachment.txt",
    )
    repository = MongoSessionRepository()

    await asyncio.gather(*(
        repository.add_file("session-1", file_info)
        for _ in range(20)
    ))

    assert collection.files == [file_info.model_dump()]
    with pytest.raises(ValueError, match="canonical file ID"):
        await repository.add_file(
            "session-1",
            FileInfo(filename="missing-id.txt"),
        )
