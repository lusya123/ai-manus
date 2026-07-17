"""
Unit tests for the AgentBay sandbox provider.

These tests mock the AgentBay SDK objects — no network or credentials needed.
"""
import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.infrastructure.external.sandbox.agentbay_sandbox as agentbay_module
from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox, _get_agent_bay
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.domain.external.sandbox import (
    SandboxProvisioningError,
    SandboxUnavailableError,
)


AGENTBAY_ENV = {
    "SANDBOX_PROVIDER": "agentbay",
    "AGENTBAY_API_KEY": "akm-test-key",
    "AGENTBAY_IMAGE_ID": "img-test-image",
    "AGENTBAY_DEPLOYMENT_ID": "test-deployment",
}


@pytest.fixture(autouse=True)
def agentbay_settings(monkeypatch):
    """Point settings at the AgentBay provider and reset cached singletons."""
    for key, value in AGENTBAY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("API_KEY", "test-llm-key")

    from app.core.config import get_settings
    get_settings.cache_clear()

    agentbay_module._agent_bay_client = None
    yield
    get_settings.cache_clear()
    agentbay_module._agent_bay_client = None


def _link_result(url, success=True, error=None):
    result = MagicMock()
    result.success = success
    result.data = url
    result.error_message = error
    return result


def _fake_session(session_id="session-abc123"):
    session = MagicMock()
    session.session_id = session_id
    session.get_link = AsyncMock(
        side_effect=lambda protocol, port: _link_result(f"{protocol}://gw.example/{port}")
    )
    delete_result = MagicMock()
    delete_result.success = True
    session.delete = AsyncMock(return_value=delete_result)
    return session


def _fake_agent_bay(session=None, create_success=True, get_success=True):
    agent_bay = MagicMock()

    create_result = MagicMock()
    create_result.success = create_success
    create_result.session = session if create_success else None
    create_result.error_message = None if create_success else "quota exceeded"
    agent_bay.create = AsyncMock(return_value=create_result)

    get_result = MagicMock()
    get_result.success = get_success
    get_result.session = session if get_success else None
    get_result.error_message = None if get_success else "not found"
    get_result.code = "" if get_success else "InvalidMcpSession.NotFound"
    agent_bay.get = AsyncMock(return_value=get_result)

    return agent_bay


class TestConstruction:
    def test_is_a_sandbox_via_docker_base(self):
        assert issubclass(AgentBaySandbox, DockerSandbox)

    def test_urls_and_id(self):
        sb = AgentBaySandbox(
            _fake_session("session-xyz"),
            base_url="https://gw.example/api/",
            cdp_url="wss://gw.example/cdp",
            vnc_url="wss://gw.example/vnc",
        )
        assert sb.id == "session-xyz"
        assert sb.base_url == "https://gw.example/api"  # trailing slash stripped
        assert sb.cdp_url == "wss://gw.example/cdp"
        assert sb.vnc_url == "wss://gw.example/vnc"
        # No Docker container is managed
        assert sb._managed_container is False
        assert sb._container_name is None

    def test_inherits_full_api_surface(self):
        sb = AgentBaySandbox(_fake_session(), "https://gw", "wss://gw", "wss://gw")
        for method in (
            "ensure_sandbox", "exec_command", "view_shell", "wait_for_process",
            "write_to_process", "kill_process", "file_write", "file_read",
            "file_exists", "file_delete", "file_list", "file_replace",
            "file_search", "file_find", "file_upload", "file_download",
            "get_browser", "destroy",
        ):
            assert callable(getattr(sb, method)), method


class TestClientInit:
    def test_client_requires_api_key(self, monkeypatch):
        monkeypatch.setenv("AGENTBAY_API_KEY", "")
        from app.core.config import get_settings
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="AGENTBAY_API_KEY"):
            _get_agent_bay()

    def test_client_singleton(self):
        client1 = _get_agent_bay()
        client2 = _get_agent_bay()
        assert client1 is client2
        assert client1.api_key == "akm-test-key"

    def test_client_disables_sdk_console_and_file_logging(self):
        agentbay_module._agent_bay_client = None
        with patch("agentbay._common.logger.AgentBayLogger.setup") as setup:
            _get_agent_bay()

        setup.assert_called_once_with(
            level="WARNING",
            enable_console=False,
            enable_file=False,
        )


