from types import SimpleNamespace
import io

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.application.errors.exceptions import NotFoundError, UnauthorizedError
from app.application.services.agent_service import AgentService
from app.application.services.file_service import FileService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.event import MessageEvent
from app.domain.models.session import Session
from app.interfaces.api.session_routes import (
    PREVIEW_CONTENT_SECURITY_POLICY,
    _blocked_preview_ports,
    _build_preview_target_url,
    _rewrite_preview_content,
    _validate_preview_path,
    create_preview_url,
    download_shared_session_file,
    get_shared_session_files,
    proxy_preview,
    vnc_websocket,
)
from app.domain.models.file import FileInfo
from app.interfaces.schemas.session import PreviewUrlRequest
from app.interfaces.api.session_routes import router as session_router
from app.interfaces.dependencies import (
    get_agent_service,
    get_file_service,
    get_token_service,
)
from app.interfaces.errors.exception_handlers import register_exception_handlers


@pytest.fixture(autouse=True)
def _secure_test_settings(monkeypatch):
    """Keep settings deterministic regardless of the developer's root .env."""

    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("AUTH_PROVIDER", "none")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-only-jwt-secret-at-least-32-bytes-long"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _SessionRepository:
    def __init__(self, session, trace):
        self.session = session
        self.trace = trace

    async def find_by_id_and_user_id(self, session_id, user_id):
        return self.session

    async def delete(self, session_id):
        self.trace.append("delete")
        self.session = None


def _agent_service(
    session,
    trace,
    *,
    cancel_error=None,
    wait_result=True,
    destroy_result=True,
):
    class AgentRepository:
        async def find_by_id(self, agent_id):
            return None

        async def delete(self, agent_id):
            trace.append("agent-delete")

    class Task:
        async def cancel(self):
            trace.append("cancel")
            if cancel_error:
                raise cancel_error
            return True

        async def wait_for_done(self, timeout_seconds):
            trace.append("wait")
            return wait_result

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return Task()

    class Sandbox:
        async def destroy(self):
            trace.append("destroy")
            return destroy_result

    class SandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            return Sandbox()

    class Provisioner:
        async def ensure_locked(self, session):
            return await SandboxClass.get(session.sandbox_id)

        async def destroy_locked(self, session):
            sandbox = await SandboxClass.get(session.sandbox_id)
            if await sandbox.destroy() is not True:
                raise RuntimeError("sandbox provider reported failure")
            session.sandbox_id = None
            session.task_id = None

    repository = _SessionRepository(session, trace)
    service = AgentService(
        agent_repository=AgentRepository(),
        session_repository=repository,
        sandbox_cls=SandboxClass,
        task_cls=TaskClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        sandbox_provisioner=Provisioner(),
    )
    return service, repository


async def test_delete_session_cancels_and_destroys_before_record_delete():
    trace = []
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        task_id="task-1",
        sandbox_id="sandbox-1",
    )
    service, repository = _agent_service(session, trace)

    await service.delete_session("session-1", "user-1")

    assert trace == ["cancel", "wait", "destroy", "agent-delete", "delete"]
    assert repository.session is None


async def test_delete_session_is_idempotent_when_record_is_absent():
    trace = []
    service, repository = _agent_service(None, trace)

    await service.delete_session("missing", "user-1")

    assert trace == []
    assert repository.session is None


async def test_delete_session_keeps_record_when_sandbox_destroy_fails():
    trace = []
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        task_id="task-1",
        sandbox_id="sandbox-1",
    )
    service, repository = _agent_service(
        session, trace, destroy_result=False
    )

    with pytest.raises(RuntimeError, match="sandbox destruction failed"):
        await service.delete_session("session-1", "user-1")

    assert trace == ["cancel", "wait", "destroy"]
    assert repository.session is session


