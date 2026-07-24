import io
import logging
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from uvicorn.logging import AccessFormatter
from fastapi import FastAPI, Request

from app.application.services.email_service import EmailService
from app.application.services.token_service import TokenService
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.message import ToolCall
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.mcp import MCPClientManager
from app.infrastructure.external.cache.redis_cache import RedisCache
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage
from app.infrastructure.external.message_queue.redis_stream_queue import (
    RedisStreamQueue,
)
from app.infrastructure.external.search.custom_search import CustomSearchEngine
from app.infrastructure.logging import (
    CapabilityRedactionFilter,
    UvicornAccessLogCapabilityRedactionFilter,
    setup_logging,
)
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.interfaces.errors.exception_handlers import register_exception_handlers


def _access_record(request_target: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", request_target, "1.1", 200),
        exc_info=None,
    )


def test_uvicorn_access_log_redacts_capability_query_string():
    record = _access_record(
        "/api/v1/files/abc?expires=123&signature=super-secret&token=also-secret"
    )

    assert UvicornAccessLogCapabilityRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    assert "/api/v1/files/abc" in rendered
    assert "expires=" not in rendered
    assert "signature=" not in rendered
    assert "super-secret" not in rendered


def test_uvicorn_access_log_keeps_path_without_query_unchanged():
    record = _access_record("/api/v1/health")

    UvicornAccessLogCapabilityRedactionFilter().filter(record)

    assert record.args[2] == "/api/v1/health"


def test_generic_redaction_preserves_uvicorn_access_formatter_contract():
    preview_token = "eyJhbGciOiJIUzI1NiJ9.secret.signature"
    record = _access_record(
        f"/api/v1/sessions/session-1/preview/{preview_token}/8765/"
        "?signature=query-secret"
    )

    UvicornAccessLogCapabilityRedactionFilter().filter(record)
    CapabilityRedactionFilter().filter(record)
    rendered = AccessFormatter("%(message)s").format(record)

    assert len(record.args) == 5
    assert preview_token not in rendered
    assert "query-secret" not in rendered
    assert "/preview/<redacted>/8765/" in rendered


def test_setup_logging_installs_access_filter_once(monkeypatch):
    access_logger = logging.getLogger("uvicorn.access")
    root_logger = logging.getLogger()
    original_filters = list(access_logger.filters)
    original_handlers = list(root_logger.handlers)
    access_logger.filters.clear()
    monkeypatch.setattr(
        "app.infrastructure.logging.get_settings",
        lambda: type("Settings", (), {"log_level": "INFO"})(),
    )

    try:
        setup_logging()
        setup_logging()
        installed = [
            log_filter
            for log_filter in access_logger.filters
            if isinstance(log_filter, UvicornAccessLogCapabilityRedactionFilter)
        ]
        assert len(installed) == 1
    finally:
        access_logger.filters[:] = original_filters
        root_logger.handlers[:] = original_handlers


def test_root_capability_filter_redacts_args_bearer_tokens_and_exceptions():
    signed_url = "wss://gateway.example/cdp?token=query-secret&signature=secret"
    bearer = "Bearer standalone-secret"
    try:
        raise RuntimeError(
            "connection failed "
            "https://gateway.example/error?signature=exception-secret"
        )
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="cdp_use.client",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="gateway=%s authorization=%s token=standalone-token",
        args=(signed_url, bearer),
        exc_info=exc_info,
    )

    assert CapabilityRedactionFilter().filter(record) is True

    rendered = logging.Formatter("%(message)s").format(record)
    assert "wss://gateway.example/cdp?<redacted>" in rendered
    assert "query-secret" not in rendered
    assert "standalone-secret" not in rendered
    assert "standalone-token" not in rendered
    assert "exception-secret" not in rendered
    assert "authorization=<redacted>" in rendered
    assert "token=<redacted>" in rendered


