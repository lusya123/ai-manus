import pytest

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