async def test_delete_session_does_not_destroy_sandbox_when_cancel_fails():
    trace = []
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        task_id="task-1",
        sandbox_id="sandbox-1",
    )
    service, repository = _agent_service(
        session, trace, cancel_error=RuntimeError("redis unavailable")
    )

    with pytest.raises(RuntimeError, match="task cancellation failed"):
        await service.delete_session("session-1", "user-1")

    assert trace == ["cancel"]
    assert repository.session is session


async def test_delete_session_does_not_destroy_sandbox_before_cancel_ack():
    trace = []
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        task_id="task-1",
        sandbox_id="sandbox-1",
    )
    service, repository = _agent_service(session, trace, wait_result=False)

    with pytest.raises(RuntimeError, match="task cancellation failed"):
        await service.delete_session("session-1", "user-1")

    assert trace == ["cancel", "wait"]
    assert repository.session is session


def _request(
    method: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: bytes = b"",
) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/preview",
            "raw_path": b"/preview",
            "query_string": query,
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive=receive,
    )


def test_preview_management_port_denylist_covers_all_control_planes():
    assert {
        8080,
        8222,
        9222,
        5900,
        5901,
        30150,
        30151,
        30152,
    } <= _blocked_preview_ports()


async def test_preview_url_refuses_sandbox_management_port_before_token_creation():
    class AgentServiceStub:
        async def get_session(self, session_id, user_id):
            return SimpleNamespace(is_shared=True)

    class TokenServiceStub:
        def create_resource_access_token(self, **kwargs):
            raise AssertionError("a blocked port must not receive a token")

    with pytest.raises(HTTPException) as exc:
        await create_preview_url(
            session_id="session-1",
            request_data=PreviewUrlRequest(url="http://localhost:8080/"),
            current_user=None,
            agent_service=AgentServiceStub(),
            token_service=TokenServiceStub(),
        )

    assert exc.value.status_code == 403


async def test_external_preview_url_does_not_claim_a_fake_expiry():
    class AgentServiceStub:
        async def get_session(self, session_id, user_id):
            return SimpleNamespace(is_shared=False)

    result = await create_preview_url(
        session_id="session-1",
        request_data=PreviewUrlRequest(
            url="https://example.com/demo", expire_minutes=15
        ),
        current_user=SimpleNamespace(id="user-1"),
        agent_service=AgentServiceStub(),
        token_service=SimpleNamespace(),
    )

    assert result.data.signed_url == "https://example.com/demo"
    assert result.data.expires_in == 0


async def test_preview_proxy_rechecks_management_port_from_signed_token():
    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:30150",
            "user_id": "user-1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await proxy_preview(
            request=_request("GET"),
            session_id="session-1",
            token="valid",
            port=30150,
            agent_service=SimpleNamespace(),
            token_service=token_service,
        )

    assert exc.value.status_code == 403


async def test_shared_preview_token_is_read_only():
    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "shared",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await proxy_preview(
            request=_request("POST"),
            session_id="session-1",
            token="valid",
            port=4173,
            agent_service=SimpleNamespace(),
            token_service=token_service,
        )

    assert exc.value.status_code == 405


async def test_shared_preview_token_is_invalid_after_session_is_unshared():
    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return None

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "shared",
        }
    )

    with pytest.raises(UnauthorizedError):
        await proxy_preview(
            request=_request("GET"),
            session_id="session-1",
            token="valid",
            port=4173,
            agent_service=AgentServiceStub(),
            token_service=token_service,
        )


async def test_shared_preview_token_binds_current_share_epoch():
    captured = {}

    class AgentServiceStub:
        async def get_session(self, session_id, user_id):
            assert user_id is None
            return SimpleNamespace(is_shared=True, share_epoch="epoch-one")

    class TokenServiceStub:
        def create_resource_access_token(self, **kwargs):
            captured.update(kwargs)
            return "signed-token"

    result = await create_preview_url(
        session_id="session-1",
        request_data=PreviewUrlRequest(url="http://localhost:4173/app"),
        current_user=None,
        agent_service=AgentServiceStub(),
        token_service=TokenServiceStub(),
    )

    assert captured["resource_id"] == "session-1:4173:epoch-one"
    assert captured["user_id"] == "shared"
    assert "/preview/signed-token/4173/app" in result.data.signed_url


