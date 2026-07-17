import socket
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.application.errors.exceptions import BadRequestError
from app.application.services.agent_service import AgentService
from app.core.config import ConfiguredModelOption, Settings, get_settings
from app.domain.models.agent import Agent
from app.infrastructure.external.llm.factory import ConfigurableLLMFactory
from app.infrastructure.external.llm.langchain_llm import LangchainLLM
from app.infrastructure.external.llm.security import (
    LegacyModelCredentialMigrationRequired,
    ModelCredentialEncryptionError,
    PinnedModelEndpointTransport,
    ResolvedPublicModelEndpoint,
    _fernet_for_secret,
    decrypt_model_api_key,
    encrypt_model_api_key,
    validate_model_credential_encryption,
)
from app.infrastructure.models.documents import AgentDocument
from app.interfaces.api.openai_routes import (
    _anthropic_headers,
    _anthropic_stream_event_to_openai_chunks,
    _configured_llm_api_base,
    _get_llm_response,
    _safe_stream_llm_response,
    _stream_llm_response,
)
from app.interfaces.schemas.session import AgentModelConfigRequest


PUBLIC_IP = "93.184.216.34"
STRONG_SECRET = "test-only-model-credential-secret-32-bytes"


def _dns_result(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


@pytest.fixture(autouse=True)
def isolated_model_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "system-openai-key")
    monkeypatch.setenv("API_BASE", "https://system.example/v1")
    monkeypatch.setenv("MODEL_NAME", "system-model")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("JWT_SECRET_KEY", STRONG_SECRET)
    monkeypatch.setenv(
        "MODEL_CREDENTIAL_ENCRYPTION_KEYS",
        '["test-only-independent-model-key-at-least-32-bytes"]',
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(
        "app.infrastructure.external.llm.security.socket.getaddrinfo",
        lambda host, port, **kwargs: [_dns_result(PUBLIC_IP, port)],
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _AgentRepo:
    def __init__(self):
        self.saved = []
        self.deleted = []

    async def save(self, agent):
        self.saved.append(agent)

    async def delete(self, agent_id):
        self.deleted.append(agent_id)


def _service(agent_repo=None, session_repo=None):
    return AgentService(
        agent_repository=agent_repo or _AgentRepo(),
        session_repository=session_repo or SimpleNamespace(),
        sandbox_cls=SimpleNamespace(),
        task_cls=SimpleNamespace(),
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
    )


def _byok(**overrides):
    values = {
        "api_key": "sk-user",
        "api_base": "https://models.example/v1",
        "model_name": "custom-model-1",
        "model_provider": "openai",
    }
    values.update(overrides)
    return values


def test_model_schema_keeps_catalog_and_byok_modes_mutually_exclusive():
    with pytest.raises(ValidationError, match="cannot be combined"):
        AgentModelConfigRequest(model_id="gpt-4o", **_byok())


def test_model_schema_requires_a_complete_byok_tuple_and_forbids_extra_fields():
    with pytest.raises(ValidationError, match="missing"):
        AgentModelConfigRequest(api_base="https://models.example/v1")
    with pytest.raises(ValidationError, match="Extra inputs"):
        AgentModelConfigRequest.model_validate({"model_id": "gpt-4o", "admin": True})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost:8000/v1",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://127.0.0.1/v1",
        "http://[::1]/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/v1",
        "https://user:password@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#fragment",
    ],
)
async def test_byok_rejects_non_public_or_credential_bearing_urls(url):
    with pytest.raises(BadRequestError):
        await _service()._create_agent(_byok(api_base=url))


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        [PUBLIC_IP, "10.0.0.9"],
        ["::1"],
    ],
)
async def test_byok_rejects_dns_resolving_to_any_non_public_address(monkeypatch, answers):
    monkeypatch.setattr(
        "app.infrastructure.external.llm.security.socket.getaddrinfo",
        lambda host, port, **kwargs: [_dns_result(ip, port) for ip in answers],
    )

    with pytest.raises(BadRequestError, match="non-public"):
        await _service()._create_agent(_byok())


async def test_numeric_hostname_that_resolves_to_loopback_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.external.llm.security.socket.getaddrinfo",
        lambda host, port, **kwargs: [_dns_result("127.0.0.1", port)],
    )

    with pytest.raises(BadRequestError, match="non-public"):
        await _service()._create_agent(_byok(api_base="http://2130706433/v1"))


