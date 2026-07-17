from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException

from app.domain.models.event import (
    BrowserToolContent,
    MessageEvent,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.file import FileInfo
from app.domain.models.session import Session
from app.interfaces.api.session_routes import router as session_router
from app.interfaces.dependencies import (
    get_agent_service,
    get_current_user,
    get_file_service,
    get_optional_current_user,
)


def _unauthorized():
    raise HTTPException(status_code=401, detail="Authentication required")


async def test_legacy_session_files_route_requires_auth_even_when_shared():
    class AgentService:
        async def is_session_shared(self, session_id):
            return True

        async def get_session_files(self, session_id, user_id):
            raise AssertionError("anonymous request must not reach session files")

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = _unauthorized
    app.dependency_overrides[get_optional_current_user] = lambda: None
    app.dependency_overrides[get_agent_service] = AgentService

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/sessions/session-1/files")

    assert response.status_code == 401


async def test_shared_session_route_never_emits_generic_file_capability():
    attachment = FileInfo(
        file_id="file-1",
        filename="report.txt",
        file_path="/home/ubuntu/report.txt",
        user_id="private-owner",
        metadata={"internal": "secret"},
    )
    session = Session(
        id="session-1",
        user_id="private-owner",
        agent_id="agent-1",
        is_shared=True,
        share_epoch="epoch-1",
        files=[attachment],
        events=[
            MessageEvent(
                role="assistant",
                message="report",
                attachments=[attachment],
            )
        ],
    )

    class AgentService:
        async def get_shared_session(self, session_id):
            return session

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            return (
                f"/api/v1/sessions/{session_id}/share/files/{file_id}"
                f"?share_epoch={share_epoch}&signature=public"
            )

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = AgentService
    app.dependency_overrides[get_file_service] = FileService

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/sessions/shared/session-1"
        )

    assert response.status_code == 200
    public_attachment = response.json()["data"]["events"][0]["data"][
        "attachments"
    ][0]
    assert "/sessions/session-1/share/files/file-1" in public_attachment[
        "file_url"
    ]
    assert "/api/v1/files/file-1" not in public_attachment["file_url"]
    assert "metadata" not in public_attachment
    assert "user_id" not in public_attachment
    assert "file_path" not in public_attachment


async def test_canonical_browser_screenshot_is_shared_from_public_event():
    screenshot = FileInfo(
        file_id="shot-1",
        filename="screenshot.png",
        user_id="private-owner",
        content_type="image/png",
    )
    session = Session(
        id="session-1",
        user_id="private-owner",
        agent_id="agent-1",
        is_shared=True,
        share_epoch="epoch-1",
        files=[screenshot],
        events=[
            ToolEvent(
                tool_call_id="browser-1",
                tool_name="browser",
                function_name="browser_navigate",
                function_args={},
                status=ToolStatus.CALLED,
                tool_content=BrowserToolContent(screenshot="shot-1"),
            )
        ],
    )

    class AgentService:
        async def get_shared_session(self, session_id):
            return session

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            return f"/shared/{session_id}/{file_id}/{share_epoch}"

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = AgentService
    app.dependency_overrides[get_file_service] = FileService

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/sessions/shared/session-1")

    assert response.status_code == 200
    assert response.json()["data"]["events"][0]["data"]["content"] == {
        "screenshot": "/shared/session-1/shot-1/epoch-1"
    }


async def test_forged_browser_screenshot_without_canonical_file_is_not_shared():
    session = Session(
        id="session-1",
        user_id="private-owner",
        agent_id="agent-1",
        is_shared=True,
        share_epoch="epoch-1",
        files=[],
        events=[
            ToolEvent(
                tool_call_id="browser-1",
                tool_name="browser",
                function_name="browser_navigate",
                function_args={},
                status=ToolStatus.CALLED,
                tool_content=BrowserToolContent(screenshot="foreign-shot"),
            )
        ],
    )

    class AgentService:
        async def get_shared_session(self, session_id):
            return session

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("forged screenshot must not receive a public URL")

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = AgentService
    app.dependency_overrides[get_file_service] = FileService

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/sessions/shared/session-1")

    assert response.status_code == 200
    assert response.json()["data"]["events"][0]["data"].get("content") is None


async def test_shared_session_route_redacts_every_tool_argument():
    secrets = {
        "file": "shared-file-body-secret",
        "browser": "shared-browser-text-secret",
        "shell": "shared-shell-input-secret",
        "token": "shared-api-token-secret",
    }
    session = Session(
        id="session-1",
        user_id="private-owner",
        agent_id="agent-1",
        is_shared=True,
        share_epoch="epoch-1",
        events=[
            ToolEvent(
                tool_call_id="file-1",
                tool_name="file",
                function_name="file_write",
                function_args={"file": "/tmp/report", "content": secrets["file"]},
                status=ToolStatus.CALLING,
            ),
            ToolEvent(
                tool_call_id="browser-1",
                tool_name="browser",
                function_name="browser_input",
                function_args={"text": secrets["browser"], "password": "password"},
                status=ToolStatus.CALLING,
            ),
            ToolEvent(
                tool_call_id="shell-1",
                tool_name="shell",
                function_name="shell_write_to_process",
                function_args={"id": "terminal", "input": secrets["shell"]},
                status=ToolStatus.CALLED,
            ),
            ToolEvent(
                tool_call_id="mcp-1",
                tool_name="mcp",
                function_name="mcp_remote_call",
                function_args={"api_token": secrets["token"]},
                status=ToolStatus.CALLED,
            ),
        ],
    )

    class AgentService:
        async def get_shared_session(self, session_id):
            return session

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("this fixture has no shared files")

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = AgentService
    app.dependency_overrides[get_file_service] = FileService

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/sessions/shared/session-1")

    assert response.status_code == 200
    public_events = response.json()["data"]["events"]
    assert len(public_events) == 4
    assert all(event["data"]["args"] == {} for event in public_events)
    for secret in (*secrets.values(), "password"):
        assert secret not in response.text