async def test_old_shared_preview_token_stays_revoked_after_reshare():
    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return SimpleNamespace(is_shared=True, share_epoch="epoch-two")

        async def get_preview_proxy_base_url(self, session_id):
            raise AssertionError("revoked token must not reach the sandbox")

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173:epoch-one",
            "user_id": "shared",
        }
    )

    with pytest.raises(UnauthorizedError):
        await proxy_preview(
            request=_request("GET"),
            session_id="session-1",
            token="old-token",
            port=4173,
            agent_service=AgentServiceStub(),
            token_service=token_service,
        )


@pytest.mark.parametrize(
    "path",
    [
        "../file/read",
        "assets/../../file/read",
        "%2e%2e/file/read",
        "%252e%252e/file/read",
        "%2e%2e%2ffile/read",
        "%252e%252e%252ffile/read",
        r"assets\..\file\read",
    ],
)
def test_backend_preview_path_rejects_normalization_tricks(path):
    with pytest.raises(HTTPException) as exc:
        _validate_preview_path(path)

    assert exc.value.status_code == 400


def test_preview_target_stays_under_proxy_prefix_and_preserves_gateway_query():
    target = _build_preview_target_url(
        "https://gateway.example/capability?signature=gateway-secret&expires=123",
        4173,
        "assets/app.js",
        "theme=dark",
    )

    assert target.startswith(
        "https://gateway.example/capability/api/v1/proxy/4173/assets/app.js?"
    )
    assert "signature=gateway-secret" in target
    assert target.endswith("theme=dark")


async def test_preview_route_rejects_double_encoded_traversal_before_sandbox():
    class AgentServiceStub:
        async def get_preview_proxy_base_url(self, session_id):
            raise AssertionError("traversal must not reach the sandbox")

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "user-1",
        }
    )
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = lambda: AgentServiceStub()
    app.dependency_overrides[get_token_service] = lambda: token_service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/sessions/session-1/preview/valid/4173/"
            "%252e%252e/file/read"
        )

    assert response.status_code == 400


async def test_vnc_exception_never_logs_or_returns_capability_url(caplog):
    secret = "vnc-secret-capability"

    class WebSocketStub:
        async def accept(self, **kwargs):
            return None

        async def close(self, **kwargs):
            self.close_args = kwargs

    class AgentServiceStub:
        async def get_vnc_url(self, session_id):
            raise RuntimeError(
                f"failed wss://gateway.example/vnc?signature={secret}"
            )

    websocket = WebSocketStub()
    with caplog.at_level("ERROR"):
        await vnc_websocket(
            websocket=websocket,
            session_id="session-1",
            signature="verified",
            agent_service=AgentServiceStub(),
        )

    assert secret not in caplog.text
    assert "signature=" not in caplog.text
    assert secret not in websocket.close_args["reason"]
    assert "signature=" not in websocket.close_args["reason"]


async def test_preview_upstream_exception_never_logs_capability_url(
    monkeypatch, caplog
):
    secret = "preview-secret-capability"

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def build_request(self, method, url, **kwargs):
            raise httpx.ConnectError(
                f"failed {url}&leaked={secret}",
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(
        "app.interfaces.api.session_routes.httpx.AsyncClient",
        lambda **kwargs: ClientStub(),
    )

    class AgentServiceStub:
        async def get_preview_proxy_base_url(self, session_id):
            return (
                "https://gateway.example/capability"
                f"?signature={secret}&expires=123"
            )

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "user-1",
        }
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as exc:
            await proxy_preview(
                request=_request("GET"),
                session_id="session-1",
                token="valid",
                port=4173,
                agent_service=AgentServiceStub(),
                token_service=token_service,
            )

    assert exc.value.status_code == 502
    assert secret not in caplog.text
    assert "signature=" not in caplog.text


