"""
Claw management API routes.
Endpoints for creating, managing, and chatting with OpenClaw instances.
"""
import logging
from fastapi import APIRouter, Depends, Header, UploadFile, File, HTTPException, status
from fastapi.responses import Response

from app.application.services.claw_service import ClawService
from app.application.services.file_service import FileService
from app.application.errors.exceptions import NotFoundError
from app.interfaces.dependencies import get_current_user, get_claw_service, get_file_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.claw import (
    ClawResponse,
    ClawHistoryResponse, ClawMessageSchema, ClawAttachmentSchema,
)
from app.interfaces.schemas.file import FileInfoResponse
from app.domain.external.file import (
    FileStorageBusyError,
    FileStorageQuotaExceededError,
    FileTooLargeError,
)
from app.domain.models.user import User
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/claw", tags=["claw"])


@router.get("", response_model=APIResponse[ClawResponse])
async def get_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[ClawResponse]:
    """Get the current user's claw instance"""
    claw = await claw_service.get_claw(current_user.id)
    if not claw:
        raise NotFoundError("No claw instance found")
    return APIResponse.success(ClawResponse.from_domain(claw))


@router.post("", response_model=APIResponse[ClawResponse])
async def create_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[ClawResponse]:
    """Create a new claw instance for the current user"""
    try:
        claw = await claw_service.create_claw(current_user.id)
    except RuntimeError as exc:
        logger.warning(
            "[claw] create request failed for user=%s: %s",
            current_user.id,
            safe_exception_summary(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Claw creation is unavailable; please retry",
        ) from exc
    return APIResponse.success(ClawResponse.from_domain(claw))


@router.delete("", response_model=APIResponse[dict])
async def delete_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[dict]:
    """Delete the current user's claw instance"""
    await claw_service.delete_claw(current_user.id)
    return APIResponse.success({})


@router.post("/upload", response_model=APIResponse[FileInfoResponse])
async def upload_claw_file(
    file: UploadFile = File(...),
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[FileInfoResponse]:
    """Upload a file from the claw workspace to Manus storage (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    max_upload_bytes = max(1, int(get_settings().claw_upload_max_bytes))
    upload_size = file.size
    try:
        file.file.seek(0, 2)
        actual_size = file.file.tell()
        file.file.seek(0)
        upload_size = max(upload_size or 0, actual_size)
    except Exception as exc:
        if upload_size is None:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Unable to verify Claw upload size",
            ) from exc
    if upload_size < 0 or upload_size > max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Claw upload exceeds the configured size limit",
        )
    try:
        result = await file_service.upload_file(
            file_data=file.file,
            filename=file.filename or "file",
            user_id=user_id,
            content_type=file.content_type,
        )
    except (FileTooLargeError, FileStorageQuotaExceededError) as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except FileStorageBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage is busy; please retry",
        ) from exc
    return APIResponse.success(await FileInfoResponse.from_domain(result))


@router.get("/files/{filename}")
async def download_claw_file(
    filename: str,
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
):
    """Proxy a file download from the user's claw workspace"""
    try:
        content, content_type = await claw_service.get_file(current_user.id, filename)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Claw file request is unavailable",
        ) from exc
    except Exception as exc:
        logger.error(
            "[claw-file] failed to proxy workspace file for user=%s: %s",
            current_user.id,
            safe_exception_summary(exc),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from claw")


@router.get("/resolve/{file_id}")
async def resolve_claw_file_meta(
    file_id: str,
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[FileInfoResponse]:
    """Get file metadata for manus-file:// resolution (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    file_info = await file_service.get_file_info(file_id, user_id)
    if not file_info:
        raise NotFoundError("File not found")
    return APIResponse.success(await FileInfoResponse.from_domain(file_info))


@router.get("/resolve/{file_id}/download")
async def resolve_claw_file_download(
    file_id: str,
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
):
    """Download file content for manus-file:// resolution (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    try:
        file_data, file_info = await file_service.download_file(file_id, user_id)
    except (FileNotFoundError, PermissionError):
        raise NotFoundError("File not found")
    import urllib.parse
    encoded_filename = urllib.parse.quote(file_info.filename, safe='')
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        file_data,
        media_type=file_info.content_type or 'application/octet-stream',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/history", response_model=APIResponse[ClawHistoryResponse])
async def get_history(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[ClawHistoryResponse]:
    """Get chat history for the current user's claw"""
    raw_messages = await claw_service.get_history(current_user.id)
    schemas = []
    for m in raw_messages:
        schema = ClawMessageSchema.from_domain(m)
        if schema.attachments:
            for att in schema.attachments:
                try:
                    att.file_url = await file_service.create_signed_url(
                        att.file_id, current_user.id
                    )
                except Exception:
                    pass
        schemas.append(schema)
    return APIResponse.success(ClawHistoryResponse(messages=schemas))
