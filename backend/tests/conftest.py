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
    """Load root .env values for local integration tests without overriding the shell."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_root_env_defaults()

# Base URL for API testing. Allow parallel local stacks to run on different ports.
SERVER_URL = os.getenv("API_TEST_SERVER_URL") or os.getenv("BACKEND_PUBLIC_URL") or "http://localhost:8000"
BASE_URL = f"{SERVER_URL.rstrip('/')}/api/v1"

@pytest.fixture
def client():
    """Create requests session"""
    session = requests.Session()
    # Don't set default Content-Type to allow multipart/form-data for file uploads
    return session
