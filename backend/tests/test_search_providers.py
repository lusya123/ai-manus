import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.core.config import get_settings
import app.infrastructure.external.search.baidu_web_search as baidu_web_module
import app.infrastructure.external.search.bing_web_search as bing_web_module
from app.infrastructure.external.search import get_search_engine
from app.infrastructure.external.search.baidu_search import BaiduSearchEngine
from app.infrastructure.external.search.baidu_web_search import (
    BaiduWebSearchEngine,
)
from app.infrastructure.external.search.bing_search import BingSearchEngine
from app.infrastructure.external.search.bing_web_search import BingWebSearchEngine
from app.infrastructure.external.search.custom_search import CustomSearchEngine
from app.infrastructure.external.search.google_search import GoogleSearchEngine
from app.infrastructure.external.search.serper_search import SerperSearchEngine
from app.infrastructure.external.search.tavily_search import TavilySearchEngine


_SECRET_KEY = "search-api-key-should-never-appear"
_SECRET_QUERY = "private acquisition query should never appear in an error"
_SECRET_BODY = '{"credential":"response-body-should-never-appear"}'
_SECRET_URL = (
    "https://search.invalid/private?key="
    f"{_SECRET_KEY}&q={_SECRET_QUERY.replace(' ', '+')}"
)


class LeakySearchTransportError(RuntimeError):
    pass


class _FailingAsyncSession:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        raise self.error

    async def post(self, *_args, **_kwargs):
        raise self.error


class _FailingTavilyClient:
    def __init__(self, error):
        self.error = error

    async def search(self, **_kwargs):
        raise self.error


def _engine_with_failure(provider, error, monkeypatch):
    if provider == "baidu":
        engine = BaiduSearchEngine(api_key=_SECRET_KEY)
    elif provider == "bing":
        engine = BingSearchEngine(api_key=_SECRET_KEY)
    elif provider == "custom":
        engine = CustomSearchEngine(
            api_url="https://custom-search.invalid/api",
            api_key=_SECRET_KEY,
            api_key_param="key",
        )
    elif provider == "google":
        engine = GoogleSearchEngine(api_key=_SECRET_KEY, cx="private-cx")
    elif provider == "serper":
        engine = SerperSearchEngine(api_key=_SECRET_KEY)
    elif provider == "baidu_web":
        monkeypatch.setattr(
            baidu_web_module,
            "AsyncSession",
            lambda *_args, **_kwargs: _FailingAsyncSession(error),
        )
        return BaiduWebSearchEngine()
    elif provider == "bing_web":
        monkeypatch.setattr(
            bing_web_module,
            "AsyncSession",
            lambda *_args, **_kwargs: _FailingAsyncSession(error),
        )
        return BingWebSearchEngine()
    elif provider == "tavily":
        engine = TavilySearchEngine(api_key=_SECRET_KEY)
        engine.client = _FailingTavilyClient(error)
        return engine
    else:
        raise AssertionError(f"unknown test provider: {provider}")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _FailingAsyncSession(error),
    )
    return engine


def _assert_safe_failure(result, caplog, expected_type):
    assert result.success is False
    assert expected_type in result.message
    exposed = f"{result.message}\n{caplog.text}"
    for secret in (
        _SECRET_KEY,
        _SECRET_QUERY,
        _SECRET_QUERY.replace(" ", "+"),
        _SECRET_BODY,
        _SECRET_URL,
        "response-body-should-never-appear",
    ):
        assert secret not in exposed


def test_custom_search_query_param_auth_skips_header_auth():
    engine = CustomSearchEngine(
        api_url="https://example.com/search",
        api_key="secret",
        api_key_param="api_key",
    )

    assert "Authorization" not in engine._build_headers()
    assert engine._build_params("hello") == {"q": "hello", "api_key": "secret"}


@pytest.mark.asyncio
async def test_custom_search_parses_nested_results_from_get_response():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            payload = {
                "web": {
                    "results": [
                        {
                            "title": "Result",
                            "url": "https://example.com/result",
                            "description": "Nested snippet",
                        },
                        {"title": "Skipped without link"},
                    ]
                }
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        engine = CustomSearchEngine(
            api_url=f"http://127.0.0.1:{server.server_port}/search",
            api_key="secret",
            api_key_param="api_key",
            method="GET",
            result_field="web.results",
            snippet_field="description",
        )

        result = await engine.search("query")
    finally:
        server.shutdown()
        thread.join(timeout=1)

    assert result.success
    assert result.data.total_results == 1
    assert result.data.results[0].title == "Result"
    assert result.data.results[0].link == "https://example.com/result"
    assert result.data.results[0].snippet == "Nested snippet"
    assert "q=query" in requests[0][0]
    assert "api_key=secret" in requests[0][0]
    assert "Authorization" not in requests[0][1]


@pytest.mark.parametrize(
    "provider",
    [
        "baidu",
        "baidu_web",
        "bing",
        "bing_web",
        "custom",
        "google",
        "serper",
        "tavily",
    ],
)
@pytest.mark.asyncio
async def test_search_provider_failure_never_exposes_exception_text(
    provider,
    monkeypatch,
    caplog,
):
    error = LeakySearchTransportError(
        f"request={_SECRET_URL} body={_SECRET_BODY}"
    )
    engine = _engine_with_failure(provider, error, monkeypatch)
    caplog.set_level(logging.ERROR)

    result = await engine.search(_SECRET_QUERY)

    _assert_safe_failure(result, caplog, "LeakySearchTransportError")


@pytest.mark.parametrize(
    "provider",
    ["baidu", "bing", "custom", "google", "serper"],
)
@pytest.mark.asyncio
async def test_http_status_failure_keeps_type_and_status_but_not_request_details(
    provider,
    monkeypatch,
    caplog,
):
    request = httpx.Request("GET", _SECRET_URL)
    response = httpx.Response(
        403,
        request=request,
        content=_SECRET_BODY.encode(),
    )
    error = httpx.HTTPStatusError(
        f"forbidden request={_SECRET_URL} body={_SECRET_BODY}",
        request=request,
        response=response,
    )
    engine = _engine_with_failure(provider, error, monkeypatch)
    caplog.set_level(logging.ERROR)

    result = await engine.search(_SECRET_QUERY)

    _assert_safe_failure(result, caplog, "HTTPStatusError (HTTP 403)")


def test_search_engine_factory_supports_serper_and_custom(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")

    get_settings.cache_clear()
    get_search_engine.cache_clear()
    monkeypatch.setenv("SEARCH_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "serper-secret")
    assert isinstance(get_search_engine(), SerperSearchEngine)

    get_settings.cache_clear()
    get_search_engine.cache_clear()
    monkeypatch.setenv("SEARCH_PROVIDER", "custom")
    monkeypatch.setenv("SEARCH_API_URL", "https://example.com/search")
    monkeypatch.setenv("SEARCH_API_KEY", "custom-secret")
    monkeypatch.setenv("SEARCH_API_METHOD", "GET")
    monkeypatch.setenv("SEARCH_RESULT_FIELD", "web.results")

    engine = get_search_engine()
    assert isinstance(engine, CustomSearchEngine)
    assert engine.method == "GET"
    assert engine.result_field == "web.results"

    get_settings.cache_clear()
    get_search_engine.cache_clear()