def test_setup_logging_suppresses_and_redacts_browser_gateway_logs(
    monkeypatch,
):
    root_logger = logging.getLogger()
    logger_names = (
        "cdp_use",
        "cdp_use.client",
        "browser_use",
        "websockets.client",
    )
    dependency_loggers = {
        name: logging.getLogger(name) for name in logger_names
    }
    original_handlers = list(root_logger.handlers)
    original_root_filters = list(root_logger.filters)
    original_levels = {
        name: logger.level for name, logger in dependency_loggers.items()
    }
    original_filters = {
        name: list(logger.filters)
        for name, logger in dependency_loggers.items()
    }
    stream = io.StringIO()
    root_logger.handlers.clear()
    root_logger.filters.clear()
    monkeypatch.setattr("app.infrastructure.logging.sys.stdout", stream)
    monkeypatch.setattr(
        "app.infrastructure.logging.get_settings",
        lambda: type("Settings", (), {"log_level": "INFO"})(),
    )

    try:
        setup_logging()
        for logger in dependency_loggers.values():
            assert logger.level == logging.WARNING

        cdp_logger = dependency_loggers["cdp_use.client"]
        cdp_logger.info(
            "Connecting to wss://gateway.example/cdp?token=info-secret"
        )
        cdp_logger.warning(
            "Failed wss://gateway.example/cdp?token=warning-secret"
        )
        for handler in root_logger.handlers:
            handler.flush()

        rendered = stream.getvalue()
        assert "info-secret" not in rendered
        assert "warning-secret" not in rendered
        assert "wss://gateway.example/cdp?<redacted>" in rendered
        assert all(
            any(
                isinstance(log_filter, CapabilityRedactionFilter)
                for log_filter in handler.filters
            )
            for handler in root_logger.handlers
        )
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.filters[:] = original_root_filters
        for name, logger in dependency_loggers.items():
            logger.setLevel(original_levels[name])
            logger.filters[:] = original_filters[name]


def test_browser_use_adapter_disables_library_logging_before_import():
    env = dict(os.environ)
    # Prove the adapter overrides an unsafe caller value before browser-use's
    # import-time setup can attach its own handlers.
    env["BROWSER_USE_SETUP_LOGGING"] = "true"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import logging, os; "
                "import app.infrastructure.external.browser.browser_use_browser; "
                "print(os.environ['BROWSER_USE_SETUP_LOGGING'], "
                "len(logging.getLogger('browser_use').handlers))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip().splitlines()[-1] == "false 0"


def test_uvicorn_access_log_redacts_preview_token_in_path():
    preview_token = "eyJhbGciOiJIUzI1NiJ9.secret.signature"
    record = _access_record(
        f"/api/v1/sessions/session-1/preview/{preview_token}/8765/assets/app.js"
        "?cache=123"
    )

    UvicornAccessLogCapabilityRedactionFilter().filter(record)

    rendered = record.getMessage()
    assert preview_token not in rendered
    assert "<redacted>/8765/assets/app.js" in rendered
    assert "cache=123" not in rendered


def test_uvicorn_access_log_redacts_truncated_preview_token_path():
    preview_token = "eyJhbGciOiJIUzI1NiJ9.truncated.signature"
    record = _access_record(
        f"/api/v1/sessions/session-1/preview/{preview_token}?bad=1"
    )

    UvicornAccessLogCapabilityRedactionFilter().filter(record)

    rendered = record.getMessage()
    assert preview_token not in rendered
    assert "/preview/<redacted>" in rendered
    assert "bad=1" not in rendered


def test_uvicorn_websocket_log_redacts_signed_query_on_error_logger():
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=(
            "127.0.0.1:12345",
            "/api/v1/sessions/session-1/vnc?signature=websocket-secret&expires=123",
        ),
        exc_info=None,
    )

    UvicornAccessLogCapabilityRedactionFilter().filter(record)

    rendered = record.getMessage()
    assert "/api/v1/sessions/session-1/vnc" in rendered
    assert "websocket-secret" not in rendered
    assert "signature=" not in rendered


def test_signed_url_verification_never_logs_capability_query(caplog):
    token_service = TokenService()
    signed_url = token_service.create_signed_url("/api/v1/files/secret-file")
    signature = signed_url.split("signature=", 1)[1].split("&", 1)[0]

    with caplog.at_level(logging.INFO):
        assert token_service.verify_signed_url(signed_url) is True

    rendered_logs = caplog.text
    assert "/api/v1/files/secret-file" in rendered_logs
    assert signature not in rendered_logs
    assert "signature=" not in rendered_logs


