import asyncio
import io
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.domain.external.file import (
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
        async def upload_from_stream(self, *args, **kwargs):
            raise RuntimeError("gridfs unavailable")

    async def reserve(user_id, size):
        calls.append(("reserve", user_id, size))

    async def release(user_id, size):
        calls.append(("release", user_id, size))

    monkeypatch.setattr(storage, "_reserve_user_quota", reserve)
    monkeypatch.setattr(storage, "_release_user_quota", release)
    monkeypatch.setattr(storage, "_get_gridfs_bucket", lambda: Bucket())

    with pytest.raises(RuntimeError, match="gridfs unavailable"):
        await storage.upload_file(io.BytesIO(b"12345"), "small.bin", "user-1")

    assert calls == [
        ("reserve", "user-1", 5),
        ("release", "user-1", 5),
    ]