@pytest.mark.asyncio
class TestCreate:
    async def test_allocate_returns_provider_handle_before_link_resolution(self):
        session = _fake_session("session-owned-before-links")
        agent_bay = _fake_agent_bay(session)
        labels = {
            "manus-deployment": "deployment-hash",
            "manus-operation": "operation-hash",
        }

        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            allocated = await AgentBaySandbox.allocate(labels=labels)

        assert allocated is session
        session.get_link.assert_not_awaited()
        assert agent_bay.create.call_args.args[0].labels == labels

    async def test_create_builds_sandbox_with_gateway_links(self):
        session = _fake_session()
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            sb = await AgentBaySandbox.create()

        assert sb.id == "session-abc123"
        assert sb.base_url == "https://gw.example/30150"
        assert sb.cdp_url == "wss://gw.example/30151"
        assert sb.vnc_url == "wss://gw.example/30152"

        # Custom image + TTL passed through
        params = agent_bay.create.call_args.args[0]
        assert params.image_id == "img-test-image"
        assert params.idle_release_timeout == 30 * 60  # SANDBOX_TTL_MINUTES default

    async def test_create_failure_raises(self):
        agent_bay = _fake_agent_bay(create_success=False)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxUnavailableError, match="creation failed"):
                await AgentBaySandbox.create()

    async def test_create_requires_image_id(self, monkeypatch):
        monkeypatch.setenv("AGENTBAY_IMAGE_ID", "")
        from app.core.config import get_settings
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="AGENTBAY_IMAGE_ID"):
            await AgentBaySandbox.create()

    async def test_create_rolls_back_session_when_links_fail(self):
        """A session we cannot reach must be deleted, not leaked (billing!)."""
        session = _fake_session()
        session.get_link = AsyncMock(
            return_value=_link_result(None, success=False, error="gateway error")
        )
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxUnavailableError, match="unavailable"):
                await AgentBaySandbox.create()
        session.delete.assert_awaited_once()

    async def test_create_rollback_failure_does_not_mask_original_error(self):
        session = _fake_session()
        session.get_link = AsyncMock(
            return_value=_link_result(None, success=False, error="gateway error")
        )
        session.delete = AsyncMock(side_effect=Exception("delete failed"))
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxProvisioningError) as exc:
                await AgentBaySandbox.create()
        assert exc.value.sandbox_id == session.session_id

    async def test_create_rollback_false_preserves_provider_id_in_error(self):
        session = _fake_session("session-needs-cleanup")
        session.get_link = AsyncMock(
            return_value=_link_result(None, success=False, error="gateway error")
        )
        delete_result = MagicMock(success=False, error_message="busy")
        session.delete = AsyncMock(return_value=delete_result)
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxProvisioningError) as exc:
                await AgentBaySandbox.create()

        assert exc.value.sandbox_id == "session-needs-cleanup"


@pytest.mark.asyncio
class TestGet:
    async def test_get_reconnects_by_session_id(self):
        session = _fake_session("session-restored")
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            sb = await AgentBaySandbox.get("session-restored")

        assert sb is not None
        assert sb.id == "session-restored"
        agent_bay.get.assert_awaited_once_with("session-restored")

    async def test_get_returns_fresh_live_handle_each_time(self):
        session = _fake_session("session-restored")
        agent_bay = _fake_agent_bay(session)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            first = await AgentBaySandbox.get("session-restored")
            second = await AgentBaySandbox.get("session-restored")

        assert first is not second
        assert agent_bay.get.await_count == 2
        await first.client.aclose()
        await second.client.aclose()

    async def test_get_missing_session_returns_none(self):
        agent_bay = _fake_agent_bay(get_success=False)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            sb = await AgentBaySandbox.get("session-gone")
        assert sb is None

    async def test_get_exception_is_inconclusive_not_missing(self):
        agent_bay = MagicMock()
        agent_bay.get = AsyncMock(side_effect=Exception("network down"))
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxUnavailableError):
                await AgentBaySandbox.get("session-err")

    async def test_get_provider_failure_is_inconclusive_not_missing(self):
        result = MagicMock(
            success=False,
            session=None,
            code="ServiceUnavailable",
            error_message="temporary outage",
        )
        agent_bay = MagicMock()
        agent_bay.get = AsyncMock(return_value=result)
        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxUnavailableError):
                await AgentBaySandbox.get("session-err")

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            ("", "session not found"),
            ("Service.NotFound", "not found"),
            ("InvalidMcpSession.NotFoundTemporary", "not found"),
            ("ServiceUnavailable", "backend not found during routing"),
        ],
    )
    async def test_get_only_accepts_exact_provider_not_found_code(
        self, code, message
    ):
        result = MagicMock(
            success=False,
            session=None,
            code=code,
            error_message=message,
        )
        agent_bay = MagicMock()
        agent_bay.get = AsyncMock(return_value=result)

        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            with pytest.raises(SandboxUnavailableError):
                await AgentBaySandbox.get("session-still-possibly-live")

    async def test_list_provider_sessions_reads_every_page(self):
        first = MagicMock(
            success=True,
            session_ids=[{"sessionId": "provider-1"}],
            next_token="next",
        )
        second = MagicMock(
            success=True,
            session_ids=[{"sessionId": "provider-2"}, "provider-1"],
            next_token="",
        )
        agent_bay = MagicMock()
        agent_bay.list = AsyncMock(side_effect=[first, second])

        with patch.object(agentbay_module, "_get_agent_bay", return_value=agent_bay):
            result = await AgentBaySandbox.list_provider_session_ids(
                {"manus-operation": "stable-operation"}
            )

        assert result == ["provider-1", "provider-2"]
        assert [call.kwargs["page"] for call in agent_bay.list.await_args_list] == [
            1,
            2,
        ]


