"""Classify and encrypt credentials from the pre-marker custom branch.

Usage (from ``backend/``):

    LEGACY_SYSTEM_API_KEYS='["old default key", "old catalog key"]' \
      uv run python scripts/migrate_agent_credentials.py          # dry run
    LEGACY_SYSTEM_API_KEYS='["old default key", "old catalog key"]' \
      uv run python scripts/migrate_agent_credentials.py --apply

The old schema stored both copied deployment keys and real per-user BYOK keys
in the same plaintext field.  Every deployment/catalog key that may have been
copied by an old release is therefore required to classify records without
guessing.  Values are never printed.  ``LEGACY_SYSTEM_API_KEY`` remains a
backwards-compatible way to supply one key.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from collections.abc import Iterable, Mapping

from app.core.config import get_settings
from app.infrastructure.external.llm.security import encrypt_model_api_key
from app.infrastructure.storage.mongodb import get_mongodb


def parse_legacy_system_keys(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Load the complete old server-key set without logging any values."""

    raw_many = environ.get("LEGACY_SYSTEM_API_KEYS", "").strip()
    keys: list[str] = []
    if raw_many:
        try:
            parsed = json.loads(raw_many)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LEGACY_SYSTEM_API_KEYS must be a JSON array of non-empty strings"
            ) from exc
        if not isinstance(parsed, list) or not all(
            isinstance(value, str) and value for value in parsed
        ):
            raise RuntimeError(
                "LEGACY_SYSTEM_API_KEYS must be a JSON array of non-empty strings"
            )
        keys.extend(parsed)

    legacy_single = environ.get("LEGACY_SYSTEM_API_KEY", "")
    if legacy_single:
        keys.append(legacy_single)

    # Preserve order for deterministic audits while removing duplicates.
    unique_keys = tuple(dict.fromkeys(keys))
    if not unique_keys:
        raise RuntimeError(
            "LEGACY_SYSTEM_API_KEYS must contain every deployment/catalog key "
            "used by the old custom-branch releases (JSON array); the singular "
            "LEGACY_SYSTEM_API_KEY is accepted for one-key histories"
        )
    return unique_keys


def is_legacy_system_key(
    api_key: str, legacy_system_keys: str | Iterable[str]
) -> bool:
    """Compare against all supplied snapshots exactly and in constant time."""

    candidates = (
        (legacy_system_keys,)
        if isinstance(legacy_system_keys, str)
        else tuple(legacy_system_keys)
    )
    # Evaluate every candidate so matching the first versus last key does not
    # change the comparison work performed by the migration process.
    return any(
        [hmac.compare_digest(api_key, candidate) for candidate in candidates]
    )


async def migrate(*, apply: bool) -> int:
    legacy_system_keys = parse_legacy_system_keys(os.environ)

    settings = get_settings()
    mongodb = get_mongodb()
    await mongodb.initialize()
    try:
        collection = mongodb.client[settings.mongodb_database]["agents"]
        query = {
            "api_key": {"$type": "string", "$ne": ""},
            "is_byok": {"$exists": False},
        }
        records = await collection.find(query).to_list(length=None)
        system_count = sum(
            is_legacy_system_key(record["api_key"], legacy_system_keys)
            for record in records
        )
        byok_count = len(records) - system_count
        print(
            "Legacy credential audit: "
            f"{len(records)} total, {system_count} copied system, "
            f"{byok_count} BYOK"
        )
        if not apply:
            print("Dry run only; rerun with --apply after verifying the counts.")
            return 0

        migrated = 0
        for record in records:
            plaintext = record["api_key"]
            selector = {
                "_id": record["_id"],
                "api_key": plaintext,
                "is_byok": {"$exists": False},
            }
            if is_legacy_system_key(plaintext, legacy_system_keys):
                update = {
                    "$set": {"is_byok": False},
                    "$unset": {
                        "api_key": "",
                        "api_key_encrypted": "",
                        # Without an explicit model_id mapping this record must
                        # safely fall back to the current deployment default.
                        "model_name": "",
                        "model_provider": "",
                        "api_base": "",
                    },
                }
            else:
                update = {
                    "$set": {
                        "is_byok": True,
                        "api_key_encrypted": encrypt_model_api_key(
                            plaintext, settings
                        ),
                    },
                    "$unset": {"api_key": ""},
                }
            result = await collection.update_one(selector, update)
            migrated += result.modified_count
        print(f"Migration complete: {migrated}/{len(records)} records updated.")
        return 0 if migrated == len(records) else 1
    finally:
        await mongodb.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write classified/encrypted records (default is dry-run)",
    )
    args = parser.parse_args()
    return asyncio.run(migrate(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
