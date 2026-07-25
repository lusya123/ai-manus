import base64
import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.core.config import get_settings
from app.domain.models.search import SearchResultItem
import app.infrastructure.external.search.baidu_web_search as baidu_web_module
import app.infrastructure.external.search.bing_web_search as bing_web_module
from app.infrastructure.external.search import get_search_engine
from app.infrastructure.external.search.baidu_search import BaiduSearchEngine
from app.infrastructure.external.search.baidu_web_search import (
    BaiduWebSearchEngine,
)
from app.infrastructure.external.search.bing_search import BingSearchEngine
from app.infrastructure.external.search.bing_web_search import (
    BingWebSearchEngine,
    _decode_bing_redirect,
    _results_match_query,
)
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


class _FakeBingResponse:
    def __init__(
        self,
        text,
        *,
        content_type="text/html; charset=utf-8",
        url="https://www.bing.com/search",
    ):
        self.text = text
        self.headers = {"content-type": content_type}
        self.url = url

    def raise_for_status(self):
        return None


class _FakeBingSession:
    def __init__(self, factory, response, kwargs):
        self.factory = factory
        self.response = response
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        self.factory.requests.append((url, kwargs))
        return self.response


class _FakeBingSessionFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sessions = []
        self.requests = []

    def __call__(self, *_args, **kwargs):
        if not self.responses:
            raise AssertionError("Unexpected extra Bing request")
        session = _FakeBingSession(
            self,
            self.responses.pop(0),
            kwargs,
        )
        self.sessions.append(session)
        return session


def _bing_page(query, results_markup="", extra_markup=""):
    return (
        "<!doctype html><html><body>"
        f'<input id="sb_form_q" name="q" value="{html.escape(query, quote=True)}">'
        f'<ol id="b_results">{results_markup}</ol>'
        f"{extra_markup}</body></html>"
    )


def _bing_result(title, link, snippet):
    return (
        '<li class="b_algo"><h2>'
        f'<a href="{html.escape(link, quote=True)}">{title}</a>'
        "</h2><div class=\"b_caption\">"
        f"<p>{snippet}</p></div></li>"
    )


@pytest.mark.parametrize(
    ("query", "title", "snippet"),
    [
        (
            "IANA example domains official",
            "Example recipes",
            "A collection of simple dinner ideas.",
        ),
        (
            "OpenAI Responses API documentation",
            "Random weather API",
            "A small public endpoint for weather forecasts.",
        ),
        (
            "苹果公司最新新闻",
            "无关公司招聘",
            "本周招聘岗位及办公地点介绍。",
        ),
    ],
)
def test_bing_relevance_rejects_one_word_incidental_overlap(
    query,
    title,
    snippet,
):
    result = SearchResultItem(
        title=title,
        link="https://unrelated.example/result",
        snippet=snippet,
    )

    assert _results_match_query(query, [result]) is False


def test_bing_relevance_accepts_a_majority_in_one_result():
    result = SearchResultItem(
        title="IANA-managed Reserved Domains",
        link="https://www.iana.org/help/example-domains",
        snippet="Official information about example domains.",
    )

    assert _results_match_query(
        "IANA example domains official",
        [result],
    )


@pytest.mark.parametrize(
    "query",
    [
        "苹果公司 新闻",
        "苹果公司 最新新闻",
        "苹果公司新闻",
    ],
)
def test_bing_relevance_does_not_count_one_chinese_entity_as_many_concepts(
    query,
):
    entity_only = SearchResultItem(
        title="Apple（中国大陆）- 官方网站",
        link="https://www.apple.com.cn/",
        snippet="探索苹果公司的产品、服务、技术支持与购买信息。",
    )

    assert _results_match_query(query, [entity_only]) is False


@pytest.mark.parametrize(
    "query",
    [
        "苹果公司 新闻",
        "苹果公司 最新新闻",
        "苹果公司新闻",
    ],
)
def test_bing_relevance_accepts_chinese_entity_and_requested_concept(query):
    relevant = SearchResultItem(
        title="苹果公司 新闻与公告",
        link="https://news.example/apple-company",
        snippet="苹果公司今天发布最新产品新闻及公司公告。",
    )

    assert _results_match_query(query, [relevant]) is True


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


@pytest.mark.asyncio
async def test_bing_web_retries_poisoned_200_with_fresh_session(
    monkeypatch,
):
    query = "IANA example domains official"
    poisoned = _bing_page(
        query,
        _bing_result(
            "<span>Kayu</span> - Wikipedia bahasa Indonesia",
            "https://id.wikipedia.org/wiki/Kayu",
            "Ilmu kayu membahas sifat fisika dan mekanika berbagai jenis kayu.",
        ),
    )
    valid = _bing_page(
        query,
        _bing_result(
            "<span>Internet Assigned</span> <strong>Numbers Authority</strong>",
            "https://www.iana.org/",
            "IANA manages protocol registries and reserved example domains.",
        ),
    )
    factory = _FakeBingSessionFactory(
        [_FakeBingResponse(poisoned), _FakeBingResponse(valid)]
    )
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(query, "past_week")

    assert result.success
    assert result.data.results[0].title == (
        "Internet Assigned Numbers Authority"
    )
    assert result.data.results[0].link == "https://www.iana.org/"
    assert len(factory.sessions) == 2
    assert factory.sessions[0] is not factory.sessions[1]
    assert len(factory.requests) == 2
    for _url, request in factory.requests:
        assert request["params"]["mkt"] == "en-US"
        assert request["params"]["setlang"] == "en"
        assert request["params"]["filters"] == 'ex1:"ez3"'
        assert "cc" not in request["params"]
    for session in factory.sessions:
        assert session.kwargs == {"impersonate": "chrome"}