def test_signed_url_creation_redacts_existing_query_and_preview_token(caplog):
    service = TokenService()
    preview_token = "eyJhbGciOiJIUzI1NiJ9.preview-secret.signature"
    share_secret = "share-epoch-secret"
    base_url = (
        f"/api/v1/sessions/session-1/preview/{preview_token}/8000/"
        f"?share_epoch={share_secret}"
    )

    with caplog.at_level(logging.DEBUG):
        service.create_signed_url(base_url)

    assert preview_token not in caplog.text
    assert share_secret not in caplog.text
    assert "signature=" not in caplog.text
    assert "/preview/<redacted>/8000/" in caplog.text


def test_setup_logging_suppresses_http_client_request_urls(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_levels = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore")
    }
    monkeypatch.setattr(
        "app.infrastructure.logging.get_settings",
        lambda: type("Settings", (), {"log_level": "INFO"})(),
    )

    try:
        setup_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        root_logger.handlers[:] = original_handlers
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)


async def test_sandbox_http_error_never_logs_signed_gateway_url(
    caplog, monkeypatch
):
    secret = "agentbay-bearer-signature"
    request = httpx.Request(
        "GET", f"https://gateway.example/session?signature={secret}"
    )
    response = httpx.Response(503, request=request)
    error = httpx.HTTPStatusError(
        "upstream failed", request=request, response=response
    )
    sandbox = object.__new__(DockerSandbox)
    sandbox.base_url = f"https://gateway.example/session?signature={secret}"
    sandbox.client = type(
        "Client", (), {"get": AsyncMock(side_effect=error)}
    )()
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.asyncio.sleep",
        AsyncMock(),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception, match="failed to become ready"):
            await sandbox.ensure_sandbox()

    assert secret not in caplog.text
    assert "signature=" not in caplog.text
    assert "HTTPStatusError (HTTP 503)" in caplog.text


def test_invalid_jwt_log_uses_exception_type_only(caplog, monkeypatch):
    secret = "eyJhbGciOiJIUzI1NiJ9.secret.signature"
    service = TokenService()

    def reject_token(*_args, **_kwargs):
        raise jwt.InvalidTokenError(
            f"invalid token {secret}?signature=also-secret"
        )

    monkeypatch.setattr(
        "app.application.services.token_service.jwt.decode", reject_token
    )

    with caplog.at_level(logging.WARNING):
        assert service.verify_token(secret) is None

    assert secret not in caplog.text
    assert "also-secret" not in caplog.text
    assert "InvalidTokenError" in caplog.text


async def test_redis_cache_error_never_logs_key_or_exception_message(caplog):
    secret_key = "verification_code:person@example.test"
    secret_error = "redis://user:password@cache/?token=secret"
    cache = object.__new__(RedisCache)
    cache.redis_client = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError(secret_error))
    )

    with caplog.at_level(logging.ERROR):
        assert await cache.get(secret_key) is None

    assert secret_key not in caplog.text
    assert secret_error not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_redis_stream_logging_never_serializes_message_payload(caplog):
    secret = "user prompt with api_key=payload-secret"
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "session-events"
    queue._redis = SimpleNamespace(
        client=SimpleNamespace(eval=AsyncMock(return_value="1-0"))
    )

    with caplog.at_level(logging.DEBUG):
        assert await queue.put({"prompt": secret}) == "1-0"

    assert secret not in caplog.text
    assert "payload-secret" not in caplog.text
    assert "session-events" in caplog.text


async def test_tool_failure_log_never_serializes_args_or_exception_message(
    caplog,
):
    secret = "tool-argument-api-key"
    agent = object.__new__(BaseAgent)
    agent._agent_id = "agent-safe-id"
    agent._tool_call_timeout_seconds = 1
    agent.max_retries = 0
    agent.retry_interval = 0
    tool = SimpleNamespace(
        name="safe_operation",
        invoke=AsyncMock(side_effect=RuntimeError(f"provider body: {secret}")),
    )
    call = ToolCall(
        id="call-safe-id",
        name="safe_operation",
        args={"api_key": secret, "prompt": "private prompt"},
    )

    with caplog.at_level(logging.ERROR):
        await agent.invoke_tool(tool, call)

    assert secret not in caplog.text
    assert "private prompt" not in caplog.text
    assert "provider body" not in caplog.text
    assert "safe_operation" in caplog.text
    assert "RuntimeError" in caplog.text


