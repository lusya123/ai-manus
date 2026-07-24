from typing import Dict, Any, Optional, BinaryIO, Tuple
import logging
import urllib.parse
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.application.services.token_service import TokenService
from app.domain.utils.error_reporting import safe_exception_summary

# Set up logger
logger = logging.getLogger(__name__)

class FileService:
    def __init__(self, file_storage: Optional[FileStorage] = None, token_service: Optional[TokenService] = None):
        self._file_storage = file_storage
        self._token_service = token_service

    async def upload_file(self, file_data: BinaryIO, filename: str, user_id: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> FileInfo:
        """Upload file"""
        logger.info("Upload file request: user_id=%s", user_id)
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.upload_file(file_data, filename, user_id, content_type, metadata)
            logger.info(f"File uploaded successfully: file_id={result.file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(
                "Failed to upload file for user %s: %s",
                user_id,
                safe_exception_summary(e),
            )
            raise
    
    async def download_file(self, file_id: str, user_id: str) -> Tuple[BinaryIO, FileInfo]:
        """Download a file after an explicit owner check."""
        logger.info(f"Download file request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.download_file(file_id, user_id)
            logger.info(f"File downloaded successfully: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(
                "Failed to download file %s for user %s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            raise

    async def download_file_by_capability(self, file_id: str) -> Tuple[BinaryIO, FileInfo]:
        """Download after a route has verified a resource-bound signed URL.

        Keeping the unchecked operation explicitly named prevents an omitted
        user id from accidentally becoming an authorization bypass.
        """
        if not self._file_storage:
            raise RuntimeError("File storage service not available")
        return await self._file_storage.download_file(file_id, None)

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        """Delete file"""
        logger.info(f"Delete file request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.delete_file(file_id, user_id)
            if result:
                logger.info(f"File deleted successfully: file_id={file_id}, user_id={user_id}")
            else:
                logger.warning(f"File deletion failed or file not found: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(
                "Failed to delete file %s for user %s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            raise

    async def get_file_info(self, file_id: str, user_id: str) -> Optional[FileInfo]:
        """Get file information after an explicit owner check."""
        logger.info(f"Get file info request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.get_file_info(file_id, user_id)
            if result:
                logger.info(f"File info retrieved successfully: file_id={file_id}, user_id={user_id}")
            else:
                logger.warning(f"File not found or access denied: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(
                "Failed to get file info %s for user %s: %s",
                file_id,
                user_id,
                safe_exception_summary(e),
            )
            raise

    async def get_file_info_by_capability(self, file_id: str) -> Optional[FileInfo]:
        """Read metadata for an already-authorized internal capability."""
        if not self._file_storage:
            raise RuntimeError("File storage service not available")
        return await self._file_storage.get_file_info(file_id, None)
    
    async def enrich_with_file_url(self, file_info: FileInfo) -> FileInfo:
        """Enrich file information with file URL"""
        logger.info(
            "Enrich file info request: file_id=%s user_id=%s",
            file_info.file_id,
            file_info.user_id,
        )
        
        try:
            signed_url = await self.create_signed_url(file_info.file_id, file_info.user_id)
            file_info.file_url = signed_url
            return file_info
        except Exception as e:
            logger.error(
                "Failed to enrich file info %s with file URL: %s",
                file_info.file_id,
                safe_exception_summary(e),
            )
            raise

    async def create_signed_url(self, file_id: str, user_id: str, expire_minutes: int = 30) -> str:
        """Create signed URL for file download"""
        logger.info(f"Create signed URL request: file_id={file_id}, user_id={user_id}, expire_minutes={expire_minutes}")
        
        if not self._token_service:
            logger.error("Token service not available")
            raise RuntimeError("Token service not available")
        
        # Validate expiration time (max 15 minutes)
        if expire_minutes > 30:
            expire_minutes = 30
        
        # Check if file exists and user has access
        file_info = await self.get_file_info(file_id, user_id)
        if not file_info:
            logger.warning(f"File not found or access denied for signed URL: file_id={file_id}, user_id={user_id}")
            raise FileNotFoundError("File not found")
        
        # Create signed URL for file download
        base_url = f"/api/v1/files/{file_id}"
        signed_url = self._token_service.create_signed_url(
            base_url=base_url,
            expire_minutes=expire_minutes
        )
        
        logger.info(f"Created signed URL for file download for user {user_id}, file {file_id}")
        
        return signed_url

    async def create_internal_signed_url(self, file_id: str, expire_minutes: int = 30) -> str:
        """Create a URL for a file ID produced by trusted backend code."""
        file_info = await self.get_file_info_by_capability(file_id)
        if not file_info:
            raise FileNotFoundError("File not found")
        return self._token_service.create_signed_url(
            base_url=f"/api/v1/files/{file_id}",
            expire_minutes=min(expire_minutes, 30),
        )

    async def create_shared_session_signed_url(
        self,
        session_id: str,
        file_id: str,
        share_epoch: str,
        expire_minutes: int = 30,
    ) -> str:
        """Create a share-scoped URL whose route rechecks live share state."""

        if not self._token_service:
            raise RuntimeError("Token service not available")
        return self._token_service.create_signed_url(
            base_url=(
                f"/api/v1/sessions/{session_id}/share/files/{file_id}"
                f"?share_epoch={urllib.parse.quote(share_epoch, safe='')}"
            ),
            expire_minutes=min(expire_minutes, 30),
        )
