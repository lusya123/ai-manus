"""Security helpers for user-supplied LLM endpoints and credentials.

System-owned endpoints are intentionally not validated here: local deployments
commonly point ``API_BASE`` at a loopback or container-network address.  These
checks are for untrusted per-session (BYOK) configuration only.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import is_secure_jwt_secret


_LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")
_FERNET_CONTEXT = b"ai-manus:model-credential:v1\0"


class ModelEndpointValidationError(ValueError):
    """Raised when an untrusted model endpoint is not a public HTTP(S) URL."""


class ModelCredentialEncryptionError(RuntimeError):
    """Raised when a model credential cannot be safely encrypted/decrypted."""


class LegacyModelCredentialMigrationRequired(RuntimeError):
    """A legacy plaintext key cannot be classified without operator input."""


@dataclass(frozen=True)
class ResolvedPublicModelEndpoint:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def resolve_public_model_endpoint(url: str) -> ResolvedPublicModelEndpoint:
    """Validate and normalize a BYOK endpoint.

    Every address returned by DNS must be globally routable.  Requiring all
    answers to be public prevents a hostname with mixed public/private answers
    from being accepted.  Callers should run this again when constructing a
    client so a persisted hostname is not trusted forever.
    """

    if not isinstance(url, str):
        raise ModelEndpointValidationError("Model endpoint must be a URL")
    normalized = url.strip()
    if not normalized or len(normalized) > 2048:
        raise ModelEndpointValidationError("Model endpoint is empty or too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ModelEndpointValidationError("Model endpoint contains control characters")
    if "\\" in normalized:
        raise ModelEndpointValidationError("Model endpoint contains an invalid backslash")

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ModelEndpointValidationError("Model endpoint is malformed") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ModelEndpointValidationError("Model endpoint must use http or https")
    if not parsed.hostname:
        raise ModelEndpointValidationError("Model endpoint must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ModelEndpointValidationError("Model endpoint must not contain user info")
    if parsed.query:
        raise ModelEndpointValidationError("Model endpoint must not contain a query string")
    if parsed.fragment:
        raise ModelEndpointValidationError("Model endpoint must not contain a fragment")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _LOCAL_HOSTNAMES or hostname.endswith(_LOCAL_SUFFIXES):
        raise ModelEndpointValidationError("Model endpoint must use a public hostname")

    try:
        literal_ip = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise ModelEndpointValidationError("Model endpoint must use a public IP address")
        return ResolvedPublicModelEndpoint(
            url=normalized,
            hostname=hostname,
            addresses=(str(literal_ip),),
        )

    try:
        results = socket.getaddrinfo(
            hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, OSError) as exc:
        raise ModelEndpointValidationError("Model endpoint hostname could not be resolved") from exc

    addresses = {result[4][0] for result in results if result[4]}
    if not addresses:
        raise ModelEndpointValidationError("Model endpoint hostname returned no addresses")
    if any(not _is_public_ip(address) for address in addresses):
        raise ModelEndpointValidationError(
            "Model endpoint hostname resolves to a non-public address"
        )
    return ResolvedPublicModelEndpoint(
        url=normalized,
        hostname=hostname,
        addresses=tuple(sorted(addresses)),
    )


def validate_public_model_endpoint(url: str) -> str:
    """Backwards-compatible URL-only wrapper around endpoint resolution."""

    return resolve_public_model_endpoint(url).url


class PinnedModelEndpointTransport(httpx.AsyncBaseTransport):
    """Connect to one validated IP while preserving HTTP Host and TLS SNI."""

    def __init__(
        self,
        endpoint_url: str,
        pinned_ip: str,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
    ):
        endpoint = urlsplit(endpoint_url)
        hostname = (endpoint.hostname or "").rstrip(".").lower()
        if not hostname or not _is_public_ip(pinned_ip):
            raise ModelEndpointValidationError(
                "Pinned model endpoint must use a validated public IP"
            )
        self._hostname = hostname
        self._pinned_ip = pinned_ip
        self._port = endpoint.port
        self._scheme = endpoint.scheme.lower()
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_host = request.url.host.rstrip(".").lower()
        if request_host != self._hostname:
            raise httpx.UnsupportedProtocol(
                "Pinned BYOK client refused a request to another host"
            )

        default_port = 443 if self._scheme == "https" else 80
        authority_host = (
            f"[{self._hostname}]" if ":" in self._hostname else self._hostname
        )
        authority = (
            authority_host
            if self._port in (None, default_port)
            else f"{authority_host}:{self._port}"
        )
        headers = request.headers.copy()
        headers["host"] = authority
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self._hostname
        pinned_request = httpx.Request(
            request.method,
            request.url.copy_with(host=self._pinned_ip),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def create_pinned_model_http_client(
    endpoint_url: str,
    pinned_ip: str,
    *,
    timeout: float = 120.0,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PinnedModelEndpointTransport(endpoint_url, pinned_ip),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


def _fernet_for_secret(secret: str) -> tuple[str, Fernet]:
    raw_key = hashlib.sha256(_FERNET_CONTEXT + secret.encode("utf-8")).digest()
    key_id = hashlib.sha256(b"kid\0" + raw_key).hexdigest()[:16]
    return key_id, Fernet(base64.urlsafe_b64encode(raw_key))


def _configured_credential_secrets(settings: Any) -> tuple[str, ...]:
    raw = getattr(settings, "model_credential_encryption_keys", None)
    if raw in (None, "", []):
        return ()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelCredentialEncryptionError(
                "MODEL_CREDENTIAL_ENCRYPTION_KEYS must be a JSON array"
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, list) or not parsed or not all(
        isinstance(secret, str) and is_secure_jwt_secret(secret)
        for secret in parsed
    ):
        raise ModelCredentialEncryptionError(
            "MODEL_CREDENTIAL_ENCRYPTION_KEYS must be a non-empty JSON array "
            "of distinct non-default secrets of at least 32 bytes"
        )
    if len(set(parsed)) != len(parsed):
        raise ModelCredentialEncryptionError(
            "MODEL_CREDENTIAL_ENCRYPTION_KEYS must not contain duplicates"
        )
    return tuple(parsed)


def _credential_keyring(settings: Any) -> tuple[str, dict[str, Fernet], Fernet]:
    """Return active key id, all keyed ciphers, and the legacy JWT cipher."""

    configured = _configured_credential_secrets(settings)
    environment = str(
        getattr(settings, "deployment_environment", "") or ""
    ).strip().lower()
    if not configured and environment not in {"development", "local", "test"}:
        raise ModelCredentialEncryptionError(
            "BYOK credentials require MODEL_CREDENTIAL_ENCRYPTION_KEYS outside "
            "local/test/development environments"
        )

    jwt_secret = str(getattr(settings, "jwt_secret_key", "") or "")
    if not is_secure_jwt_secret(jwt_secret):
        raise ModelCredentialEncryptionError(
            "BYOK credential migration requires a non-default JWT_SECRET_KEY "
            "of at least 32 bytes"
        )
    _legacy_id, legacy_fernet = _fernet_for_secret(jwt_secret)

    entries = [_fernet_for_secret(secret) for secret in configured]
    if not entries:
        entries = [(_legacy_id, legacy_fernet)]
    keyring = dict(entries)
    # Keep the current JWT-derived cipher readable for pre-keyring records, but
    # never make it active when an independent key is configured.
    keyring.setdefault(_legacy_id, legacy_fernet)
    return entries[0][0], keyring, legacy_fernet


def encrypt_model_api_key(api_key: str, settings: Any) -> str:
    if not api_key:
        raise ModelCredentialEncryptionError("Cannot encrypt an empty model API key")
    active_id, keyring, _legacy = _credential_keyring(settings)
    token = keyring[active_id].encrypt(api_key.encode("utf-8")).decode("ascii")
    return f"v2${active_id}${token}"


def validate_model_credential_encryption(settings: Any) -> None:
    """Fail fast before accepting a BYOK credential that cannot be encrypted."""

    _credential_keyring(settings)


def decrypt_model_api_key(ciphertext: str, settings: Any) -> str:
    _active_id, keyring, legacy_fernet = _credential_keyring(settings)
    try:
        if ciphertext.startswith("v2$"):
            _version, key_id, token = ciphertext.split("$", 2)
            fernet = keyring.get(key_id)
            if fernet is None:
                raise InvalidToken
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")

        # Bare Fernet values were written before the independent keyring.
        return legacy_fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ModelCredentialEncryptionError(
            "Stored model credential could not be decrypted; verify the model "
            "credential keyring and complete key rotation before removing keys"
        ) from exc


def provider_api_key(settings: Any, provider: str, explicit: str | None = None) -> str | None:
    """Resolve a system-owned provider key without crossing provider boundaries."""

    if explicit:
        return explicit
    normalized_provider = (provider or "").lower()
    configured_provider = (getattr(settings, "model_provider", "") or "").lower()
    if normalized_provider == "anthropic":
        anthropic_key = getattr(settings, "anthropic_api_key", None)
        if anthropic_key:
            return anthropic_key
    if normalized_provider == configured_provider:
        return getattr(settings, "api_key", None)
    return None


def provider_api_base(settings: Any, provider: str, explicit: str | None = None) -> str | None:
    """Resolve a system-owned base URL without reusing another provider's URL."""

    if explicit:
        return explicit
    normalized_provider = (provider or "").lower()
    configured_provider = (getattr(settings, "model_provider", "") or "").lower()
    if normalized_provider == configured_provider:
        return getattr(settings, "api_base", None)
    # ``None`` lets each official SDK integration select its own canonical URL.
    return None
