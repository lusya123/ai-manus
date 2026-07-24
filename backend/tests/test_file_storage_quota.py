import asyncio
import io
from types import SimpleNamespace

import pytest
from gridfs.errors import NoFile

from app.core.config import get_settings
from app.domain.external.file import (
    FileStorageBusyError,
    FileStorageQuotaExceededError,
    FileTooLargeError,
)
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage


@pytest.fixture(autouse=True)
def storage_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "none")
    monkeypatch.setenv("FILE_UPLOAD_MAX_BYTES", "10")
    monkeypatch.setenv("FILE_STORAGE_MAX_BYTES_PER_USER", "100")
    monkeypatch.setenv("FILE_STORAGE_MAX_FILES_PER_USER", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _storage():
    return GridFSFileStorage(SimpleNamespace(client=object()))


def test_single_file_limit_is_checked_before_gridfs_or_quota(monkeypatch):
    storage = _storage()

    async def must_not_reserve(*args, **kwargs):
        raise AssertionError("oversized files must not reserve quota")

    monkeypatch.setattr(storage, "_reserve_user_quota", must_not_reserve)

    with pytest.raises(FileTooLargeError, match="size limit"):
        asyncio.run(
            storage.upload_file(io.BytesIO(b"x" * 11), "large.bin", "user-1")
        )


@pytest.mark.asyncio
async def test_quota_reservation_is_atomic_across_concurrent_uploads(monkeypatch):
    storage = _storage()
    lock = asyncio.Lock()
    usage = {"bytes_used": 0, "file_count": 0}

    class Quotas:
        async def find_one_and_update(self, selector, update, return_document=None):
            async with lock:
                max_start_bytes = selector["bytes_used"]["$lte"]
                max_start_files = selector["file_count"]["$lt"]
                if (
                    usage["bytes_used"] > max_start_bytes
                    or usage["file_count"] >= max_start_files
                ):
                    return None
                usage["bytes_used"] += update["$inc"]["bytes_used"]
                usage["file_count"] += update["$inc"]["file_count"]
                return dict(usage)

    async def initialized(user_id):
        return None

    monkeypatch.setattr(storage, "_ensure_user_quota", initialized)
    monkeypatch.setattr(storage, "_get_quota_collection", lambda: Quotas())

    results = await asyncio.gather(
        storage._reserve_user_quota("user-1", 60),
        storage._reserve_user_quota("user-1", 60),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(
        isinstance(result, FileStorageQuotaExceededError) for result in results
    ) == 1
    assert usage == {"bytes_used": 60, "file_count": 1}


@pytest.mark.asyncio
async def test_quota_initialization_awaits_native_pymongo_aggregate(monkeypatch):
    storage = _storage()
    inserted = []

    class Cursor:
        async def to_list(self, length):
            assert length == 1
            return [{"bytes_used": 12, "file_count": 1}]

    class Files:
        async def aggregate(self, pipeline):
            assert pipeline[0]["$match"] == {"metadata.user_id": "user-1"}
            return Cursor()

    class Quotas:
        async def find_one(self, selector, projection=None):
            return None

        async def insert_one(self, document):
            inserted.append(document)

    monkeypatch.setattr(storage, "_get_files_collection", lambda: Files())
    monkeypatch.setattr(storage, "_get_quota_collection", lambda: Quotas())

    await storage._ensure_user_quota("user-1")

    assert inserted[0]["_id"] == "user-1"
    assert inserted[0]["bytes_used"] == 12
    assert inserted[0]["file_count"] == 1


@pytest.mark.asyncio
async def test_failed_gridfs_upload_releases_reserved_quota(monkeypatch):
    storage = _storage()
    calls = []

    class Bucket:
        async def upload_from_stream_with_id(self, *args, **kwargs):
            raise RuntimeError("gridfs unavailable")

        async def delete(self, file_id):
            raise NoFile("upload never committed")

    async def reserve(user_id, size, reservation_id=None):
        calls.append(("reserve", user_id, size))

    async def release(user_id, size, reservation_id=None):
        calls.append(("release", user_id, size))

    async def record_intent(*args, **kwargs):
        return None

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())

    with pytest.raises(RuntimeError, match="gridfs unavailable"):
        await storage.upload_file(io.BytesIO(b"12345"), "small.bin", "user-1")

    assert calls == [
        ("reserve", "user-1", 5),
        ("release", "user-1", 5),
    ]


@pytest.mark.asyncio
async def test_cancelled_gridfs_upload_deletes_known_id_and_releases_quota(
    monkeypatch,
):
    storage = _storage()
    calls = []
    upload_started = asyncio.Event()
    finish_upload = asyncio.Event()

    class Bucket:
        async def upload_from_stream_with_id(
            self,
            file_id,
            filename,
            file_data,
            metadata=None,
        ):
            calls.append(("upload", file_id))
            upload_started.set()
            await finish_upload.wait()

        async def delete(self, file_id):
            calls.append(("delete", file_id))

    bucket = Bucket()

    async def reserve(user_id, size, reservation_id=None):
        calls.append(("reserve", user_id, size))

    async def release(user_id, size, reservation_id=None):
        calls.append(("release", user_id, size))

    async def record_intent(*args, **kwargs):
        return None

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: bucket)

    task = asyncio.create_task(
        storage.upload_file(
            io.BytesIO(b"12345"),
            "small.bin",
            "user-1",
        )
    )
    await upload_started.wait()
    task.cancel()
    finish_upload.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    upload_id = next(item[1] for item in calls if item[0] == "upload")
    assert ("delete", upload_id) in calls
    assert ("release", "user-1", 5) in calls


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_detach_gridfs_rollback(monkeypatch):
    storage = _storage()
    calls = []
    upload_started = asyncio.Event()
    finish_upload = asyncio.Event()
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()

    class Bucket:
        async def upload_from_stream_with_id(
            self,
            file_id,
            filename,
            file_data,
            metadata=None,
        ):
            calls.append(("upload", file_id))
            upload_started.set()
            await finish_upload.wait()

        async def delete(self, file_id):
            calls.append(("delete", file_id))
            rollback_started.set()
            await release_rollback.wait()

    async def reserve(user_id, size, reservation_id=None):
        calls.append(("reserve", user_id, size))

    async def release(user_id, size, reservation_id=None):
        calls.append(("release", user_id, size))

    async def record_intent(*args, **kwargs):
        return None

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())

    task = asyncio.create_task(
        storage.upload_file(io.BytesIO(b"12345"), "small.bin", "user-1")
    )
    await upload_started.wait()
    task.cancel()
    finish_upload.set()
    await rollback_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    release_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)

    assert ("release", "user-1", 5) in calls


