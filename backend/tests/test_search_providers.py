import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.config import get_settings
from app.infrastructure.external.search import get_search_engine
from app.infrastructure.external.search.custom_search import CustomSearchEngine
from app.infrastructure.external.search.serper_search import SerperSearchEngine


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
