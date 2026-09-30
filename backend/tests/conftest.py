import io
from datetime import UTC, datetime

import pytest


class FakeFileStorage:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def upload_file(
        self,
        file_data,
        filename,
        user_id,
        content_type=None,
        metadata=None,
    ):
        from app.domain.models.file import FileInfo

        data = file_data.read()
        file_id = f"file_{len(self.files) + 1}"
        self.files[file_id] = data
        return FileInfo(
            file_id=file_id,
            filename=filename,
            size=len(data),
            upload_date=datetime.now(UTC),
        )

    async def download_file(self, file_id, user_id=None):
        from app.domain.models.file import FileInfo

        data = self.files[file_id]
        return io.BytesIO(data), FileInfo(
            file_id=file_id,
            filename="skill.zip",
            size=len(data),
            upload_date=datetime.now(UTC),
        )


@pytest.fixture
def fake_file_storage():
    return FakeFileStorage()
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


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Keep offline tests deterministic regardless of the host environment.

    Two leak paths exist: the shell may carry a real API_BASE, and importing
    browser_use (pulled in transitively at collection time) runs load_dotenv,
    which walks up to the repo-root .env and injects API_BASE into the
    process. Settings() would then pick it up and break provider-default
    assertions depending on test order.
    """
    monkeypatch.delenv("API_BASE", raising=False)

@pytest.fixture
def client():
    """Create requests session"""
    session = requests.Session()
    # Don't set default Content-Type to allow multipart/form-data for file uploads
    return session