@pytest.mark.asyncio
async def test_cancel_during_quota_reserve_waits_then_releases(monkeypatch):
    storage = _storage()
    reserve_started = asyncio.Event()
    release_reserve_reply = asyncio.Event()
    calls = []

    async def reserve(user_id, size, reservation_id=None):
        calls.append(("reserve-committed", user_id, size))
        reserve_started.set()
        await release_reserve_reply.wait()

    async def release(user_id, size, reservation_id=None):
        calls.append(("release", user_id, size))

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(
        storage,
        "_get_gridfs_bucket",
        lambda: (_ for _ in ()).throw(
            AssertionError("upload must not begin after caller cancellation")
        ),
    )

    task = asyncio.create_task(
        storage.upload_file(io.BytesIO(b"12345"), "small.bin", "user-1")
    )
    await reserve_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_reserve_reply.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)

    assert calls == [
        ("reserve-committed", "user-1", 5),
        ("release", "user-1", 5),
    ]


@pytest.mark.asyncio
async def test_hung_rollbacks_keep_bounded_upload_slots(monkeypatch):
    storage = _storage()
    storage._MAX_CONCURRENT_UPLOADS = 2
    storage._ROLLBACK_TIMEOUT_SECONDS = 0.01
    uploads_started = 0
    all_uploads_started = asyncio.Event()
    rollback_started = asyncio.Event()
    rollback_count = 0
    release_rollbacks = asyncio.Event()
    finish_uploads = asyncio.Event()

    class Bucket:
        async def upload_from_stream_with_id(self, *args, **kwargs):
            nonlocal uploads_started
            uploads_started += 1
            if uploads_started == 2:
                all_uploads_started.set()
            await finish_uploads.wait()

        async def delete(self, file_id):
            nonlocal rollback_count
            rollback_count += 1
            if rollback_count == 2:
                rollback_started.set()
            await release_rollbacks.wait()

    async def reserve(user_id, size, reservation_id=None):
        return None

    async def release(user_id, size, reservation_id=None):
        return None

    async def record_intent(*args, **kwargs):
        return None

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())

    first = asyncio.create_task(
        storage.upload_file(io.BytesIO(b"1"), "one.bin", "user-1")
    )
    second = asyncio.create_task(
        storage.upload_file(io.BytesIO(b"2"), "two.bin", "user-1")
    )
    await all_uploads_started.wait()
    first.cancel()
    second.cancel()
    finish_uploads.set()
    await rollback_started.wait()

    with pytest.raises(FileStorageBusyError):
        await storage.upload_file(io.BytesIO(b"3"), "three.bin", "user-1")

    release_rollbacks.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_cancel_after_gridfs_delete_waits_for_quota_release(monkeypatch):
    storage = _storage()
    file_id = "507f1f77bcf86cd799439011"
    delete_finished = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    calls = []

    class Files:
        async def find_one(self, query):
            return {
                "_id": query["_id"],
                "length": 5,
                "metadata": {"user_id": "user-1"},
            }

    class Bucket:
        async def delete(self, object_id):
            calls.append(("delete", str(object_id)))
            delete_finished.set()

    async def initialized(user_id):
        return None

    async def release(user_id, size, reservation_id=None):
        calls.append(("release-start", user_id, size))
        release_started.set()
        await allow_release.wait()
        calls.append(("release-done", user_id, size))

    async def record_intent(user_id, reservation_id, size):
        calls.append(("intent", user_id, reservation_id, size))

    monkeypatch.setattr(storage, "_get_files_collection", lambda: Files())
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())
    monkeypatch.setattr(storage, "_ensure_user_quota", initialized)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)

    task = asyncio.create_task(storage.delete_file(file_id, "user-1"))
    await delete_finished.wait()
    await release_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

    assert calls == [
        ("intent", "user-1", file_id, 5),
        ("delete", file_id),
        ("release-start", "user-1", 5),
        ("release-done", "user-1", 5),
    ]


