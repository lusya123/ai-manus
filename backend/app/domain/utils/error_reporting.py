"""Stable, capability-safe error summaries shared across application layers."""


def safe_exception_summary(error: BaseException) -> str:
    """Return exception metadata without serializing URLs or response bodies."""

    details = [type(error).__name__]
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        details.append(f"HTTP {status_code}")
    errno = getattr(error, "errno", None)
    if isinstance(errno, int):
        details.append(f"errno {errno}")
    return details[0] if len(details) == 1 else (
        f"{details[0]} ({', '.join(details[1:])})"
    )
