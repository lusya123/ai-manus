import logging
import re
import sys
from typing import Any

from app.core.config import get_settings


_CAPABILITY_QUERY = re.compile(r"\?[^\s\"'<>]*")
_PREVIEW_CAPABILITY = re.compile(
    r"(?P<prefix>/api/v1/sessions/[^/?\s]+/preview/)"
    r"[^/?\s]+(?P<suffix>/|$)"
)
_BEARER_CAPABILITY = re.compile(
    r"(?i)\bbearer\s+[^\s,;\"']+"
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?i)\bauthorization\s*=\s*(?:\"[^\"]*\"|'[^']*'|"
    r"(?:bearer|basic)\s+[^\s,;]+|[^\s,;]+)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"token|signature|api[_-]?key)\s*=\s*(?:\"[^\"]*\"|'[^']*'|"
    r"[^\s,;]+)"
)


def redact_capability_text(value: Any) -> str:
    """Remove signed-query and preview-token capabilities from diagnostic text.

    This helper is intentionally conservative: query strings are never useful
    enough in application logs to justify retaining a bearer signature, and a
    preview JWT is always the first path segment after ``/preview/``.
    """

    text = str(value)
    text = _PREVIEW_CAPABILITY.sub(
        r"\g<prefix><redacted>\g<suffix>", text
    )
    text = _CAPABILITY_QUERY.sub("?<redacted>", text)
    text = _AUTHORIZATION_ASSIGNMENT.sub("authorization=<redacted>", text)
    text = _BEARER_CAPABILITY.sub("Bearer <redacted>", text)
    return _SENSITIVE_ASSIGNMENT.sub(r"\g<key>=<redacted>", text)


class CapabilityRedactionFilter(logging.Filter):
    """Redact bearer capabilities from any record handled by the application.

    Third-party browser and WebSocket libraries can serialize a complete signed
    gateway URL in either a message argument or an exception. Installing this
    filter on root handlers provides a final boundary even when a dependency's
    own logging statements are outside our control.
    """

    _exception_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access formatter consumes a five-element ``record.args``
        # tuple directly (client, method, target, HTTP version, status).  If we
        # eagerly render that record and clear ``args`` like an ordinary log
        # record, ``AccessFormatter`` raises while trying to unpack the tuple
        # and every request produces a logging traceback.  Preserve structured
        # Uvicorn arguments while still redacting every string field.  The
        # request-target-specific filter runs first and removes query/path
        # capabilities; this branch is the generic final boundary.
        preserve_uvicorn_args = (
            record.name.startswith("uvicorn.")
            and isinstance(record.args, tuple)
            and bool(record.args)
        )
        if preserve_uvicorn_args:
            record.msg = redact_capability_text(record.msg)
            record.args = tuple(
                redact_capability_text(value)
                if isinstance(value, str)
                else value
                for value in record.args
            )
            record.name = redact_capability_text(record.name)
            self._redact_exception_fields(record)
            return True

        # Render first so redacting a format template such as ``token=%s`` does
        # not remove a placeholder while leaving its argument behind. Replacing
        # msg/args at the handler boundary preserves the rendered message while
        # making URL-like objects and nested exception strings safe as well.
        try:
            message = record.getMessage()
        except Exception:
            # A malformed third-party format string must not bypass the final
            # logging boundary with its original arguments.
            message = str(record.msg)
        record.msg = redact_capability_text(message)
        record.args = ()
        record.name = redact_capability_text(record.name)

        self._redact_exception_fields(record)
        return True

    def _redact_exception_fields(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            # Formatter.formatException would otherwise append the original
            # exception text after handler filters have already run.
            record.exc_text = redact_capability_text(
                self._exception_formatter.formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_capability_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_capability_text(record.stack_info)


def _install_capability_filter(target: Any) -> None:
    if not any(
        isinstance(log_filter, CapabilityRedactionFilter)
        for log_filter in target.filters
    ):
        target.addFilter(CapabilityRedactionFilter())


class UvicornAccessLogCapabilityRedactionFilter(logging.Filter):
    """Remove bearer capabilities from Uvicorn access-log request targets.

    File and preview URLs use their query strings as short-lived bearer
    capabilities.  Uvicorn normally includes that complete request target in
    ``uvicorn.access`` records, so retaining it in logs would turn a temporary
    URL into a reusable credential for anyone who can read those logs.
    """

    _preview_token = _PREVIEW_CAPABILITY

    def _redact_request_target(self, request_target: str) -> str:
        # Signed files/VNC use the query string as the capability.
        redacted_target = request_target.partition("?")[0]
        # Preview URLs must carry a JWT in the path so relative asset URLs keep
        # working inside an iframe. Hide that segment too.
        return self._preview_token.sub(
            r"\g<prefix><redacted>\g<suffix>", redacted_target
        )

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            redacted_args = list(args)
            changed = False
            # HTTP access records store the target at args[2]. WebSocket
            # accept/reject records are emitted on uvicorn.error and store it
            # at args[1]. Looking for path-shaped arguments safely covers both
            # formats without mutating client addresses or status messages.
            for index, value in enumerate(redacted_args):
                if isinstance(value, str) and value.startswith("/api/"):
                    replacement = self._redact_request_target(value)
                    if replacement != value:
                        redacted_args[index] = replacement
                        changed = True
            if changed:
                record.args = tuple(redacted_args)
        return True


def setup_logging():
    """
    Configure the application logging system

    Sets up application console logging and capability-safe Uvicorn request
    logging. File rotation remains the responsibility of the process manager.
    """
    # Get configuration
    settings = get_settings()
    
    # Get root logger
    root_logger = logging.getLogger()
    
    # Set root log level
    log_level = getattr(logging, settings.log_level)
    root_logger.setLevel(log_level)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    _install_capability_filter(console_handler)
    
    # Add handlers to root logger
    root_logger.addHandler(console_handler)
    # Protect handlers installed by a process manager before the application
    # configured its own console output as well.
    for handler in root_logger.handlers:
        _install_capability_filter(handler)
    _install_capability_filter(root_logger)

    # Disable verbose logging for pymongo
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # httpx/httpcore include complete request URLs at INFO. Some providers use
    # signed gateway URLs, so those records would persist bearer capabilities.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # cdp-use logs its complete connection URL at INFO. AgentBay CDP links are
    # bearer capabilities, and browser-use may also include them in failures.
    for logger_name in (
        "cdp_use",
        "cdp_use.client",
        "browser_use",
        "websockets.client",
    ):
        dependency_logger = logging.getLogger(logger_name)
        dependency_logger.setLevel(logging.WARNING)
        _install_capability_filter(dependency_logger)

    for logger_name in ("uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(log_filter, UvicornAccessLogCapabilityRedactionFilter)
            for log_filter in uvicorn_logger.filters
        ):
            uvicorn_logger.addFilter(UvicornAccessLogCapabilityRedactionFilter())
        # Uvicorn normally owns non-propagating handlers, so retain the generic
        # boundary in addition to its request-target-specific filter.
        _install_capability_filter(uvicorn_logger)
    
    # Log initialization complete
    root_logger.info("Logging system initialized - Console logging active")
