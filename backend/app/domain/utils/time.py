from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return an aware UTC timestamp for domain persistence."""

    return datetime.now(UTC)


def epoch_seconds(value: datetime) -> int:
    """Serialize Mongo/Pydantic datetimes without host-timezone drift.

    MongoDB stores UTC instants but PyMongo returns them as naive datetimes by
    default. ``datetime.timestamp()`` interprets a naive value in the host's
    local timezone, so treat legacy naive database values as UTC.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())