async def test_public_shared_file_response_omits_owner_and_internal_metadata():
    private_file = FileInfo(
        file_id="file-1",
        filename="report.txt",
        file_path="/home/ubuntu/upload/report.txt",
        content_type="text/plain",
        size=5,
        user_id="private-owner-id",
        metadata={"user_id": "private-owner-id", "internal": "secret"},
    )

    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return SimpleNamespace(
                files=[private_file],
                events=[
                    MessageEvent(
                        role="assistant",
                        message="report",
                        attachments=[private_file],
                    )
                ],
                share_epoch="epoch-1",
            )

    class FileServiceStub:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            return (
                f"/api/v1/sessions/{session_id}/share/files/{file_id}"
                f"?share_epoch={share_epoch}&signature=signed&expires=123"
            )

    response = await get_shared_session_files(
        session_id="session-1",
        agent_service=AgentServiceStub(),
        file_service=FileServiceStub(),
    )

    public_file = response.model_dump()["data"][0]
    assert public_file["file_id"] == "file-1"
    assert "/sessions/session-1/share/files/file-1" in public_file["file_url"]
    assert "user_id" not in public_file
    assert "metadata" not in public_file
    assert "file_path" not in public_file


async def test_orphan_session_file_is_not_listed_or_downloadable_when_shared():
    orphan_file = FileInfo(
        file_id="orphan-file",
        filename="private-draft.txt",
        content_type="text/plain",
        size=14,
        user_id="private-owner-id",
    )

    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return SimpleNamespace(
                files=[orphan_file],
                events=[],
                share_epoch="epoch-1",
            )

    class FileServiceStub:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("orphan file must not receive a public URL")

        async def download_file_by_capability(self, file_id):
            raise AssertionError("orphan file must not reach file storage")

    agent_service = AgentServiceStub()
    file_service = FileServiceStub()
    response = await get_shared_session_files(
        session_id="session-1",
        agent_service=agent_service,
        file_service=file_service,
    )

    assert response.model_dump()["data"] == []
    with pytest.raises(NotFoundError):
        await download_shared_session_file(
            session_id="session-1",
            file_id="orphan-file",
            share_epoch="epoch-1",
            signature="already-verified",
            agent_service=agent_service,
            file_service=file_service,
        )


async def test_foreign_event_attachment_is_not_listed_or_downloadable():
    foreign_file = FileInfo(
        file_id="foreign-file",
        filename="another-users-file.txt",
        content_type="text/plain",
        size=20,
        user_id="another-user",
    )

    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return SimpleNamespace(
                files=[],
                events=[
                    MessageEvent(
                        role="user",
                        message="foreign attachment",
                        attachments=[foreign_file],
                    )
                ],
                share_epoch="epoch-1",
            )

    class FileServiceStub:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("foreign file must not receive a public URL")

        async def download_file_by_capability(self, file_id):
            raise AssertionError("foreign file must not reach file storage")

    agent_service = AgentServiceStub()
    file_service = FileServiceStub()
    response = await get_shared_session_files(
        session_id="session-1",
        agent_service=agent_service,
        file_service=file_service,
    )

    assert response.model_dump()["data"] == []
    with pytest.raises(NotFoundError):
        await download_shared_session_file(
            session_id="session-1",
            file_id="foreign-file",
            share_epoch="epoch-1",
            signature="already-verified",
            agent_service=agent_service,
            file_service=file_service,
        )


async def test_shared_file_url_is_rejected_after_session_is_unshared():
    class AgentServiceStub:
        async def get_shared_session(self, session_id):
            return None

    class FileServiceStub:
        async def download_file_by_capability(self, file_id):
            raise AssertionError("revoked share must not reach file storage")

    with pytest.raises(NotFoundError):
        await download_shared_session_file(
            session_id="session-1",
            file_id="file-1",
            share_epoch="old-epoch",
            signature="already-verified",
            agent_service=AgentServiceStub(),
            file_service=FileServiceStub(),
        )


