"""
Pytest configuration and fixtures
"""
import sys
import os
import pytest
import tempfile
from pathlib import Path

# Add the parent directory to Python path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_root_env_defaults() -> None:
    """Load root .env defaults without overriding the invoking shell."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


_load_root_env_defaults()

# Some modules (notably the Celery producer) validate settings at import time,
# before pytest can enter an autouse fixture.  Never inherit a real or weak JWT
# root into the test process, and ensure collection has a harmless model key.
os.environ["JWT_SECRET_KEY"] = (
    "pytest-jwt-root-secret-at-least-32-bytes-long"
)
os.environ.setdefault("API_KEY", "test-model-key")
os.environ["DEPLOYMENT_ENVIRONMENT"] = "test"

from app.core.config import get_settings

# Allow integration tests to target parallel/local stacks on non-default ports.
SERVER_URL = (
    os.getenv("API_TEST_SERVER_URL")
    or os.getenv("BACKEND_PUBLIC_URL")
    or "http://localhost:8000"
)
BASE_URL = f"{SERVER_URL.rstrip('/')}/api/v1"


@pytest.fixture(autouse=True)
def secure_test_process_settings(monkeypatch):
    """Prevent a developer's weak local .env from destabilizing unit tests."""

    monkeypatch.setenv("API_KEY", "test-model-key")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "pytest-jwt-root-secret-at-least-32-bytes-long"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@pytest.fixture
def client():
    """Create requests session"""
    session = requests.Session()
    # Don't set default Content-Type to allow multipart/form-data for file uploads
    return session
