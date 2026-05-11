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


def _load_root_env_value(key: str) -> str | None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name == key:
            return value.strip().strip("\"'")
    return None


# Base URL for API testing
BASE_URL = f"http://localhost:{os.getenv('SANDBOX_API_PORT') or _load_root_env_value('SANDBOX_API_PORT') or '8080'}"

@pytest.fixture
def client():
    """Create requests session"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture
def temp_test_file():
    """Create temporary test file path for container"""
    # Use container-accessible path
    temp_file = "/tmp/test_file.txt"
    # Create content via API
    import requests
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    
    content = "Line 1: Hello World\nLine 2: This is a test\nLine 3: Python testing"
    session.post(f"{BASE_URL}/api/v1/file/write", json={
        "file": temp_file,
        "content": content
    })
    
    yield temp_file
    
    # Cleanup via API
    try:
        session.post(f"{BASE_URL}/api/v1/file/write", json={
            "file": temp_file,
            "content": ""
        })
    except:
        pass