async def test_system_owned_local_api_base_is_not_subject_to_byok_ssrf_policy(monkeypatch):
    monkeypatch.setenv("API_BASE", "http://127.0.0.1:8190/v1")
    monkeypatch.setattr(
        "app.infrastructure.external.llm.security.socket.getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("system API_BASE must not be DNS validated")
        ),
    )
    get_settings.cache_clear()

    agent = await _service()._create_agent(None)

    assert agent.api_base == "http://127.0.0.1:8190/v1"
    assert agent.api_key is None
    assert agent.is_byok is False


async def test_application_rejects_mixed_mode_even_when_schema_is_bypassed():
    with pytest.raises(BadRequestError, match="cannot be combined"):
        await _service()._create_agent({"model_id": "gpt-4o", **_byok()})


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"api_key": None}, "missing"),
        ({"model_provider": "not-installed"}, "Unsupported"),
        ({"model_name": "bad model name"}, "Invalid model_name"),
    ],
)
async def test_application_validates_byok_key_provider_and_model_name(overrides, message):
    config = _byok(**overrides)
    with pytest.raises(BadRequestError, match=message):
        await _service()._create_agent(config)


async def test_weak_default_deployment_secret_refuses_new_byok(monkeypatch):
    # Keep this test independent from a developer's root .env. Disposable
    # auth-none mode may start with the legacy JWT default, but creating new
    # BYOK state must still fail at the application boundary.
    monkeypatch.setenv("AUTH_PROVIDER", "none")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv("JWT_SECRET_KEY", "your-secret-key-here")
    monkeypatch.delenv("MODEL_CREDENTIAL_ENCRYPTION_KEYS", raising=False)
    get_settings.cache_clear()

    with pytest.raises(BadRequestError, match="JWT_SECRET_KEY"):
        await _service()._create_agent(_byok())


async def test_default_and_catalog_server_keys_are_never_copied_to_agent():
    default_agent = await _service()._create_agent(None)
    catalog_agent = await _service()._create_agent(SimpleNamespace(model_id="gpt-4o"))

    assert default_agent.api_key is None
    assert catalog_agent.api_key is None
    assert catalog_agent.model_id == "gpt-4o"
    assert catalog_agent.is_byok is False


def test_byok_factory_uses_only_custom_key_and_never_global_headers(monkeypatch):
    captured = {}

    class Gateway:
        def __init__(self, settings):
            captured["settings"] = settings

    monkeypatch.setattr("app.infrastructure.external.llm.factory.OpenAILLM", Gateway)
    monkeypatch.setattr(
        "app.infrastructure.external.llm.factory.resolve_public_model_endpoint",
        lambda url: ResolvedPublicModelEndpoint(
            url=url,
            hostname="private.example",
            addresses=(PUBLIC_IP,),
        ),
    )
    settings = Settings(
        _env_file=None,
        api_key="global-key",
        api_base="https://global.example/v1",
        model_name="global-model",
        model_provider="openai",
        llm_provider="openai",
        extra_headers={"Authorization": "Bearer global", "X-Global": "secret"},
    )
    agent = Agent(
        model_name="private-model",
        model_provider="openai",
        api_base="https://private.example/v1",
        api_key="private-key",
        is_byok=True,
    )

    ConfigurableLLMFactory(settings).create(agent)

    resolved = captured["settings"]
    assert resolved.api_key == "private-key"
    assert resolved.api_base == "https://private.example/v1"
    assert resolved.extra_headers is None
    assert resolved.byok_pinned_ip == PUBLIC_IP


