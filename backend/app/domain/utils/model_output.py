import json
import re
from typing import Any


VISIBLE_CONTENT_TYPES = {"text", "output_text"}
HIDDEN_CONTENT_TYPES = {
    "thinking",
    "reasoning",
    "redacted_thinking",
    "reasoning_content",
    "signature",
    "tool_use",
}

_HIDDEN_KEYS = HIDDEN_CONTENT_TYPES | {"thought", "thinking_content"}
_HIDDEN_TAG_NAMES = ("think", "thinking", "reasoning")
_HIDDEN_TAG_RE = re.compile(
    r"<(?:think|thinking|reasoning)\b[^>]*>.*?(?:</(?:think|thinking|reasoning)>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_TAG_BODY_RE = re.compile(
    r"<(?P<tag>think|thinking|reasoning)\b[^>]*>(?P<body>.*?)(?:</(?P=tag)>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_FENCE_RE = re.compile(
    r"```(?:think|thinking|thought|reasoning)\s*\n.*?(?:```|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_FENCE_BODY_RE = re.compile(
    r"```(?:think|thinking|thought|reasoning)\s*\n(?P<body>.*?)(?:```|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_TOKEN_RE = re.compile(
    r"<\|(?:begin|start)_of_(?:thinking|thought|reasoning)\|>.*?"
    r"(?:<\|(?:end|stop)_of_(?:thinking|thought|reasoning)\|>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_TOKEN_BODY_RE = re.compile(
    r"<\|(?:begin|start)_of_(?:thinking|thought|reasoning)\|>(?P<body>.*?)"
    r"(?:<\|(?:end|stop)_of_(?:thinking|thought|reasoning)\|>|$)",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_model_text(text: str) -> str:
    """Remove private reasoning text from model-produced plain strings."""
    if not text:
        return ""

    previous = None
    while previous != text:
        previous = text
        text = _HIDDEN_TOKEN_RE.sub("", text)
        text = _HIDDEN_FENCE_RE.sub("", text)
        text = _HIDDEN_TAG_RE.sub("", text)

    text = _remove_trailing_hidden_tag_prefix(text)
    return text.strip()


def extract_model_thinking_text(text: str) -> str:
    """Return model-produced private reasoning from explicit thinking wrappers."""
    if not text:
        return ""

    parts: list[str] = []
    for pattern in (_HIDDEN_TOKEN_BODY_RE, _HIDDEN_FENCE_BODY_RE, _HIDDEN_TAG_BODY_RE):
        for match in pattern.finditer(text):
            body = match.group("body").strip()
            if body:
                parts.append(body)
    return "\n\n".join(parts)


def normalize_model_content(value: Any) -> str:
    """Normalize model content blocks to user-visible plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_model_string(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = normalize_model_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        return _normalize_model_dict(value)
    if hasattr(value, "model_dump"):
        try:
            return normalize_model_content(value.model_dump())
        except Exception:
            pass
    item_type = getattr(value, "type", None)
    if item_type in HIDDEN_CONTENT_TYPES:
        return ""
    if item_type and item_type not in VISIBLE_CONTENT_TYPES:
        return ""
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return sanitize_model_text(text)
    content = getattr(value, "content", None)
    if isinstance(content, (str, list, dict)):
        return normalize_model_content(content)
    return sanitize_model_text(str(value))


def _normalize_model_string(value: str) -> str:
    sanitized = sanitize_model_text(value)
    if not _should_parse_json_string(sanitized):
        return sanitized

    try:
        parsed = json.loads(sanitized)
    except (TypeError, ValueError):
        return sanitized

    normalized = normalize_model_content(parsed)
    if normalized or _contains_hidden_payload(parsed):
        return normalized
    return sanitized


def _normalize_model_dict(value: dict[str, Any]) -> str:
    value_type = value.get("type")
    if value_type in HIDDEN_CONTENT_TYPES:
        return ""
    if value_type and value_type not in VISIBLE_CONTENT_TYPES:
        return ""

    if isinstance(value.get("text"), str):
        return sanitize_model_text(value["text"])
    if "content" in value:
        return normalize_model_content(value["content"])
    if "message" in value and isinstance(value["message"], (str, list, dict)):
        return normalize_model_content(value["message"])
    if any(key in value for key in _HIDDEN_KEYS):
        return ""

    return sanitize_model_text(json.dumps(value, ensure_ascii=False))


def _should_parse_json_string(value: str) -> bool:
    stripped = value.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return False

    return (
        any(f'"{key}"' in stripped for key in _HIDDEN_KEYS)
        or '"type"' in stripped
    )


def _contains_hidden_payload(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_hidden_payload(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in HIDDEN_CONTENT_TYPES:
            return True
        if any(key in value for key in _HIDDEN_KEYS):
            return True
        return any(_contains_hidden_payload(item) for item in value.values())
    return False


def _remove_trailing_hidden_tag_prefix(text: str) -> str:
    last_open = text.rfind("<")
    if last_open < 0:
        return text

    suffix = text[last_open:].lower()
    if ">" in suffix:
        return text

    candidate = suffix[1:].lstrip("/")
    if any(tag.startswith(candidate) for tag in _HIDDEN_TAG_NAMES):
        return text[:last_open]
    return text
