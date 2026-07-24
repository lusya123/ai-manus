import logging
import io
import inspect
import asyncio
from typing import BinaryIO, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from bson import ObjectId
from gridfs import AsyncGridFSBucket
from gridfs.errors import NoFile
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain.external.file import (
    FileStorage,
    FileStorageBusyError,
    FileStorageQuotaExceededError,
    FileTooLargeError,
)
from app.domain.models.file import FileInfo
from app.infrastructure.storage.mongodb import MongoDB
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from functools import lru_cache

logger = logging.getLogger(__name__)


_ACTIVE_UPLOADS_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    int,
] = {}
_QUOTA_RECONCILIATIONS_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    set[asyncio.Task[Any]],
] = {}


async def _await_task_to_known_outcome(task: asyncio.Task[Any]) -> Any:
    """Suppress repeated caller cancellation until a Mongo mutation replies."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
    return task.result()


class GridFSFileStorage(FileStorage):
    """MongoDB GridFS-based file storage implementation"""
    _ROLLBACK_TIMEOUT_SECONDS = 5.0
    _MAX_CONCURRENT_UPLOADS = 64
    _QUOTA_MUTATION_RETRIES = 3
    _RELEASE_TOMBSTONE_LIMIT = 1024
    _STALE_RESERVATION_SECONDS = 3600
    _MAX_RECONCILE_OPERATIONS = 16
    _MAX_ACTIVE_QUOTA_RECONCILIATIONS = 64
    _RECONCILE_DELETE_TIMEOUT_SECONDS = 5.0
    
    def __init__(self, mongodb: MongoDB, bucket_name: str = "fs"):
        """
        Initialize GridFS file storage
        
        Args:
            mongodb: MongoDB connection instance
            bucket_name: GridFS bucket name, default is 'fs'
        """
        self.mongodb = mongodb
        self.bucket_name = bucket_name
        self.settings = get_settings()
    
    def _get_gridfs_bucket(self) -> AsyncGridFSBucket:
        """Get async GridFS Bucket instance"""
        if not self.mongodb.client:
            raise RuntimeError("MongoDB client not initialized")
        
        # Use database name from configuration
        database = self.mongodb.client[self.settings.mongodb_database]
        return AsyncGridFSBucket(database, bucket_name=self.bucket_name)
    
    def _get_files_collection(self):
        """Get files collection for querying file metadata"""
        if not self.mongodb.client:
            raise RuntimeError("MongoDB client not initialized")
        
        database = self.mongodb.client[self.settings.mongodb_database]
        return database[f"{self.bucket_name}.files"]

    def _get_quota_collection(self):
        if not self.mongodb.client:
            raise RuntimeError("MongoDB client not initialized")
        database = self.mongodb.client[self.settings.mongodb_database]
        return database["file_storage_quotas"]

    @staticmethod
    def _remaining_stream_size(file_data: BinaryIO) -> int:
        try:
            position = file_data.tell()
            file_data.seek(0, 2)
            end = file_data.tell()
            file_data.seek(position)
        except (AttributeError, OSError) as exc:
            raise FileTooLargeError(
                "File size cannot be verified before storage"
            ) from exc
        size = end - position
        if size < 0:
            raise FileTooLargeError("File size is invalid")
        return size

    async def _ensure_user_quota(self, user_id: str) -> None:
        quotas = self._get_quota_collection()
        existing = await quotas.find_one(
            {"_id": user_id},
            projection={
                "_id": 1,
                "active_reservations": 1,
                "pending_deletions": 1,
            },
        )
        if existing:
            await self._reconcile_quota_operations(
                user_id,
                existing.get("active_reservations", ()),
                existing.get("pending_deletions", ()),
            )
            return
        files = self._get_files_collection()
        aggregate_result = files.aggregate(
            [
                {"$match": {"metadata.user_id": user_id}},
                {
                    "$group": {
                        "_id": None,
                        "bytes_used": {"$sum": "$length"},
                        "file_count": {"$sum": 1},
                    }
                },
            ]
        )
        # PyMongo's native AsyncCollection (used by Beanie 2) makes
        # ``aggregate`` awaitable, while Motor returns its cursor directly.
        # Support both APIs during the upstream migration instead of calling
        # ``to_list`` on the PyMongo coroutine.
        cursor = (
            await aggregate_result
            if inspect.isawaitable(aggregate_result)
            else aggregate_result
        )
        rows = await cursor.to_list(length=1)
        usage = rows[0] if rows else {"bytes_used": 0, "file_count": 0}
        try:
            await quotas.insert_one(
                {
                    "_id": user_id,
                    "bytes_used": int(usage.get("bytes_used", 0)),
                    "file_count": int(usage.get("file_count", 0)),
                    "active_reservations": [],
                    "pending_deletions": [],
                    "released_reservation_ids": [],
                    "updated_at": datetime.utcnow(),
                }
            )
        except DuplicateKeyError:
            # A concurrent replica initialized the authoritative counter.
            pass

    @staticmethod
    def _has_quota_operation(
        document: Optional[Dict[str, Any]],
        field: str,
        reservation_id: str,
    ) -> bool:
        if not document:
            return False
        entries = document.get(field, ()) or ()
        if field == "released_reservation_ids":
            return reservation_id in entries
        return any(
            isinstance(entry, dict)
            and entry.get("reservation_id") == reservation_id
            for entry in entries
        )

    async def _reconcile_quota_operations(
        self,
        user_id: str,
        active_reservations: Any,
        pending_deletions: Any,
    ) -> None:
        """Repair durable deletion intents and stale failed reservations."""
        pending_deletions = (
            pending_deletions
            if isinstance(pending_deletions, (list, tuple))
            else ()
        )
        active_reservations = (
            active_reservations
            if isinstance(active_reservations, (list, tuple))
            else ()
        )
        files = self._get_files_collection()
        bucket = self._get_gridfs_bucket()

        async def reconcile_pending(pending: Any) -> None:
            if not isinstance(pending, dict):
                return
            reservation_id = str(pending.get("reservation_id", ""))
            if not reservation_id:
                return
            loop = asyncio.get_running_loop()
            active = _QUOTA_RECONCILIATIONS_BY_LOOP.setdefault(loop, set())
            if len(active) >= self._MAX_ACTIVE_QUOTA_RECONCILIATIONS:
                return

            async def finish_pending() -> None:
                try:
                    await bucket.delete(self._to_object_id(reservation_id))
                except NoFile:
                    pass
                await self._release_user_quota(
                    user_id,
                    int(pending.get("size", 0)),
                    reservation_id,
                )

            mutation_task = asyncio.create_task(finish_pending())
            active.add(mutation_task)

            def release_slot(done_task: asyncio.Task[Any]) -> None:
                current = _QUOTA_RECONCILIATIONS_BY_LOOP.get(loop)
                if current is not None:
                    current.discard(done_task)
                    if not current:
                        _QUOTA_RECONCILIATIONS_BY_LOOP.pop(loop, None)
                try:
                    done_task.result()
                except BaseException as error:
                    logger.warning(
                        "Deferred GridFS quota reconciliation: user_id=%s "
                        "file_id=%s error=%s",
                        user_id,
                        reservation_id,
                        safe_exception_summary(error),
                    )

            mutation_task.add_done_callback(release_slot)
            try:
                await asyncio.wait_for(
                    asyncio.shield(mutation_task),
                    timeout=self._RECONCILE_DELETE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # The strong process-wide registry retains the exact mutation;
                # the durable intent remains authoritative across a restart.
                return

        async def reconcile_stale(reservation: Any) -> None:
            if not isinstance(reservation, dict):
                return
            reservation_id = str(reservation.get("reservation_id", ""))
            created_at = reservation.get("created_at")
            if not reservation_id or not isinstance(created_at, datetime):
                return
            if created_at > datetime.utcnow() - timedelta(
                seconds=self._STALE_RESERVATION_SECONDS
            ):
                return
            try:
                object_id = self._to_object_id(reservation_id)
                file_exists = await files.find_one(
                    {"_id": object_id},
                    projection={"_id": 1},
                )
                if file_exists is None:
                    await self._release_user_quota(
                        user_id,
                        int(reservation.get("size", 0)),
                        reservation_id,
                    )
            except Exception as error:
                logger.warning(
                    "Deferred stale GridFS reservation reconciliation: "
                    "user_id=%s file_id=%s error=%s",
                    user_id,
                    reservation_id,
                    safe_exception_summary(error),
                )

        operations = [
            *(reconcile_pending(item) for item in pending_deletions),
            *(reconcile_stale(item) for item in active_reservations),
        ][: self._MAX_RECONCILE_OPERATIONS]
        if operations:
            await asyncio.gather(*operations)

    async def _reserve_user_quota(
        self,
        user_id: str,
        size: int,
        reservation_id: Optional[str] = None,
    ) -> None:
        await self._ensure_user_quota(user_id)
        generated_reservation = reservation_id is None
        reservation_id = reservation_id or str(ObjectId())
        max_bytes = max(1, int(self.settings.file_storage_max_bytes_per_user))
        max_files = max(1, int(self.settings.file_storage_max_files_per_user))
        quotas = self._get_quota_collection()
        try:
            result = await quotas.find_one_and_update(
                {
                    "_id": user_id,
                    "bytes_used": {"$lte": max_bytes - size},
                    "file_count": {"$lt": max_files},
                    "active_reservations.reservation_id": {
                        "$ne": reservation_id
                    },
                    "released_reservation_ids": {"$ne": reservation_id},
                },
                {
                    "$inc": {"bytes_used": size, "file_count": 1},
                    "$push": {
                        "active_reservations": {
                            "reservation_id": reservation_id,
                            "size": size,
                            "created_at": datetime.utcnow(),
                        }
                    },
                    "$set": {"updated_at": datetime.utcnow()},
                },
                return_document=ReturnDocument.AFTER,
            )
        except Exception:
            # A reply-lost write may already have committed. Read the exact
            # durable operation before surfacing an ambiguous failure.
            confirmation = await quotas.find_one(
                {"_id": user_id},
                projection={"active_reservations": 1},
            )
            if self._has_quota_operation(
                confirmation,
                "active_reservations",
                reservation_id,
            ):
                return
            raise
        if result is None:
            if not generated_reservation:
                confirmation = await quotas.find_one(
                    {"_id": user_id},
                    projection={
                        "active_reservations": 1,
                        "released_reservation_ids": 1,
                    },
                )
                if self._has_quota_operation(
                    confirmation,
                    "active_reservations",
                    reservation_id,
                ):
                    return
            raise FileStorageQuotaExceededError(
                "User file-storage quota has been reached"
            )

    async def _release_user_quota(
        self,
        user_id: str,
        size: int,
        reservation_id: Optional[str] = None,
    ) -> None:
        quotas = self._get_quota_collection()
        if reservation_id is None:
            # Compatibility for old direct callers. All production upload and
            # delete paths pass a stable GridFS ID and use the idempotent path.
            await quotas.update_one(
                {"_id": user_id},
                [
                    {
                        "$set": {
                            "bytes_used": {
                                "$max": [
                                    0,
                                    {"$subtract": ["$bytes_used", size]},
                                ]
                            },
                            "file_count": {
                                "$max": [
                                    0,
                                    {"$subtract": ["$file_count", 1]},
                                ]
                            },
                            "updated_at": datetime.utcnow(),
                        }
                    }
                ],
            )
            return

        last_error: Optional[BaseException] = None
        for attempt in range(self._QUOTA_MUTATION_RETRIES):
            try:
                result = await quotas.find_one_and_update(
                    {
                        "_id": user_id,
                        "released_reservation_ids": {
                            "$ne": reservation_id
                        },
                        "$or": [
                            {
                                "active_reservations.reservation_id":
                                reservation_id
                            },
                            {
                                "pending_deletions.reservation_id":
                                reservation_id
                            },
                        ],
                    },
                    [
                        {
                            "$set": {
                                "bytes_used": {
                                    "$max": [
                                        0,
                                        {
                                            "$subtract": [
                                                {"$ifNull": ["$bytes_used", 0]},
                                                size,
                                            ]
                                        },
                                    ]
                                },
                                "file_count": {
                                    "$max": [
                                        0,
                                        {
                                            "$subtract": [
                                                {"$ifNull": ["$file_count", 0]},
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                "active_reservations": {
                                    "$filter": {
                                        "input": {
                                            "$ifNull": [
                                                "$active_reservations",
                                                [],
                                            ]
                                        },
                                        "as": "reservation",
                                        "cond": {
                                            "$ne": [
                                                "$$reservation.reservation_id",
                                                reservation_id,
                                            ]
                                        },
                                    }
                                },
                                "pending_deletions": {
                                    "$filter": {
                                        "input": {
                                            "$ifNull": [
                                                "$pending_deletions",
                                                [],
                                            ]
                                        },
                                        "as": "deletion",
                                        "cond": {
                                            "$ne": [
                                                "$$deletion.reservation_id",
                                                reservation_id,
                                            ]
                                        },
                                    }
                                },
                                "released_reservation_ids": {
                                    "$slice": [
                                        {
                                            "$concatArrays": [
                                                {
                                                    "$ifNull": [
                                                        "$released_reservation_ids",
                                                        [],
                                                    ]
                                                },
                                                [reservation_id],
                                            ]
                                        },
                                        -self._RELEASE_TOMBSTONE_LIMIT,
                                    ]
                                },
                                "updated_at": datetime.utcnow(),
                            }
                        }
                    ],
                    return_document=ReturnDocument.AFTER,
                )
                if result is not None:
                    return
            except Exception as error:
                last_error = error

            try:
                confirmation = await quotas.find_one(
                    {"_id": user_id},
                    projection={
                        "active_reservations": 1,
                        "pending_deletions": 1,
                        "released_reservation_ids": 1,
                    },
                )
                if self._has_quota_operation(
                    confirmation,
                    "released_reservation_ids",
                    reservation_id,
                ):
                    return
                if not (
                    self._has_quota_operation(
                        confirmation,
                        "active_reservations",
                        reservation_id,
                    )
                    or self._has_quota_operation(
                        confirmation,
                        "pending_deletions",
                        reservation_id,
                    )
                ):
                    # The reservation never committed, so releasing it is an
                    # idempotent no-op rather than a blind decrement.
                    return
            except Exception as confirmation_error:
                last_error = last_error or confirmation_error
            if attempt + 1 < self._QUOTA_MUTATION_RETRIES:
                await asyncio.sleep(0.05 * (attempt + 1))

        if last_error is not None:
            raise last_error
        raise RuntimeError("GridFS quota release did not reach a known state")

    async def _record_deletion_intent(
        self,
        user_id: str,
        reservation_id: str,
        size: int,
    ) -> None:
        quotas = self._get_quota_collection()
        last_error: Optional[BaseException] = None
        for attempt in range(self._QUOTA_MUTATION_RETRIES):
            try:
                await quotas.update_one(
                    {"_id": user_id},
                    [
                        {
                            "$set": {
                                "active_reservations": {
                                    "$filter": {
                                        "input": {
                                            "$ifNull": [
                                                "$active_reservations",
                                                [],
                                            ]
                                        },
                                        "as": "reservation",
                                        "cond": {
                                            "$ne": [
                                                "$$reservation.reservation_id",
                                                reservation_id,
                                            ]
                                        },
                                    }
                                },
                                "pending_deletions": {
                                    "$cond": [
                                        {
                                            "$or": [
                                                {
                                                    "$in": [
                                                        reservation_id,
                                                        {
                                                            "$map": {
                                                                "input": {
                                                                    "$ifNull": [
                                                                        "$pending_deletions",
                                                                        [],
                                                                    ]
                                                                },
                                                                "as": "deletion",
                                                                "in": "$$deletion.reservation_id",
                                                            }
                                                        },
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        reservation_id,
                                                        {
                                                            "$ifNull": [
                                                                "$released_reservation_ids",
                                                                [],
                                                            ]
                                                        },
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$ifNull": [
                                                "$pending_deletions",
                                                [],
                                            ]
                                        },
                                        {
                                            "$concatArrays": [
                                                {
                                                    "$ifNull": [
                                                        "$pending_deletions",
                                                        [],
                                                    ]
                                                },
                                                [
                                                    {
                                                        "reservation_id": reservation_id,
                                                        "size": size,
                                                        "created_at": datetime.utcnow(),
                                                    }
                                                ],
                                            ]
                                        },
                                    ]
                                },
                                "updated_at": datetime.utcnow(),
                            }
                        }
                    ],
                )
                return
            except Exception as error:
                last_error = error
                # The pipeline is idempotent, including after a reply-lost
                # commit, so a bounded retry cannot duplicate the intent.
                if attempt + 1 < self._QUOTA_MUTATION_RETRIES:
                    await asyncio.sleep(0.05 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _to_object_id(file_id: str) -> ObjectId:
        try:
            return ObjectId(file_id)
        except Exception as exc:
            raise FileNotFoundError(
                f"File not found with ID: {file_id}"
            ) from exc
    
    def _create_file_info(self, file_info: Dict[str, Any], file_id: str) -> FileInfo:
        """Create FileInfo object from GridFS file metadata"""
        metadata = file_info.get('metadata', {})
        return FileInfo(
            file_id=str(file_info['_id']),
            filename=file_info.get('filename', f"file_{file_id}"),
            content_type=metadata.get('contentType'),
            size=file_info.get('length', 0),
            upload_date=file_info.get('uploadDate', datetime.utcnow()),
            metadata=metadata,
            user_id=metadata.get('user_id', '')  # Get user_id from metadata
        )
    
    async def upload_file(
        self,
        file_data: BinaryIO,
        filename: str,
        user_id: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> FileInfo:
        """Upload under a process-wide bound that includes rollback time."""
        loop = asyncio.get_running_loop()
        active_uploads = _ACTIVE_UPLOADS_BY_LOOP.get(loop, 0)
        if active_uploads >= self._MAX_CONCURRENT_UPLOADS:
            raise FileStorageBusyError(
                "Concurrent file-storage work limit has been reached"
            )
        _ACTIVE_UPLOADS_BY_LOOP[loop] = active_uploads + 1
        try:
            return await self._upload_file_with_slot(
                file_data,
                filename,
                user_id,
                content_type,
                metadata,
            )
        finally:
            remaining = _ACTIVE_UPLOADS_BY_LOOP.get(loop, 1) - 1
            if remaining > 0:
                _ACTIVE_UPLOADS_BY_LOOP[loop] = remaining
            else:
                _ACTIVE_UPLOADS_BY_LOOP.pop(loop, None)

    async def _upload_file_with_slot(
        self,
        file_data: BinaryIO,
        filename: str,
        user_id: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileInfo:
        size = self._remaining_stream_size(file_data)
        max_file_bytes = max(1, int(self.settings.file_upload_max_bytes))
        if size > max_file_bytes:
            raise FileTooLargeError("File exceeds the configured size limit")
        # One stable identifier is shared by GridFS and the quota ledger, so
        # every retry can reconcile an ambiguous Mongo reply exactly once.
        file_id = ObjectId()
        reservation_id = str(file_id)
        reserve_task = asyncio.create_task(
            self._reserve_user_quota(user_id, size, reservation_id)
        )
        try:
            await asyncio.shield(reserve_task)
        except BaseException:
            # A quota increment may have committed even when its reply was
            # lost. Resolve the child first, then run an idempotent release for
            # this exact GridFS ID before preserving the original outcome.
            try:
                await _await_task_to_known_outcome(reserve_task)
            except BaseException:
                pass
            release_task = asyncio.create_task(
                self._release_user_quota(user_id, size, reservation_id)
            )
            try:
                await _await_task_to_known_outcome(release_task)
            except Exception as release_error:
                logger.error(
                    "Failed to release cancelled GridFS reservation: "
                    "user_id=%s error=%s",
                    user_id,
                    safe_exception_summary(release_error),
                )
            raise
        try:
            bucket = self._get_gridfs_bucket()
            
            # Prepare metadata
            file_metadata = {
                **(metadata or {}),
                'filename': filename,
                'uploadDate': datetime.utcnow(),
                'user_id': user_id,  # Store user_id in metadata
            }
            
            if content_type:
                file_metadata['contentType'] = content_type
            
            # Keep the driver operation alive to a known outcome if the caller
            # is cancelled after Mongo accepted chunks. Immediate rollback
            # must never race a late GridFS files-document commit.
            upload_task = asyncio.create_task(
                bucket.upload_from_stream_with_id(
                    file_id,
                    filename,
                    file_data,
                    metadata=file_metadata,
                )
            )
            try:
                await asyncio.shield(upload_task)
            except BaseException:
                try:
                    await _await_task_to_known_outcome(upload_task)
                except BaseException:
                    pass
                raise
            
            # Get file size (can be retrieved from GridFS if needed)
            files_collection = self._get_files_collection()
            file_info = await files_collection.find_one({"_id": file_id})
            file_size = file_info.get('length', 0) if file_info else 0
            
            logger.info(
                "File uploaded successfully: file_id=%s user_id=%s",
                file_id,
                user_id,
            )
            
            return FileInfo(
                file_id=str(file_id),
                filename=filename,
                size=file_size,
                content_type=content_type,
                upload_date=file_metadata['uploadDate'],
                metadata=file_metadata,
                user_id=user_id
            )
            
        except BaseException as error:
            async def rollback_upload() -> None:
                try:
                    await self._record_deletion_intent(
                        user_id,
                        reservation_id,
                        size,
                    )
                except Exception as intent_error:
                    # Never remove a blob unless its matching quota-release
                    # intent is durable. The active reservation remains a
                    # conservative ownership record for later reconciliation.
                    logger.error(
                        "Failed to persist GridFS rollback intent: "
                        "file_id=%s user_id=%s error=%s",
                        file_id,
                        user_id,
                        safe_exception_summary(intent_error),
                    )
                    return
                release_reservation = False
                try:
                    await self._get_gridfs_bucket().delete(file_id)
                    release_reservation = True
                except NoFile:
                    # No chunks/files document survived, so the reservation
                    # can be released just as safely as after a deletion.
                    release_reservation = True
                except Exception as rollback_error:
                    # Keep the reservation conservative when an uploaded blob
                    # could not be removed; maintenance can reconcile it
                    # without under-counting user usage.
                    logger.error(
                        "Failed to roll back partially completed GridFS "
                        "upload: file_id=%s user_id=%s error=%s",
                        file_id,
                        user_id,
                        safe_exception_summary(rollback_error),
                    )
                if release_reservation:
                    try:
                        await self._release_user_quota(
                            user_id,
                            size,
                            reservation_id,
                        )
                    except Exception as rollback_error:
                        logger.error(
                            "Failed to release GridFS upload reservation: "
                            "user_id=%s error=%s",
                            user_id,
                            safe_exception_summary(rollback_error),
                        )

            # Cancellation is a normal path for artifact deadlines, but a
            # cancelled caller must not bypass quota/blob rollback.
            rollback_task = asyncio.create_task(rollback_upload())
            rollback_deadline = (
                asyncio.get_running_loop().time()
                + self._ROLLBACK_TIMEOUT_SECONDS
            )
            slow_rollback_logged = False
            while not rollback_task.done():
                remaining = max(
                    0.0,
                    rollback_deadline
                    - asyncio.get_running_loop().time(),
                )
                if remaining <= 0 and not slow_rollback_logged:
                    slow_rollback_logged = True
                    logger.error(
                        "GridFS upload rollback exceeded its time limit and "
                        "will keep its bounded upload slot until complete: "
                        "file_id=%s user_id=%s",
                        file_id,
                        user_id,
                    )
                try:
                    if slow_rollback_logged:
                        await asyncio.shield(rollback_task)
                    else:
                        await asyncio.wait_for(
                            asyncio.shield(rollback_task),
                            timeout=remaining,
                        )
                except asyncio.CancelledError:
                    # A runner close can race the deadline cancellation.  The
                    # known-id rollback remains the authority for blob/quota
                    # cleanup, so repeated cancellation must not detach it.
                    continue
                except asyncio.TimeoutError:
                    slow_rollback_logged = True
                    logger.error(
                        "GridFS upload rollback exceeded its time limit and "
                        "will keep its bounded upload slot until complete: "
                        "file_id=%s user_id=%s",
                        file_id,
                        user_id,
                    )
            if rollback_task.done():
                # Retrieve any unexpected rollback exception before raising
                # the original upload failure.
                try:
                    rollback_task.result()
                except BaseException as rollback_error:
                    logger.error(
                        "GridFS upload rollback failed: file_id=%s user_id=%s "
                        "error=%s",
                        file_id,
                        user_id,
                        safe_exception_summary(rollback_error),
                    )
            logger.error(
                "Failed to upload file for user_id=%s: %s",
                user_id,
                safe_exception_summary(error),
            )
            raise
    
    async def download_file(self, file_id: str, user_id: Optional[str] = None) -> Tuple[BinaryIO, FileInfo]:
        """Download file by file ID"""
        try:
            bucket = self._get_gridfs_bucket()
            files_collection = self._get_files_collection()
            
            obj_id = self._to_object_id(file_id)
            
            # Get file information and check user ownership
            file_info = await files_collection.find_one({"_id": obj_id})
            if not file_info:
                raise FileNotFoundError(f"File not found with ID: {file_id}")
            
            # Check if file belongs to the user (skip check if user_id is None)
            if user_id is not None:
                file_user_id = file_info.get('metadata', {}).get('user_id')
                if file_user_id != user_id:
                    raise PermissionError(f"Access denied: file {file_id} does not belong to user {user_id}")
            stream = io.BytesIO()
            await bucket.download_to_stream(obj_id, stream)
            stream.seek(0)
            return stream, self._create_file_info(file_info, file_id)
            
        except (FileNotFoundError, PermissionError):
            raise
        except Exception as e:
            logger.error(
                "Failed to download file_id=%s for user_id=%s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            raise
    
    async def delete_file(self, file_id: str, user_id: str) -> bool:
        """Delete file"""
        try:
            bucket = self._get_gridfs_bucket()
            files_collection = self._get_files_collection()
            
            obj_id = self._to_object_id(file_id)
            
            # Check if file exists and belongs to user
            file_info = await files_collection.find_one({"_id": obj_id})
            if not file_info:
                # A previous delete may have removed GridFS before its quota
                # reply. User-quota initialization reconciles durable intents;
                # report success only when this exact tombstone is confirmed.
                await self._ensure_user_quota(user_id)
                quota = await self._get_quota_collection().find_one(
                    {"_id": user_id},
                    projection={"released_reservation_ids": 1},
                )
                return self._has_quota_operation(
                    quota,
                    "released_reservation_ids",
                    file_id,
                )
            
            # Check if file belongs to the user
            file_user_id = file_info.get('metadata', {}).get('user_id')
            if file_user_id != user_id:
                logger.warning(f"Delete access denied: file {file_id} does not belong to user {user_id}")
                return False

            await self._ensure_user_quota(user_id)

            async def delete_and_release_quota() -> bool:
                size = int(file_info.get("length", 0))
                # Persist ownership before deleting the blob. A crash or
                # transient release failure can then be repaired exactly once
                # on the next user file operation.
                intent_task = asyncio.create_task(
                    self._record_deletion_intent(user_id, file_id, size)
                )
                await _await_task_to_known_outcome(intent_task)
                delete_task = asyncio.create_task(bucket.delete(obj_id))
                try:
                    await _await_task_to_known_outcome(delete_task)
                except NoFile:
                    pass
                release_task = asyncio.create_task(
                    self._release_user_quota(
                        user_id,
                        size,
                        file_id,
                    )
                )
                await _await_task_to_known_outcome(release_task)
                return True

            mutation_task = asyncio.create_task(delete_and_release_quota())
            try:
                await asyncio.shield(mutation_task)
            except asyncio.CancelledError:
                # A request timeout or application shutdown must not create a
                # missing-blob/permanently-reserved-quota split brain. Keep
                # this request task alive until the child state machine knows
                # both mutations completed, then preserve caller cancellation.
                try:
                    await _await_task_to_known_outcome(mutation_task)
                except BaseException as mutation_error:
                    logger.error(
                        "Cancelled GridFS delete could not reach a known "
                        "quota outcome: file_id=%s user_id=%s error=%s",
                        file_id,
                        user_id,
                        safe_exception_summary(mutation_error),
                    )
                raise
            logger.info(f"File deleted successfully: {file_id} by user {user_id}")
            return True

        except Exception as e:
            logger.error(
                "Failed to delete file_id=%s for user_id=%s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            return False
    
    async def get_file_info(self, file_id: str, user_id: Optional[str] = None) -> Optional[FileInfo]:
        """Get file information"""
        try:
            files_collection = self._get_files_collection()
            
            try:
                obj_id = self._to_object_id(file_id)
            except FileNotFoundError:
                return None
            
            # Get file information and check user ownership
            file_info = await files_collection.find_one({"_id": obj_id})
            if not file_info:
                return None
            
            # Check if file belongs to the user
            file_user_id = file_info.get('metadata', {}).get('user_id')
            if user_id is not None and file_user_id != user_id:
                logger.warning(f"Access denied: file {file_id} does not belong to user {user_id}")
                return None
            
            return self._create_file_info(file_info, file_id)
            
        except Exception as e:
            logger.error(
                "Failed to get file info file_id=%s for user_id=%s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            return None

@lru_cache()
def get_file_storage() -> FileStorage:
    """Get file storage instance"""
    from app.infrastructure.storage.mongodb import get_mongodb
    return GridFSFileStorage(mongodb=get_mongodb())