async def test_pinned_transport_connects_to_validated_ip_with_original_host_and_sni():
    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.request = None

        async def handle_async_request(self, request):
            self.request = request
            return httpx.Response(200, json={"ok": True}, request=request)

    recording = RecordingTransport()
    transport = PinnedModelEndpointTransport(
        "https://models.example:8443/v1",
        PUBLIC_IP,
        inner=recording,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(
            "https://models.example:8443/v1/models"
        )

    assert response.status_code == 200
    assert recording.request.url.host == PUBLIC_IP
    assert recording.request.headers["host"] == "models.example:8443"
    assert recording.request.extensions["sni_hostname"] == "models.example"


async def test_pinned_transport_refuses_cross_host_redirect_target():
    transport = PinnedModelEndpointTransport(
        "https://models.example/v1", PUBLIC_IP
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="another host"):
            await client.get("https://metadata.google.internal/latest")


def test_catalog_factory_resolves_server_key_without_persisting_it(monkeypatch):
    captured = {}

    class Gateway:
        def __init__(self, settings):
            captured["settings"] = settings

    monkeypatch.setattr("app.infrastructure.external.llm.factory.LangchainLLM", Gateway)
    settings = Settings(
        _env_file=None,
        api_key="global-openai-key",
        api_base="https://global.example/v1",
        model_name="global-model",
        model_provider="openai",
        llm_provider="openai",
        available_models=[
            ConfiguredModelOption(
                id="private-catalog",
                label="Private catalog",
                model_name="catalog-model",
                model_provider="anthropic",
                api_base="https://catalog.example/v1",
                api_key="catalog-server-key",
            )
        ],
    )
    agent = Agent(
        model_id="private-catalog",
        model_name="catalog-model",
        model_provider="anthropic",
        api_base="https://catalog.example/v1",
        api_key=None,
    )

    ConfigurableLLMFactory(settings).create(agent)

    assert captured["settings"].api_key == "catalog-server-key"
    assert agent.api_key is None


def test_agent_document_encrypts_byok_and_round_trips_without_plaintext(monkeypatch):
    monkeypatch.setattr(
        AgentDocument, "get_pymongo_collection", classmethod(lambda cls: None)
    )
    agent = Agent(
        id="agent-secure",
        model_name="private-model",
        model_provider="openai",
        api_base="https://models.example/v1",
        api_key="super-secret-user-key",
        is_byok=True,
    )

    document = AgentDocument.from_domain(agent)

    assert document.api_key is None
    assert document.api_key_encrypted
    assert "super-secret-user-key" not in document.model_dump_json()
    restored = document.to_domain()
    assert restored.api_key == "super-secret-user-key"
    assert restored.is_byok is True


def test_agent_document_detects_authenticated_ciphertext_tampering(monkeypatch):
    monkeypatch.setattr(
        AgentDocument, "get_pymongo_collection", classmethod(lambda cls: None)
    )
    document = AgentDocument.from_domain(
        Agent(
            model_name="private-model",
            model_provider="openai",
            api_base="https://models.example/v1",
            api_key="super-secret-user-key",
            is_byok=True,
        )
    )
    token = document.api_key_encrypted
    document.api_key_encrypted = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(ModelCredentialEncryptionError, match="could not be decrypted"):
        document.to_domain()


def test_independent_model_keyring_survives_jwt_rotation():
    model_key = "independent-model-key-at-least-32-bytes-long"
    before = Settings(
        _env_file=None,
        api_key="system-key",
        jwt_secret_key="old-jwt-signing-secret-at-least-32-bytes",
        deployment_environment="production",
        model_credential_encryption_keys=f'["{model_key}"]',
    )
    after = before.model_copy(
        update={"jwt_secret_key": "new-jwt-signing-secret-at-least-32-bytes"}
    )

    encrypted = encrypt_model_api_key("user-byok-secret", before)

    assert encrypted.startswith("v2$")
    assert decrypt_model_api_key(encrypted, after) == "user-byok-secret"


def test_model_keyring_rotation_reads_old_and_writes_new_key():
    old_key = "old-model-encryption-key-at-least-32-bytes"
    new_key = "new-model-encryption-key-at-least-32-bytes"
    common = {
        "_env_file": None,
        "api_key": "system-key",
        "jwt_secret_key": "jwt-signing-secret-at-least-32-bytes-long",
        "deployment_environment": "production",
    }
    old_settings = Settings(
        **common,
        model_credential_encryption_keys=f'["{old_key}"]',
    )
    rotating = Settings(
        **common,
        model_credential_encryption_keys=f'["{new_key}", "{old_key}"]',
    )
    new_only = Settings(
        **common,
        model_credential_encryption_keys=f'["{new_key}"]',
    )

    old_ciphertext = encrypt_model_api_key("old-record", old_settings)
    assert decrypt_model_api_key(old_ciphertext, rotating) == "old-record"
    new_ciphertext = encrypt_model_api_key("new-record", rotating)
    assert decrypt_model_api_key(new_ciphertext, new_only) == "new-record"
    with pytest.raises(ModelCredentialEncryptionError, match="could not be decrypted"):
        decrypt_model_api_key(old_ciphertext, new_only)


def test_legacy_jwt_derived_ciphertext_can_be_reencrypted_into_keyring():
    jwt_secret = "legacy-jwt-root-secret-at-least-32-bytes"
    settings = Settings(
        _env_file=None,
        api_key="system-key",
        jwt_secret_key=jwt_secret,
        deployment_environment="production",
        model_credential_encryption_keys=(
            '["independent-new-model-key-at-least-32-bytes"]'
        ),
    )
    legacy_ciphertext = _fernet_for_secret(jwt_secret)[1].encrypt(
        b"legacy-record"
    ).decode("ascii")

    plaintext = decrypt_model_api_key(legacy_ciphertext, settings)
    reencrypted = encrypt_model_api_key(plaintext, settings)

    assert plaintext == "legacy-record"
    assert reencrypted.startswith("v2$")
    assert decrypt_model_api_key(reencrypted, settings) == "legacy-record"


def test_production_byok_requires_independent_model_keyring():
    settings = Settings(
        _env_file=None,
        api_key="system-key",
        jwt_secret_key="jwt-signing-secret-at-least-32-bytes-long",
        deployment_environment="production",
        model_credential_encryption_keys=None,
    )

    with pytest.raises(
        ModelCredentialEncryptionError,
        match="MODEL_CREDENTIAL_ENCRYPTION_KEYS",
    ):
        validate_model_credential_encryption(settings)


def test_legacy_system_agent_uses_rotated_current_deployment_config(monkeypatch):
    """Old copied system keys must not silently become permanent BYOK keys."""

    monkeypatch.setattr(
        AgentDocument, "get_pymongo_collection", classmethod(lambda cls: None)
    )
    document = AgentDocument(
        agent_id="legacy-system-agent",
        model_name="retired-system-model",
        model_provider="openai",
        api_base="https://retired-system.example/v1",
        api_key="retired-deployment-key",
        api_key_encrypted=None,
        is_byok=False,
        temperature=0.0,
        max_tokens=1024,
    )

    restored = document.to_domain()

    assert restored.api_key is None
    assert restored.is_byok is False


def test_legacy_system_agent_cannot_send_current_key_to_retired_endpoint(monkeypatch):
    captured = {}

    class Gateway:
        def __init__(self, settings):
            captured["settings"] = settings

    monkeypatch.setattr("app.infrastructure.external.llm.factory.OpenAILLM", Gateway)
    settings = Settings(
        _env_file=None,
        api_key="current-system-key",
        api_base="https://current-system.example/v1",
        model_name="current-system-model",
        model_provider="openai",
        llm_provider="openai",
    )
    restored_legacy = Agent(
        model_name="retired-model",
        model_provider="anthropic",
        api_base="https://retired-or-reassigned.example/v1",
        api_key=None,
        is_byok=False,
    )

    ConfigurableLLMFactory(settings).create(restored_legacy)

    resolved = captured["settings"]
    assert resolved.api_key == "current-system-key"
    assert resolved.api_base == "https://current-system.example/v1"
    assert resolved.model_name == "current-system-model"
    assert resolved.model_provider == "openai"


def test_legacy_plaintext_byok_is_preserved_only_with_explicit_marker(monkeypatch):
    monkeypatch.setattr(
        AgentDocument, "get_pymongo_collection", classmethod(lambda cls: None)
    )
    document = AgentDocument(
        agent_id="legacy-byok-agent",
        model_name="private-model",
        model_provider="openai",
        api_base="https://models.example/v1",
        api_key="legacy-user-key",
        api_key_encrypted=None,
        is_byok=True,
        temperature=0.0,
        max_tokens=1024,
    )

    restored = document.to_domain()

    assert restored.api_key == "legacy-user-key"
    assert restored.is_byok is True


@pytest.mark.parametrize(
    "legacy_key",
    ["old-copied-system-key", "real-user-byok-key"],
)
def test_unmarked_legacy_plaintext_credentials_fail_closed_until_migrated(
    monkeypatch, legacy_key
):
    monkeypatch.setattr(
        AgentDocument, "get_pymongo_collection", classmethod(lambda cls: None)
    )
    document = AgentDocument.model_validate(
        {
            "agent_id": "ambiguous-legacy-agent",
            "model_name": "legacy-model",
            "model_provider": "openai",
            "api_base": "https://legacy.example/v1",
            "api_key": legacy_key,
            # Real custom-branch documents predate this field entirely.
            "temperature": 0.0,
            "max_tokens": 1024,
        }
    )

    assert document.is_byok is None
    with pytest.raises(LegacyModelCredentialMigrationRequired, match="migrate"):
        document.to_domain()


async def test_create_session_rolls_back_agent_when_session_write_fails():
    repo = _AgentRepo()

    class FailingSessionRepo:
        async def save(self, session):
            raise RuntimeError("mongo write failed")

    service = _service(repo, FailingSessionRepo())

    with pytest.raises(RuntimeError, match="mongo write failed"):
        await service.create_session("user-1")
    assert len(repo.saved) == 1
    assert repo.deleted == [repo.saved[0].id]


async def test_delete_session_restores_agent_if_session_delete_fails():
    trace = []
    agent = Agent(id="agent-1", model_name="system-model", model_provider="openai")
    session = SimpleNamespace(
        id="session-1",
        user_id="user-1",
        agent_id=agent.id,
        task_id=None,
        sandbox_id=None,
    )

    class AgentRepo:
        async def find_by_id(self, agent_id):
            return agent

        async def delete(self, agent_id):
            trace.append("agent-delete")

        async def save(self, restored):
            assert restored is agent
            trace.append("agent-restore")

    class SessionRepo:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return session

        async def delete(self, session_id):
            trace.append("session-delete")
            raise RuntimeError("session delete failed")

    service = _service(AgentRepo(), SessionRepo())

    with pytest.raises(RuntimeError, match="session delete failed"):
        await service.delete_session("session-1", "user-1")
    assert trace == ["agent-delete", "session-delete", "agent-restore"]


def test_anthropic_defaults_use_anthropic_key_base_and_protect_required_headers():
    settings = Settings(
        _env_file=None,
        api_key=None,
        anthropic_api_key="anthropic-key",
        api_base=None,
        model_name="claude-sonnet-4-6",
        model_provider="anthropic",
        jwt_secret_key="test-only-jwt-root-secret-at-least-32-bytes",
        extra_headers={"x-api-key": "wrong", "X-Custom": "ok"},
    )
    settings.validate()

    assert _configured_llm_api_base(settings) == "https://api.anthropic.com"
    headers = _anthropic_headers(settings)
    assert headers["x-api-key"] == "anthropic-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["X-Custom"] == "ok"


def test_langchain_anthropic_uses_provider_key_and_official_base(monkeypatch):
    captured = {}
    fake_model = SimpleNamespace()
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.init_chat_model",
        lambda **kwargs: captured.update(kwargs) or fake_model,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.RetryWithErrorOutputParser.from_llm",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.RobustJsonParser.from_llm",
        lambda model: SimpleNamespace(),
    )
    settings = Settings(
        _env_file=None,
        api_key=None,
        anthropic_api_key="anthropic-key",
        api_base=None,
        model_name="claude-sonnet-4-6",
        model_provider="anthropic",
    )

    LangchainLLM(settings)

    assert captured["model_provider"] == "anthropic"
    assert captured["api_key"] == "anthropic-key"
    assert captured["base_url"] is None


