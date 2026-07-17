"""One-way storage for Claw runtime capability keys."""

import hashlib
import hmac


_CLAW_API_KEY_CONTEXT = b"ai-manus/claw-api-key/v1\0"


def claw_api_key_digest(api_key: str, server_secret: str) -> str:
    """Return a domain-separated, server-keyed digest for a runtime key."""

    if not api_key:
        raise ValueError("Claw API key is required")
    if not server_secret:
        raise ValueError("Server credential secret is required")
    return hmac.new(
        server_secret.encode("utf-8"),
        _CLAW_API_KEY_CONTEXT + api_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def claw_api_key_hmac_secrets(settings) -> tuple[str, ...]:
    """Return current then previous HMAC secrets for online key rotation.

    ``CLAW_API_KEY_HMAC_KEYS`` is a comma-separated keyring whose first item
    signs new records.  Falling back to JWT_SECRET_KEY keeps existing installs
    bootable; production deployments should configure the independent keyring
    before rotating JWT_SECRET_KEY.
    """

    configured = str(
        getattr(settings, "claw_api_key_hmac_keys", None) or ""
    )
    keys = tuple(part.strip() for part in configured.split(",") if part.strip())
    if keys:
        return keys
    fallback = str(getattr(settings, "jwt_secret_key", "") or "")
    if not fallback:
        raise ValueError("Claw API key HMAC keyring is not configured")
    return (fallback,)