@pytest.mark.asyncio
class TestDestroy:
    async def test_destroy_deletes_session(self):
        session = _fake_session()
        sb = AgentBaySandbox(session, "https://gw", "wss://gw", "wss://gw")
        assert await sb.destroy() is True
        session.delete.assert_awaited_once()

    async def test_destroy_returns_false_on_failure(self):
        session = _fake_session()
        session.delete = AsyncMock(side_effect=Exception("api error"))
        sb = AgentBaySandbox(session, "https://gw", "wss://gw", "wss://gw")
        assert await sb.destroy() is False

    async def test_destroy_never_touches_docker(self):
        """destroy() must not fall through to Docker container removal."""
        session = _fake_session()
        sb = AgentBaySandbox(session, "https://gw", "wss://gw", "wss://gw")
        with patch("app.infrastructure.external.sandbox.docker_sandbox.docker") as mock_docker:
            await sb.destroy()
            mock_docker.from_env.assert_not_called()


@pytest.mark.asyncio
class TestHttpMethodsUseGatewayUrl:
    async def test_exec_command_posts_to_gateway(self):
        session = _fake_session()
        sb = AgentBaySandbox(session, "https://gw.example/api", "wss://gw", "wss://gw")

        response = MagicMock()
        response.json.return_value = {"success": True, "message": "ok", "data": {}}
        sb.client.post = AsyncMock(return_value=response)

        result = await sb.exec_command("sess-1", "/tmp", "echo hi")
        assert result.success is True
        url = sb.client.post.call_args.args[0]
        assert url == "https://gw.example/api/api/v1/shell/exec"

    async def test_signed_gateway_query_is_preserved_after_appended_api_path(self):
        session = _fake_session()
        sb = AgentBaySandbox(
            session,
            "https://gw.example/session/path?signature=signed-value&expires=123",
            "wss://gw",
            "wss://gw",
        )

        response = MagicMock()
        response.json.return_value = {"success": True, "message": "ok", "data": {}}
        sb.client.post = AsyncMock(return_value=response)

        result = await sb.exec_command("sess-1", "/tmp", "echo hi")

        assert result.success is True
        assert sb.client.post.call_args.args[0] == (
            "https://gw.example/session/path/api/v1/shell/exec"
            "?signature=signed-value&expires=123"
        )

    async def test_file_download_gets_from_gateway(self):
        session = _fake_session()
        sb = AgentBaySandbox(session, "https://gw.example/api", "wss://gw", "wss://gw")

        response = MagicMock()
        response.content = b"file-bytes"
        response.raise_for_status = MagicMock()
        sb.client.get = AsyncMock(return_value=response)

        stream = await sb.file_download("/tmp/x.txt")
        assert isinstance(stream, io.BytesIO)
        assert stream.read() == b"file-bytes"


class TestProviderSwitch:
    def test_dependencies_select_agentbay(self):
        from app.core.config import get_settings
        settings = get_settings()
        assert settings.sandbox_provider == "agentbay"
        # Mirror the selection logic in dependencies.get_agent_service
        if settings.sandbox_provider == "agentbay":
            selected = AgentBaySandbox
        else:
            selected = DockerSandbox
        assert selected is AgentBaySandbox

    def test_default_provider_is_docker(self, monkeypatch):
        monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
        from app.core.config import get_settings
        get_settings.cache_clear()
        assert get_settings().sandbox_provider == "docker"

    def test_unknown_provider_fails_fast(self, monkeypatch):
        """A typo in SANDBOX_PROVIDER must raise, not silently use Docker."""
        monkeypatch.setenv("SANDBOX_PROVIDER", "agentbay-typo")
        from app.core.config import get_settings
        get_settings.cache_clear()
        from app.interfaces import dependencies
        dependencies.get_agent_service.cache_clear()
        with pytest.raises(ValueError, match="Unknown SANDBOX_PROVIDER"):
            dependencies.get_agent_service()
        dependencies.get_agent_service.cache_clear()