@pytest.mark.asyncio
async def test_bing_web_poisoned_200_fails_closed_without_leaking_content(
    monkeypatch,
    caplog,
):
    query = "private IANA diagnostic query 7f91"
    poisoned_titles = ("Bstation video portal", "Random calculator")
    responses = [
        _FakeBingResponse(
            _bing_page(
                query,
                _bing_result(
                    title,
                    f"https://example.com/{index}?q=IANA+diagnostic",
                    "Unrelated response content that must not be trusted.",
                ),
            )
        )
        for index, title in enumerate(poisoned_titles)
    ]
    factory = _FakeBingSessionFactory(responses)
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)
    caplog.set_level(logging.WARNING)

    result = await BingWebSearchEngine().search(query)

    assert result.success is False
    assert result.data.results == []
    assert result.message == "Bing Web Search returned an untrusted response"
    exposed = f"{result.message}\n{caplog.text}"
    assert query not in exposed
    for title in poisoned_titles:
        assert title not in exposed
    assert len(factory.sessions) == 2


@pytest.mark.asyncio
async def test_bing_web_accepts_explicit_no_results_without_retry(
    monkeypatch,
):
    query = "quoted query with no indexed result"
    no_results = _bing_page(
        query,
        '<li class="b_no"><h1>There are no results for this search</h1></li>',
    )
    factory = _FakeBingSessionFactory([_FakeBingResponse(no_results)])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(query)

    assert result.success
    assert result.data.total_results == 0
    assert result.data.results == []
    assert len(factory.sessions) == 1


@pytest.mark.parametrize(
    "responses",
    [
        [
            _FakeBingResponse(
                _bing_page(
                    "IANA example domains",
                    extra_markup=(
                        '<form action="/captcha"><div id="b_captcha">'
                        "Verify you are human</div></form>"
                    ),
                )
            ),
            _FakeBingResponse(
                _bing_page(
                    "IANA example domains",
                    extra_markup='<div id="challenge-form"></div>',
                )
            ),
        ],
        [
            _FakeBingResponse("{}", content_type="application/json"),
            _FakeBingResponse("{}", content_type="application/json"),
        ],
        [
            _FakeBingResponse(_bing_page("IANA example domains")),
            _FakeBingResponse(_bing_page("IANA example domains")),
        ],
        [
            _FakeBingResponse(_bing_page("different echoed query")),
            _FakeBingResponse(_bing_page("different echoed query")),
        ],
    ],
    ids=["challenge", "non-html", "abnormal-empty", "wrong-query"],
)
@pytest.mark.asyncio
async def test_bing_web_rejects_untrusted_success_pages(
    responses,
    monkeypatch,
):
    factory = _FakeBingSessionFactory(responses)
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search("IANA example domains")

    assert result.success is False
    assert result.data.results == []
    assert len(factory.sessions) == 2


@pytest.mark.asyncio
async def test_bing_web_decodes_urlsafe_redirect_deduplicates_and_filters_urls(
    monkeypatch,
):
    query = "Fox News official"
    destination = (
        "https://www.foxnews.com/us?"
        "msockid=1ceecb3d788d6fef03ffdc9c79fe6e3d"
    )
    encoded = base64.urlsafe_b64encode(destination.encode()).decode().rstrip("=")
    assert "_" in encoded or "-" in encoded
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}"
    page = _bing_page(
        query,
        "".join(
            [
                _bing_result(
                    "<span>Unsafe</span> result",
                    "javascript:alert(1)",
                    "Fox News text must not make an unsafe URL acceptable.",
                ),
                _bing_result(
                    "<span>Fox</span> <strong>News</strong>",
                    redirect,
                    "Fox News provides United States news and reporting.",
                ),
                _bing_result(
                    "Duplicate Fox News",
                    destination,
                    "The duplicate destination must be removed.",
                ),
            ]
        ),
    )
    factory = _FakeBingSessionFactory([_FakeBingResponse(page)])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(query)

    assert result.success
    assert len(result.data.results) == 1
    assert result.data.results[0].title == "Fox News"
    assert result.data.results[0].link == destination
    assert _decode_bing_redirect(redirect) == destination


def test_search_engine_factory_configures_bing_web_locale(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("SEARCH_PROVIDER", "bing_web")
    monkeypatch.setenv("BING_WEB_MARKET", "en-GB")
    monkeypatch.setenv("BING_WEB_SETLANG", "en")
    get_settings.cache_clear()
    get_search_engine.cache_clear()

    engine = get_search_engine()

    assert isinstance(engine, BingWebSearchEngine)
    assert engine.market == "en-GB"
    assert engine.setlang == "en"
    get_settings.cache_clear()
    get_search_engine.cache_clear()


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