async def test_old_shared_file_url_stays_revoked_after_reshare():
    private_file = FileInfo(
        file_id="file-1",
        filename="report.txt",
        content_type="text/plain",
        size=6,
        user_id="private-owner-id",
        metadata={"user_id": "private-owner-id"},
    )

    class AgentServiceStub:
        is_shared = True
        share_epoch = "epoch-one"

        async def get_shared_session(self, session_id):
            if not self.is_shared:
                return None
            return SimpleNamespace(
                files=[private_file],
                events=[
                    MessageEvent(
                        role="assistant",
                        message="report",
                        attachments=[private_file],
                    )
                ],
                share_epoch=self.share_epoch,
            )

    class StorageStub:
        async def download_file(self, file_id, user_id):
            assert file_id == "file-1"
            assert user_id is None
            return io.BytesIO(b"report"), private_file

    token_service = TokenService()
    token_service.settings = SimpleNamespace(
        jwt_secret_key="share-test-secret-at-least-32-bytes-long",
        jwt_algorithm="HS256",
    )
    file_service = FileService(
        file_storage=StorageStub(), token_service=token_service
    )
    agent_service = AgentServiceStub()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = lambda: agent_service
    app.dependency_overrides[get_file_service] = lambda: file_service
    app.dependency_overrides[get_token_service] = lambda: token_service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get(
            "/api/v1/sessions/session-1/share/files"
        )
        assert listing.status_code == 200
        public_file = listing.json()["data"][0]
        assert "metadata" not in public_file
        assert "user_id" not in public_file
        old_url = public_file["file_url"]
        assert "share_epoch=epoch-one" in old_url
        assert (await client.get(old_url)).content == b"report"

        agent_service.is_shared = False
        assert (await client.get(old_url)).status_code == 404

        agent_service.is_shared = True
        agent_service.share_epoch = "epoch-two"
        assert (await client.get(old_url)).status_code == 404

        new_listing = await client.get(
            "/api/v1/sessions/session-1/share/files"
        )
        new_url = new_listing.json()["data"][0]["file_url"]
        assert "share_epoch=epoch-two" in new_url
        assert (await client.get(new_url)).content == b"report"


async def test_preview_proxy_does_not_bridge_credentials_or_auth_headers(
    monkeypatch,
):
    captured = {}

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def build_request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return httpx.Request(method, url, **kwargs)

        async def send(self, request, *, stream=False):
            captured["stream"] = stream

            async def chunks():
                yield b"<html>preview</html>"

            return SimpleNamespace(
                status_code=200,
                headers=httpx.Headers(
                    {
                        "Content-Type": "text/html; charset=utf-8",
                        "Set-Cookie": "session=attacker-controlled",
                        "Set-Cookie2": "legacy=attacker-controlled",
                        "Clear-Site-Data": '"cookies"',
                        "WWW-Authenticate": 'Basic realm="preview"',
                        "Proxy-Authenticate": 'Basic realm="preview"',
                        "Service-Worker-Allowed": "/",
                        "Content-Security-Policy": "sandbox allow-same-origin",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Allow-Origin": "*",
                        "Permissions-Policy": "camera=*",
                        "X-Preview-App": "must-be-stripped",
                        "ETag": '"safe-metadata"',
                        "Location": "%252e%252e/api/v1/auth/me",
                    }
                ),
                aiter_bytes=chunks,
                aclose=lambda: _async_none(),
            )

    monkeypatch.setattr(
        "app.interfaces.api.session_routes.httpx.AsyncClient",
        lambda **kwargs: ClientStub(),
    )

    class AgentServiceStub:
        async def get_preview_proxy_base_url(self, session_id):
            return "http://sandbox:8080"

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "user-1",
        }
    )
    request = _request(
        "GET",
        {
            "Authorization": "Bearer main-site-token",
            "Cookie": "main_session=secret",
            "Proxy-Authorization": "Basic secret",
            "Referer": "http://test/api/v1/sessions/session-1/preview/secret/4173/",
            "X-Client-Header": "kept",
        },
    )

    response = await proxy_preview(
        request=request,
        session_id="session-1",
        token="valid",
        port=4173,
        agent_service=AgentServiceStub(),
        token_service=token_service,
    )

    outbound_headers = {
        key.lower(): value for key, value in captured["headers"].items()
    }
    assert "authorization" not in outbound_headers
    assert "cookie" not in outbound_headers
    assert "proxy-authorization" not in outbound_headers
    assert "referer" not in outbound_headers
    assert outbound_headers["x-client-header"] == "kept"
    assert captured["stream"] is True

    response_headers = {key.lower(): value for key, value in response.headers.items()}
    assert "set-cookie" not in response_headers
    assert "set-cookie2" not in response_headers
    assert "clear-site-data" not in response_headers
    assert "www-authenticate" not in response_headers
    assert "proxy-authenticate" not in response_headers
    assert "service-worker-allowed" not in response_headers
    assert "access-control-allow-credentials" not in response_headers
    assert "permissions-policy" not in response_headers
    assert "x-preview-app" not in response_headers
    assert "location" not in response_headers
    assert response_headers["etag"] == '"safe-metadata"'
    assert response_headers["x-content-type-options"] == "nosniff"
    assert response_headers["content-security-policy"] == (
        PREVIEW_CONTENT_SECURITY_POLICY
    )
    assert "allow-same-origin" not in response_headers["content-security-policy"]
    assert response_headers["referrer-policy"] == "no-referrer"
    assert response_headers["cache-control"] == "no-store"
    assert response_headers["x-frame-options"] == "SAMEORIGIN"
    assert response_headers["access-control-allow-origin"] == "null"


