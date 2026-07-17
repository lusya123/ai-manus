import asyncio
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
import pytest
from redis.asyncio import Redis

from app.application.services.email_service import EmailService
from app.infrastructure.external.cache.redis_cache import RedisCache


class _AtomicVerificationCache:
    def __init__(self):
        self.values = {}
        self._lock = asyncio.Lock()

    async def set(self, key, value, ttl=None):
        self.values[key] = dict(value)
        return True

    async def consume_verification_code(self, key, code, max_attempts):
        async with self._lock:
            value = self.values.get(key)
            if value is None:
                return False
            if datetime.fromisoformat(value["expires_at"]) <= datetime.now():
                self.values.pop(key, None)
                return False
            attempts = int(value.get("attempts", 0))
            if attempts >= max_attempts:
                self.values.pop(key, None)
                return False
            attempts += 1
            if value["code"] == code:
                self.values.pop(key, None)
                return True
            if attempts >= max_attempts:
                self.values.pop(key, None)
            else:
                value["attempts"] = attempts
            return False


def _service(cache):
    service = object.__new__(EmailService)
    service.cache = cache
    return service


async def _seed(cache, *, code="123456"):
    cache.values["verification_code:person@example.test"] = {
        "code": code,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
        "attempts": 0,
    }


async def test_concurrent_correct_verification_code_is_consumed_once():
    cache = _AtomicVerificationCache()
    await _seed(cache)
    service = _service(cache)

    results = await asyncio.gather(
        *[
            service.verify_code("person@example.test", "123456")
            for _ in range(12)
        ]
    )

    assert results.count(True) == 1
    assert results.count(False) == 11
    assert cache.values == {}


async def test_concurrent_wrong_attempts_cannot_bypass_limit():
    cache = _AtomicVerificationCache()
    await _seed(cache)
    service = _service(cache)

    assert not any(
        await asyncio.gather(
            *[
                service.verify_code("person@example.test", "000000")
                for _ in range(10)
            ]
        )
    )
    assert await service.verify_code("person@example.test", "123456") is False
    assert cache.values == {}


async def test_verification_fails_closed_when_atomic_cache_is_unavailable():
    class BrokenCache:
        async def consume_verification_code(self, *_args):
            raise ConnectionError("redis unavailable")

    assert (
        await _service(BrokenCache()).verify_code(
            "person@example.test", "123456"
        )
        is False
    )


def test_verification_code_uses_six_digit_cryptographic_range(monkeypatch):
    called = []

    def randbelow(limit):
        called.append(limit)
        return 42

    monkeypatch.setattr("secrets.randbelow", randbelow)

    assert _service(None)._generate_verification_code() == "100042"
    assert called == [900000]


async def test_smtp_transport_runs_off_the_event_loop_thread(monkeypatch):
    main_thread = threading.get_ident()
    observed_threads = []

    class SMTP:
        def __init__(self, host, port):
            observed_threads.append(threading.get_ident())

        def login(self, username, password):
            return None

        def sendmail(self, sender, recipient, text):
            return None

        def quit(self):
            return None

    monkeypatch.setattr("smtplib.SMTP_SSL", SMTP)
    service = _service(None)
    service.settings = type(
        "Settings",
        (),
        {
            "email_host": "smtp.example.test",
            "email_port": 465,
            "email_username": "sender@example.test",
            "email_password": "secret",
            "email_from": "sender@example.test",
        },
    )()
    message = service._create_verification_email(
        "person@example.test", "123456"
    )

    await service._send_smtp_email(message, "person@example.test")

    assert observed_threads
    assert observed_threads[0] != main_thread


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def standalone_verification_redis():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    process = subprocess.Popen(
        [
            executable,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            import redis

            probe = redis.Redis(host="127.0.0.1", port=port)
            probe.ping()
            probe.close()
            break
        except Exception:
            if process.poll() is not None or time.monotonic() >= deadline:
                process.terminate()
                pytest.skip("temporary redis-server did not start")
            time.sleep(0.05)
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def test_real_redis_lua_serializes_success_and_attempt_limits(
    standalone_verification_redis,
):
    client = Redis(
        host="127.0.0.1",
        port=standalone_verification_redis,
        decode_responses=True,
    )

    class InitializedRedis:
        def __init__(self, value):
            self.client = value

        async def initialize(self):
            return None

    cache = object.__new__(RedisCache)
    cache.redis_client = InitializedRedis(client)
    service = _service(cache)
    try:
        await service._store_verification_code(
            "person@example.test", "123456"
        )
        correct = await asyncio.gather(
            *[
                service.verify_code("person@example.test", "123456")
                for _ in range(24)
            ]
        )
        assert correct.count(True) == 1
        assert correct.count(False) == 23

        await service._store_verification_code(
            "person@example.test", "123456"
        )
        wrong = await asyncio.gather(
            *[
                service.verify_code("person@example.test", "000000")
                for _ in range(20)
            ]
        )
        assert not any(wrong)
        assert (
            await service.verify_code("person@example.test", "123456")
            is False
        )
        assert (
            await client.exists("verification_code:person@example.test") == 0
        )
    finally:
        await client.aclose()
