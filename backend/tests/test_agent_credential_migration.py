from types import SimpleNamespace

import pytest

import scripts.migrate_agent_credentials as migration

from scripts.migrate_agent_credentials import (
    is_legacy_system_key,
    parse_legacy_system_keys,
)


def test_legacy_credential_classifier_uses_exact_old_deployment_snapshot():
    assert is_legacy_system_key("old-system-key", "old-system-key") is True
    assert is_legacy_system_key("user-byok-key", "old-system-key") is False
    assert is_legacy_system_key("old-system-key-extra", "old-system-key") is False


def test_legacy_credential_classifier_accepts_rotated_and_catalog_key_history():
    snapshots = ("old-default-key", "old-catalog-key", "rotated-default-key")

    assert is_legacy_system_key("old-default-key", snapshots) is True
    assert is_legacy_system_key("old-catalog-key", snapshots) is True
    assert is_legacy_system_key("rotated-default-key", snapshots) is True
    assert is_legacy_system_key("real-user-byok-key", snapshots) is False


def test_legacy_system_keys_parse_json_history_and_singular_compatibility():
    assert parse_legacy_system_keys(
        {
            "LEGACY_SYSTEM_API_KEYS": '["old-default", "old-catalog"]',
            "LEGACY_SYSTEM_API_KEY": "old-default",
        }
    ) == ("old-default", "old-catalog")
    assert parse_legacy_system_keys(
        {"LEGACY_SYSTEM_API_KEY": "one-old-key"}
    ) == ("one-old-key",)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"LEGACY_SYSTEM_API_KEYS": "not-json"},
        {"LEGACY_SYSTEM_API_KEYS": "[]"},
        {"LEGACY_SYSTEM_API_KEYS": '["valid", ""]'},
        {"LEGACY_SYSTEM_API_KEYS": '{"key": "value"}'},
    ],
)
def test_legacy_system_keys_reject_missing_or_ambiguous_input(environ):
    with pytest.raises(RuntimeError, match="LEGACY_SYSTEM_API_KEYS"):
        parse_legacy_system_keys(environ)


@pytest.mark.asyncio
async def test_migrated_system_agent_keeps_required_model_fields(monkeypatch):
    record = {
        "_id": "old-agent",
        "api_key": "old-system-key",
        "api_key_encrypted": "stale-value",
        "model_name": "retired-model",
        "model_provider": "openai",
        "api_base": "https://retired.example",
    }

    class Collection:
        def find(self, query):
            assert query["is_byok"] == {"$exists": False}

            class Cursor:
                async def to_list(self, length):
                    return [record.copy()]

            return Cursor()

        async def update_one(self, selector, update):
            assert selector["_id"] == record["_id"]
            record.update(update["$set"])
            for field in update["$unset"]:
                record.pop(field, None)
            return SimpleNamespace(modified_count=1)

    class Database:
        client = {"manus": {"agents": Collection()}}

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

    monkeypatch.setenv("LEGACY_SYSTEM_API_KEYS", '["old-system-key"]')
    monkeypatch.setattr(migration, "get_settings", lambda: SimpleNamespace(mongodb_database="manus"))
    monkeypatch.setattr(migration, "get_mongodb", Database)

    assert await migration.migrate(apply=True) == 0
    assert record["is_byok"] is False
    assert record["model_name"] == ""
    assert record["model_provider"] == ""
    assert record["api_base"] == ""
    assert "api_key" not in record
    assert "api_key_encrypted" not in record