async def test_anthropic_stream_and_nonstream_use_the_same_target_and_key(monkeypatch):
    calls = []

    class FakeResponse:
        is_success = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            yield 'data: {"type":"message_stop"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            calls.append(("stream", url, kwargs["headers"]))
            return FakeResponse()

        async def post(self, url, **kwargs):
            calls.append(("nonstream", url, kwargs["headers"]))
            return FakeResponse()

    monkeypatch.setattr(
        "app.interfaces.api.openai_routes.httpx.AsyncClient", FakeClient
    )
    settings = Settings(
        _env_file=None,
        api_key=None,
        anthropic_api_key="anthropic-key",
        api_base=None,
        model_name="claude-sonnet-4-6",
        model_provider="anthropic",
    )
    body = {"model": "claude-sonnet-4-6", "messages": [], "stream": True}

    streamed = [chunk async for chunk in _stream_llm_response(body, settings)]
    nonstreamed = await _get_llm_response({**body, "stream": False}, settings)

    assert streamed[-1] == b"data: [DONE]\n\n"
    assert nonstreamed["choices"][0]["message"]["content"] == "ok"
    assert [call[1] for call in calls] == [
        "https://api.anthropic.com/v1/messages",
        "https://api.anthropic.com/v1/messages",
    ]
    assert [call[2]["x-api-key"] for call in calls] == [
        "anthropic-key",
        "anthropic-key",
    ]


def test_anthropic_stream_error_is_redacted_then_done():
    chunks = _anthropic_stream_event_to_openai_chunks(
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "try later"},
        },
        {},
    )

    assert chunks[0] == {
        "error": {
            "type": "api_error",
            "message": "LLM backend streaming request failed",
        }
    }
    assert "try later" not in str(chunks[0])
    assert chunks[1] == "[DONE]"


async def test_stream_transport_exception_becomes_explicit_sse_error(monkeypatch):
    async def broken_stream(*args, **kwargs):
        if False:
            yield b""
        raise RuntimeError("connection reset")

    monkeypatch.setattr(
        "app.interfaces.api.openai_routes._stream_llm_response", broken_stream
    )
    chunks = [
        chunk.decode("utf-8")
        async for chunk in _safe_stream_llm_response({}, SimpleNamespace())
    ]

    assert '"error"' in chunks[0]
    assert "LLM backend streaming request failed" in chunks[0]
    assert "connection reset" not in chunks[0]
    assert chunks[1] == "data: [DONE]\n\n"