async def test_mcp_initialization_log_never_serializes_config(caplog):
    secret = "mcp-bearer-secret"
    config = MCPConfig(
        mcpServers={
            "safe-server": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                url=f"https://mcp.example.test/?token={secret}",
                headers={"Authorization": f"Bearer {secret}"},
            )
        }
    )
    manager = MCPClientManager(config)
    manager._connect_servers = AsyncMock(
        side_effect=RuntimeError(f"connection rejected: {secret}")
    )

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        await manager.initialize()

    assert secret not in caplog.text
    assert "Authorization" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_custom_search_error_never_logs_url_query_or_provider_body(
    caplog, monkeypatch
):
    api_key = "custom-search-api-key"
    prompt = "private search prompt"
    provider_body = "provider response body secret"
    request = httpx.Request(
        "GET",
        f"https://search.example.test/?api_key={api_key}&q={prompt}",
    )
    response = httpx.Response(503, request=request, text=provider_body)
    error = httpx.HTTPStatusError(
        f"search failed: {provider_body}", request=request, response=response
    )
    client = SimpleNamespace(
        __aenter__=AsyncMock(),
        __aexit__=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=error),
    )

    class ClientContext:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        "app.infrastructure.external.search.custom_search.httpx.AsyncClient",
        lambda **_kwargs: ClientContext(),
    )
    engine = CustomSearchEngine(
        api_url="https://search.example.test/",
        api_key=api_key,
        api_key_param="api_key",
        method="GET",
    )

    with caplog.at_level(logging.ERROR):
        result = await engine.search(prompt)

    assert result.success is False
    assert api_key not in caplog.text
    assert prompt not in caplog.text
    assert provider_body not in caplog.text
    assert "HTTPStatusError (HTTP 503)" in caplog.text


async def test_gridfs_error_log_keeps_ids_but_not_attachment_metadata(caplog):
    filename = "private-attachment-name.txt"
    secret = "mongodb://user:password@host/private"
    storage = object.__new__(GridFSFileStorage)
    storage.settings = SimpleNamespace(file_upload_max_bytes=1024)
    storage._get_gridfs_bucket = lambda: (_ for _ in ()).throw(
        RuntimeError(secret)
    )
    storage._reserve_user_quota = AsyncMock()
    storage._release_user_quota = AsyncMock()

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await storage.upload_file(
            file_data=io.BytesIO(b"attachment"),
            filename=filename,
            user_id="user-safe-id",
            content_type="text/plain",
            metadata={"description": "private attachment content"},
        )

    assert filename not in caplog.text
    assert secret not in caplog.text
    assert "private attachment content" not in caplog.text
    assert "user-safe-id" in caplog.text
    assert "RuntimeError" in caplog.text


async def test_email_logs_never_include_recipient_code_or_message(caplog):
    email = "private.person@example.test"
    code = "654321"
    service = object.__new__(EmailService)
    service.settings = SimpleNamespace(
        email_host="smtp.example.test",
        email_port=465,
        email_username="mailer",
        email_password="smtp-secret",
        email_from="mailer@example.test",
    )
    service.cache = SimpleNamespace(get=AsyncMock(return_value=None))
    service._generate_verification_code = lambda: code
    service._send_smtp_email = AsyncMock()
    service._store_verification_code = AsyncMock()

    with caplog.at_level(logging.DEBUG):
        await service.send_verification_code(email)

    assert email not in caplog.text
    assert code not in caplog.text
    assert "smtp-secret" not in caplog.text


async def test_global_exception_log_never_includes_exception_message(caplog):
    secret = "signed-url?signature=global-handler-secret"
    app = FastAPI()
    register_exception_handlers(app)
    handler = app.exception_handlers[Exception]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1234),
        }
    )

    with caplog.at_level(logging.ERROR):
        response = await handler(request, RuntimeError(secret))

    assert response.status_code == 500
    assert secret not in caplog.text
    assert "global-handler-secret" not in response.body.decode()
    assert "RuntimeError" in caplog.text