async def _async_none():
    return None


async def test_preview_proxy_rejects_chunked_request_over_hard_limit(monkeypatch):
    monkeypatch.setattr(
        "app.interfaces.api.session_routes.PREVIEW_MAX_REQUEST_BYTES", 4
    )

    class AgentServiceStub:
        async def get_preview_proxy_base_url(self, session_id):
            return "http://sandbox:8080"

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "user-1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await proxy_preview(
            request=_request("POST", body=b"12345"),
            session_id="session-1",
            token="valid",
            port=4173,
            agent_service=AgentServiceStub(),
            token_service=token_service,
        )

    assert exc.value.status_code == 413


async def test_preview_proxy_rejects_streamed_response_over_hard_limit(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.interfaces.api.session_routes.PREVIEW_MAX_RESPONSE_BYTES", 4
    )

    closed = False

    class ResponseStub:
        status_code = 200
        headers = httpx.Headers({"Content-Type": "text/plain"})

        async def aiter_bytes(self):
            yield b"123"
            yield b"45"

        async def aclose(self):
            nonlocal closed
            closed = True

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url, **kwargs)

        async def send(self, request, *, stream=False):
            assert stream is True
            return ResponseStub()

    monkeypatch.setattr(
        "app.interfaces.api.session_routes.httpx.AsyncClient",
        lambda **kwargs: ClientStub(),
    )

    class AgentServiceStub:
        async def get_preview_proxy_base_url(self, session_id):
            return "http://sandbox:8080"

    token_service = SimpleNamespace(
        verify_token=lambda token: {
            "type": "resource_access",
            "resource_type": "preview",
            "resource_id": "session-1:4173",
            "user_id": "user-1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await proxy_preview(
            request=_request("GET"),
            session_id="session-1",
            token="valid",
            port=4173,
            agent_service=AgentServiceStub(),
            token_service=token_service,
        )

    assert exc.value.status_code == 413
    assert closed is True


def test_preview_rewrite_refuses_adversarial_output_expansion(monkeypatch):
    monkeypatch.setattr(
        "app.interfaces.api.session_routes.PREVIEW_MAX_RESPONSE_BYTES", 32
    )
    content = b'<img src="/"><img src="/"><img src="/">'

    rewritten = _rewrite_preview_content(
        content,
        "text/html",
        "/api/v1/sessions/session/token/4173",
    )

    assert rewritten == content
