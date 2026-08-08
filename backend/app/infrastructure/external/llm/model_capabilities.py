"""Provider/model capability normalization for outbound LLM requests."""

from __future__ import annotations

import re


_CLAUDE_VERSION_RE = re.compile(
    r"\Aclaude-(?P<family>opus|sonnet)-"
    r"(?P<major>[1-9][0-9]*)"
    r"(?:-(?P<minor>(?![0-9]{8}(?:-[0-9]{8})?\Z)(?:0|[1-9][0-9]*)))?"
    r"(?:-(?P<snapshot>[0-9]{8}))?\Z"
)


def anthropic_requires_default_sampling(model: str | None) -> bool:
    """Return whether Anthropic rejects non-default sampling parameters.

    Anthropic Messages models keep the legacy sampling fields in their SDK
    types, but Opus 4.7+ and Sonnet 5+ reject non-default values at runtime.
    Unknown/custom aliases remain unchanged instead of guessing their
    capabilities.
    """

    if not isinstance(model, str):
        return False
    match = _CLAUDE_VERSION_RE.fullmatch(model.lower())
    if match is None:
        return False

    family = match.group("family")
    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    if family == "opus":
        return (major, minor) >= (4, 7)
    return major >= 5


def effective_temperature(
    provider: str | None,
    model: str | None,
    temperature: float | None,
) -> float | None:
    """Return a wire-compatible temperature, or ``None`` to omit it."""

    if (
        (provider or "").strip().lower() == "anthropic"
        and anthropic_requires_default_sampling(model)
    ):
        return None
    return temperature
