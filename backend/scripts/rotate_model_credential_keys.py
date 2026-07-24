"""Audit and re-encrypt stored BYOK keys with the active independent key.

Run this while the old JWT-derived key and/or old independent keys are still
available for decryption. ``MODEL_CREDENTIAL_ENCRYPTION_KEYS`` is a JSON array:
the first entry writes new ciphertext and the remaining entries are read-only
rotation keys. The default is a no-write audit.

    uv run python scripts/rotate_model_credential_keys.py
    uv run python scripts/rotate_model_credential_keys.py --apply

No credential value is printed.
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.infrastructure.external.llm.security import (
    _credential_keyring,
    decrypt_model_api_key,
    encrypt_model_api_key,
)
from app.infrastructure.storage.mongodb import get_mongodb


async def rotate(*, apply: bool) -> int:
    settings = get_settings()
    active_id, _keyring, _legacy = _credential_keyring(settings)
    mongodb = get_mongodb()
    await mongodb.initialize()
    try:
        collection = mongodb.client[settings.mongodb_database]["agents"]
        records = await collection.find(
            {
                "is_byok": True,
                "api_key_encrypted": {"$type": "string", "$ne": ""},
            },
            projection={"_id": 1, "api_key_encrypted": 1},
        ).to_list(length=None)
        prefix = f"v2${active_id}$"
        pending = [
            record
            for record in records
            if not record["api_key_encrypted"].startswith(prefix)
        ]

        # Validate the complete set before the first write. This catches a
        # missing old key while operators can still restore the keyring.
        for record in pending:
            decrypt_model_api_key(record["api_key_encrypted"], settings)
        print(
            "Model credential key audit: "
            f"{len(records)} encrypted BYOK records, {len(pending)} need rotation"
        )
        if not apply:
            print("Dry run only; rerun with --apply after verifying the count.")
            return 0

        updated = 0
        for record in pending:
            old_ciphertext = record["api_key_encrypted"]
            plaintext = decrypt_model_api_key(old_ciphertext, settings)
            new_ciphertext = encrypt_model_api_key(plaintext, settings)
            result = await collection.update_one(
                {
                    "_id": record["_id"],
                    "is_byok": True,
                    "api_key_encrypted": old_ciphertext,
                },
                {
                    "$set": {"api_key_encrypted": new_ciphertext},
                    "$unset": {"api_key": ""},
                },
            )
            updated += result.modified_count
        print(f"Key rotation complete: {updated}/{len(pending)} records updated.")
        return 0 if updated == len(pending) else 1
    finally:
        await mongodb.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite records with the active key (default is dry-run)",
    )
    args = parser.parse_args()
    return asyncio.run(rotate(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