@pytest.mark.asyncio
async def test_reserve_reply_lost_confirms_exact_operation(monkeypatch):
    storage = _storage()
    reservation_id = "507f1f77bcf86cd799439011"
    document = {"active_reservations": []}
    mutation_calls = 0

    class Quotas:
        async def find_one_and_update(self, selector, update, **kwargs):
            nonlocal mutation_calls
            mutation_calls += 1
            document["active_reservations"].append(
                update["$push"]["active_reservations"]
            )
            raise ConnectionError("reply lost after commit")

        async def find_one(self, selector, projection=None):
            return document

    async def initialized(user_id):
        return None

    monkeypatch.setattr(storage, "_ensure_user_quota", initialized)
    monkeypatch.setattr(storage, "_get_quota_collection", lambda: Quotas())

    await storage._reserve_user_quota("user-1", 5, reservation_id)

    assert mutation_calls == 1
    assert [
        item["reservation_id"] for item in document["active_reservations"]
    ] == [reservation_id]


@pytest.mark.asyncio
async def test_release_reply_lost_is_exactly_once(monkeypatch):
    storage = _storage()
    reservation_id = "507f1f77bcf86cd799439011"
    document = {
        "bytes_used": 5,
        "file_count": 1,
        "active_reservations": [
            {"reservation_id": reservation_id, "size": 5}
        ],
        "pending_deletions": [],
        "released_reservation_ids": [],
    }
    mutation_calls = 0

    class Quotas:
        async def find_one_and_update(self, selector, update, **kwargs):
            nonlocal mutation_calls
            mutation_calls += 1
            document["bytes_used"] -= 5
            document["file_count"] -= 1
            document["active_reservations"] = []
            document["released_reservation_ids"].append(reservation_id)
            raise ConnectionError("reply lost after commit")

        async def find_one(self, selector, projection=None):
            return document

    monkeypatch.setattr(storage, "_get_quota_collection", lambda: Quotas())

    await storage._release_user_quota("user-1", 5, reservation_id)

    assert mutation_calls == 1
    assert document["bytes_used"] == 0
    assert document["file_count"] == 0
    assert document["released_reservation_ids"] == [reservation_id]


@pytest.mark.asyncio
async def test_delete_records_intent_before_blob_and_survives_release_reply_lost(
    monkeypatch,
):
    storage = _storage()
    file_id = "507f1f77bcf86cd799439011"
    calls = []
    document = {
        "bytes_used": 5,
        "file_count": 1,
        "active_reservations": [
            {"reservation_id": file_id, "size": 5}
        ],
        "pending_deletions": [],
        "released_reservation_ids": [],
    }

    class Files:
        async def find_one(self, query, projection=None):
            return {
                "_id": query["_id"],
                "length": 5,
                "metadata": {"user_id": "user-1"},
            }

    class Bucket:
        async def delete(self, object_id):
            calls.append(("delete", str(object_id)))

    class Quotas:
        async def find_one_and_update(self, selector, update, **kwargs):
            document["bytes_used"] = 0
            document["file_count"] = 0
            document["pending_deletions"] = []
            document["active_reservations"] = []
            document["released_reservation_ids"].append(file_id)
            raise ConnectionError("release reply lost after commit")

        async def find_one(self, selector, projection=None):
            return document

    async def initialized(user_id):
        return None

    async def record_intent(user_id, reservation_id, size):
        calls.append(("intent", reservation_id, size))
        document["active_reservations"] = []
        document["pending_deletions"] = [
            {"reservation_id": reservation_id, "size": size}
        ]

    monkeypatch.setattr(storage, "_get_files_collection", lambda: Files())
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())
    monkeypatch.setattr(storage, "_get_quota_collection", lambda: Quotas())
    monkeypatch.setattr(storage, "_ensure_user_quota", initialized)
    monkeypatch.setattr(storage, "_record_deletion_intent", record_intent)

    assert await storage.delete_file(file_id, "user-1") is True
    assert calls == [("intent", file_id, 5), ("delete", file_id)]
    assert document["bytes_used"] == 0
    assert document["file_count"] == 0
    assert document["released_reservation_ids"] == [file_id]
