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
import app.infrastructure.external.search.anthropic_web_search as anthropic_web_module
from app.infrastructure.external.search import get_search_engine
from app.infrastructure.external.search.baidu_search import BaiduSearchEngine
from app.infrastructure.external.search.baidu_web_search import (
    BaiduWebSearchEngine,
)
from app.infrastructure.external.search.anthropic_web_search import (
    AnthropicWebSearchEngine,
    _parse_anthropic_web_response,
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
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.url = url
        self.closed = False

    def raise_for_status(self):
        return None

    async def aiter_content(self):
        yield self.content

    async def aclose(self):
        self.closed = True


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


def test_web_relevance_accepts_all_short_query_concepts_in_one_result():
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
    ("query", "title", "link", "snippet"),
    [
        (
            "Vue Composition API documentation",
            "Vue API reference",
            "https://vuejs.org/api/",
            "The Vue API reference.",
        ),
        (
            "OpenAI Responses API documentation",
            "OpenAI API documentation",
            "https://developers.openai.com/api/",
            "Use the OpenAI API in your application.",
        ),
    ],
)
def test_web_relevance_rejects_missing_third_concept(
    query,
    title,
    link,
    snippet,
):
    result = SearchResultItem(
        title=title,
        link=link,
        snippet=snippet,
    )

    assert _results_match_query(query, [result]) is False


def test_web_relevance_requires_every_concept_for_long_queries():
    result = SearchResultItem(
        title="Alpha beta gamma",
        link="https://example.com/alpha-beta-gamma-delta",
        snippet="Alpha beta gamma are present, but the fourth concept is not.",
    )

    assert _results_match_query(
        "alpha beta gamma delta",
        [result],
    ) is False


def test_web_relevance_does_not_trust_query_terms_in_url_path():
    result = SearchResultItem(
        title="Unrelated landing page",
        link="https://example.com/openai/responses/api",
        snippet="This content is about an unrelated product.",
    )

    assert _results_match_query(
        "OpenAI Responses API documentation",
        [result],
    ) is False


def test_web_relevance_requires_all_cjk_bigrams():
    result = SearchResultItem(
        title="苹果公园开放参观",
        link="https://example.com/park",
        snippet="苹果公园发布新的访客指南。",
    )

    assert _results_match_query("苹果公司", [result]) is False


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("故宫门票", "故宫 - 门票价格与预约"),
        ("上海旅游", "上海 · 旅游攻略"),
        ("苹果公司股票", "苹果公司 股票行情"),
    ],
)
def test_web_relevance_tolerates_cjk_word_boundaries(query, title):
    result = SearchResultItem(
        title=title,
        link="https://example.com/result",
        snippet="相关信息与说明。",
    )

    assert _results_match_query(query, [result]) is True


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("Fox News", "Fox announces new features"),
        ("United States election", "Unit state election diagram"),
    ],
)
def test_web_relevance_does_not_apply_unsafe_english_stemming(
    query,
    title,
):
    result = SearchResultItem(
        title=title,
        link="https://example.com/result",
        snippet="Unrelated material.",
    )

    assert _results_match_query(query, [result]) is False


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("苹果AI教程", "苹果 教程"),
        ("华为Mate价格", "华为手机价格"),
        ("小米SU7新闻", "小米7新闻"),
    ],
)
def test_web_relevance_requires_latin_tokens_in_mixed_cjk_query(
    query,
    title,
):
    result = SearchResultItem(
        title=title,
        link="https://example.com/result",
        snippet="相关信息与说明。",
    )

    assert _results_match_query(query, [result]) is False


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("東京ホテル", "東京ニュース"),
        ("首尔호텔", "首尔뉴스"),
    ],
)
def test_web_relevance_preserves_kana_and_hangul_in_mixed_query(
    query,
    title,
):
    result = SearchResultItem(
        title=title,
        link="https://example.com/result",
        snippet="関連情報。",
    )

    assert _results_match_query(query, [result]) is False


@pytest.mark.parametrize(
    ("query", "title", "snippet"),
    [
        (
            "OpenAI Responses API official documentation",
            "Unofficial OpenAI Responses API walkthrough",
            "A community guide to the Responses API.",
        ),
        (
            "Python latest version",
            "Python 2.7 release",
            "An archived Python version announcement.",
        ),
    ],
)
def test_web_relevance_preserves_authority_and_freshness_intent(
    query,
    title,
    snippet,
):
    result = SearchResultItem(
        title=title,
        link="https://example.com/result",
        snippet=snippet,
    )

    assert _results_match_query(query, [result]) is False


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
    elif provider == "anthropic_web":
        async def fail_request(**_kwargs):
            raise error

        monkeypatch.setattr(
            anthropic_web_module,
            "_request_anthropic_web_search",
            fail_request,
        )
        return AnthropicWebSearchEngine(
            api_base="https://api.anthropic.com",
            api_key=_SECRET_KEY,
            model="claude-opus-4-6",
        )
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
            "Official IANA information about reserved example domains.",
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
                    "The official Fox News site provides US reporting.",
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


@pytest.mark.asyncio
async def test_bing_web_filters_each_result_instead_of_trusting_the_page(
    monkeypatch,
):
    query = "IANA example domains"
    page = _bing_page(
        query,
        "".join(
            [
                _bing_result(
                    "IANA-managed example domains",
                    "https://www.iana.org/help/example-domains",
                    "IANA reserves example domains for documentation.",
                ),
                _bing_result(
                    "Unrelated calculator",
                    "https://example.net/calculator",
                    "A calculator with no connection to the query.",
                ),
            ]
        ),
    )
    factory = _FakeBingSessionFactory([_FakeBingResponse(page)])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(query)

    assert result.success
    assert [item.title for item in result.data.results] == [
        "IANA-managed example domains"
    ]
    assert result.data.total_results == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "x" * 501])
async def test_bing_web_rejects_invalid_query_without_request(
    monkeypatch,
    query,
):
    factory = _FakeBingSessionFactory([])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(query)

    assert result.success is False
    assert result.message == "Bing Web Search query is invalid"
    assert factory.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("date_range", ["day", "last_24_hours", "future"])
async def test_bing_web_rejects_unknown_date_range_without_request(
    monkeypatch,
    date_range,
):
    factory = _FakeBingSessionFactory([])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    result = await BingWebSearchEngine().search(
        "IANA example domains",
        date_range,
    )

    assert result.success is False
    assert result.message == "Bing Web Search date range is invalid"
    assert factory.requests == []


@pytest.mark.asyncio
async def test_bing_web_streaming_response_cap_fails_closed(
    monkeypatch,
):
    response = _FakeBingResponse(
        "x" * (bing_web_module._MAX_RESPONSE_BYTES + 1)
    )
    factory = _FakeBingSessionFactory([response])
    monkeypatch.setattr(bing_web_module, "AsyncSession", factory)

    with pytest.raises(bing_web_module._UntrustedBingResponse):
        await bing_web_module._request_bing(
            "https://www.bing.com/search",
            {"q": "bounded"},
        )

    assert response.closed is True
    assert factory.requests[0][1]["stream"] is True
    assert factory.requests[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/secret",
        "http://127.0.0.1/secret",
        "http://10.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://service.internal/secret",
        "https://example.com:8443/secret",
        "http://127.1/secret",
        "http://127.0.0.1./secret",
        "http://localhost./secret",
        "http://service.internal./secret",
        "http://0177.0.0.1/secret",
        "http://0x7f.0.0.1/secret",
        "http://127.0.0.1\\secret",
        "http://service.internal\\secret",
        "http://①②⑦.⓪.⓪.①/secret",
        "http://１２７.０.０.１/secret",
        "http://127。0。0。1/secret",
        "http://224.0.0.1/secret",
        "http://239.255.255.250/secret",
        "http://[ff02::1]/secret",
    ],
)
def test_search_result_url_rejects_internal_destinations(url):
    assert bing_web_module._normalize_result_url(url) == ""


def _anthropic_search_response():
    official_url = "https://fastapi.tiangolo.com/async/"
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_123",
                "name": "web_search",
                "input": {"query": "FastAPI async documentation"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_123",
                "content": [
                    {
                        "type": "web_search_result",
                        "title": "Concurrency and async / await - FastAPI",
                        "url": official_url,
                        "page_age": "July 25, 2026",
                        "encrypted_content": "encrypted-result-1",
                    },
                    {
                        "type": "web_search_result",
                        "title": "FastAPI home",
                        "url": "https://fastapi.tiangolo.com/",
                        "page_age": "July 24, 2026",
                        "encrypted_content": "encrypted-result-2",
                    },
                    {
                        "type": "web_search_result",
                        "title": "Unsafe FastAPI async result",
                        "url": "javascript:alert(1)",
                    },
                    {
                        "type": "web_search_result",
                        "title": "Duplicate FastAPI async result",
                        "url": official_url,
                    },
                ],
            },
            {
                "type": "text",
                "text": "The official documentation covers async.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": official_url,
                        "title": "Concurrency and async / await - FastAPI",
                        "cited_text": (
                            "FastAPI explains async and await concurrency."
                        ),
                    }
                ],
            },
        ],
        "usage": {
            "server_tool_use": {"web_search_requests": 1},
        },
    }


def test_anthropic_web_parser_filters_each_result_and_unsafe_urls():
    result = _parse_anthropic_web_response(
        _anthropic_search_response(),
        query="FastAPI async documentation",
        date_range=None,
    )

    assert result.total_results == 1
    assert len(result.results) == 1
    assert result.results[0].title == (
        "Concurrency and async / await - FastAPI"
    )
    assert result.results[0].link == (
        "https://fastapi.tiangolo.com/async/"
    )
    assert result.results[0].snippet == (
        "FastAPI explains async and await concurrency."
    )


@pytest.mark.asyncio
async def test_anthropic_web_search_uses_bounded_server_tool_request(
    monkeypatch,
):
    captured = {}

    async def fake_request(**kwargs):
        captured.update(kwargs)
        return _anthropic_search_response()

    monkeypatch.setattr(
        anthropic_web_module,
        "_request_anthropic_web_search",
        fake_request,
    )
    engine = AnthropicWebSearchEngine(
        api_base="https://gateway.example/v1",
        api_key="private-key",
        model="claude-opus-4-8",
        extra_headers={"x-routing": "test"},
    )

    result = await engine.search(
        "FastAPI async documentation",
    )

    assert result.success
    assert captured["url"] == "https://gateway.example/v1/messages"
    assert captured["headers"]["x-api-key"] == "private-key"
    assert captured["headers"]["x-routing"] == "test"
    payload = captured["payload"]
    assert payload["model"] == "claude-opus-4-8"
    assert payload["max_tokens"] == 1024
    assert payload["temperature"] == 0
    assert payload["tools"] == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 1,
        }
    ]
    assert "FastAPI async documentation" in (
        payload["messages"][0]["content"]
    )
    assert '"query": "FastAPI async documentation"' in (
        payload["messages"][0]["content"]
    )
    assert "date_range" not in payload["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "date_range",
    [
        "past_hour",
        "past_day",
        "past_week",
        "past_month",
        "past_year",
    ],
)
async def test_anthropic_web_search_rejects_unsupported_freshness_filter(
    monkeypatch,
    date_range,
):
    called = False

    async def unexpected_request(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(
        anthropic_web_module,
        "_request_anthropic_web_search",
        unexpected_request,
    )
    engine = AnthropicWebSearchEngine(
        api_base="https://api.anthropic.com",
        api_key="private-key",
        model="claude-opus-4-8",
    )

    result = await engine.search("breaking news", date_range)

    assert result.success is False
    assert "freshness" in result.message
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "x" * 501])
async def test_anthropic_web_search_rejects_invalid_query(
    monkeypatch,
    query,
):
    called = False

    async def unexpected_request(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(
        anthropic_web_module,
        "_request_anthropic_web_search",
        unexpected_request,
    )
    engine = AnthropicWebSearchEngine(
        api_base="https://api.anthropic.com",
        api_key="private-key",
        model="claude-opus-4-8",
    )

    result = await engine.search(query)

    assert result.success is False
    assert result.message == "Anthropic Web Search query is invalid"
    assert called is False


def test_anthropic_web_parser_requires_verified_server_tool_usage():
    response = _anthropic_search_response()
    response["usage"]["server_tool_use"]["web_search_requests"] = 0

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


@pytest.mark.parametrize("request_count", [True, 1.0, "1"])
def test_anthropic_web_parser_rejects_noninteger_usage_count(
    request_count,
):
    response = _anthropic_search_response()
    response["usage"]["server_tool_use"][
        "web_search_requests"
    ] = request_count

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_requires_matching_tool_use_id():
    response = _anthropic_search_response()
    response["content"][1]["tool_use_id"] = "srvtoolu_other"

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_rejects_extra_mismatched_result_block():
    response = _anthropic_search_response()
    response["content"].insert(
        2,
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_other",
            "content": [],
        },
    )

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_rejects_unknown_result_item_type():
    response = _anthropic_search_response()
    response["content"][1]["content"].append(
        {
            "type": "unexpected_search_result",
            "title": "FastAPI async documentation",
            "url": "https://fastapi.tiangolo.com/",
        }
    )

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_accepts_verified_empty_results():
    response = _anthropic_search_response()
    query = "obscure nonexistent phrase"
    response["content"][0]["input"]["query"] = query
    response["content"][1]["content"] = []

    results = _parse_anthropic_web_response(
        response,
        query=query,
        date_range=None,
    )

    assert results.total_results == 0
    assert results.results == []


@pytest.mark.parametrize(
    "stop_reason",
    ["pause_turn", "max_tokens", "tool_use", None],
)
def test_anthropic_web_parser_rejects_incomplete_turn(stop_reason):
    response = _anthropic_search_response()
    response["stop_reason"] = stop_reason

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_rejects_server_tool_error_object():
    response = _anthropic_search_response()
    response["content"][1]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "unavailable",
    }

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_normalizes_citation_url_for_snippet():
    response = _anthropic_search_response()
    result_url = "https://fastapi.tiangolo.com/async/#concurrency"
    response["content"][1]["content"] = [
        {
            "type": "web_search_result",
            "title": "Official reference",
            "url": result_url,
            "encrypted_content": "encrypted-result",
        }
    ]
    response["content"][2]["citations"][0]["url"] = (
        "https://fastapi.tiangolo.com/async/"
    )

    result = _parse_anthropic_web_response(
        response,
        query="FastAPI async documentation",
        date_range=None,
    )

    assert result.total_results == 1
    assert result.results[0].snippet == (
        "FastAPI explains async and await concurrency."
    )


def test_anthropic_web_parser_binds_server_query_to_request():
    response = _anthropic_search_response()
    response["content"][0]["input"]["query"] = (
        "totally unrelated request"
    )

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


def test_anthropic_web_parser_allows_concept_preserving_query_rewrite():
    response = _anthropic_search_response()
    response["content"][0]["input"]["query"] = (
        "FastAPI async docs"
    )

    result = _parse_anthropic_web_response(
        response,
        query="FastAPI async documentation",
        date_range=None,
    )

    assert result.total_results == 1


def test_anthropic_web_parser_allows_documented_natural_query_rewrite():
    response = _anthropic_search_response()
    response["content"][0]["input"]["query"] = (
        "claude shannon birth date"
    )
    response["content"][1]["content"] = [
        {
            "type": "web_search_result",
            "title": "Claude Shannon - Biography",
            "url": "https://example.com/claude-shannon",
            "encrypted_content": "encrypted-result",
        }
    ]
    response["content"][2]["citations"][0].update(
        {
            "url": "https://example.com/claude-shannon",
            "title": "Claude Shannon - Biography",
            "cited_text": (
                "Claude Shannon's birth date was April 30, 1916."
            ),
        }
    )

    result = _parse_anthropic_web_response(
        response,
        query="when Claude Shannon was born",
        date_range=None,
    )

    assert result.total_results == 1
    assert result.results[0].title == "Claude Shannon - Biography"


def test_anthropic_web_parser_rejects_one_word_unrelated_rewrite():
    response = _anthropic_search_response()
    response["content"][0]["input"]["query"] = (
        "FastAPI restaurant recommendations"
    )

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


@pytest.mark.parametrize(
    ("requested_query", "tool_query"),
    [
        (
            "FastAPI async documentation",
            "FastAPI async restaurant recommendations",
        ),
        (
            "OpenAI Responses API security vulnerabilities 2026",
            "OpenAI API restaurant recommendations",
        ),
        (
            "苹果公司新闻",
            "苹果公园旅游",
        ),
        ("苹果AI教程", "苹果教程"),
        ("华为Mate价格", "华为价格"),
        ("小米SU7新闻", "小米7新闻"),
        ("東京AIホテル", "東京ホテル"),
        ("首尔AI호텔", "首尔호텔"),
        ("electrical current safety", "electrical safety"),
        ("time series forecasting", "series forecasting"),
        ("date fruit nutrition", "fruit nutrition"),
        ("C++ reference semantics", "C++ semantics"),
        ("weather forecast model", "weather model"),
        ("Green Day band", "Green band"),
        ("weather in NYC tomorrow", "NYC weather today"),
        ("weather in NYC tomorrow", "NYC weather yesterday"),
        ("Google Docs API", "Google documentation API"),
        ("Google documentation API", "Google Docs API"),
        ("birth date astrology", "birth astrology"),
        ("weather in IN today", "weather forecast"),
        ("weather IN tomorrow", "weather forecast"),
        ("IN weather tomorrow", "weather forecast"),
        (
            "When was Born in the USA released",
            "birth date in the USA released",
        ),
        ("Rust async runtime", "Rust asyncio runtime"),
        (
            "Java asynchronous programming",
            "Java asyncio programming",
        ),
        ("US tariffs on China", "China tariffs on US"),
        ("Python безопасность", "Python"),
        ("Google الأمان", "Google"),
        ("API ασφάλεια", "API"),
        ("café Paris", "caf Paris"),
        ("iPhone ١٦", "iPhone"),
        ("F# async documentation", "F async documentation"),
        ("Q# quantum documentation", "Q quantum documentation"),
        ("price > 100", "price < 100"),
        ("price -100 filters", "price 100 filters"),
        ("temperature - 10 C", "temperature 10 C"),
        ("temperature –10 C", "temperature 10 C"),
        ("python –snake", "python snake"),
        ("budget €100", "budget $100"),
        ("Python 🐍 tutorial", "Python tutorial"),
        (
            "(cats OR dogs) AND food",
            "cats OR (dogs AND food)",
        ),
        ('"When I Was Born"', "I birth date"),
        ("'When I Was Born'", "I birth date"),
        (
            "FastAPI async vs asyncio",
            "FastAPI asyncio vs asyncio",
        ),
        (
            "FastAPI asynchronous vs async",
            "FastAPI async vs async",
        ),
        (
            "OpenAI Responses vs response API",
            "OpenAI response vs response API",
        ),
        (
            "FastAPI documentation vs docs",
            "FastAPI docs vs docs",
        ),
        ("Zoho documentation API", "Zoho Docs API"),
        (
            "ONLYOFFICE documentation API",
            "ONLYOFFICE Docs API",
        ),
        (
            "FastAPI versus Rust async runtime",
            "FastAPI versus Rust asyncio runtime",
        ),
        ("谷歌 documentation API", "谷歌 docs API"),
        (
            "Python безопасность документация",
            "Python рестораны рекомендации",
        ),
        ("OpenAI أمان API", "OpenAI مطاعم API"),
    ],
)
def test_anthropic_web_parser_rejects_topic_changing_rewrite(
    requested_query,
    tool_query,
):
    response = _anthropic_search_response()
    response["content"][0]["input"]["query"] = tool_query

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query=requested_query,
            date_range=None,
        )


@pytest.mark.parametrize(
    ("requested_query", "tool_query"),
    [
        ("OpenAI Responses API documentation", "OpenAI response API docs"),
        ("FastAPI async documentation", "FastAPI asyncio docs"),
        ("weather in NYC tomorrow", "NYC weather forecast"),
        ("Python latest version", "current Python version"),
    ],
)
def test_anthropic_web_query_binding_allows_safe_equivalents(
    requested_query,
    tool_query,
):
    assert anthropic_web_module._tool_query_matches_request(
        requested_query,
        tool_query,
    )


def test_anthropic_web_parser_requires_citation_bound_results():
    response = _anthropic_search_response()
    response["content"][2]["citations"] = []

    with pytest.raises(
        anthropic_web_module._UntrustedAnthropicWebResponse
    ):
        _parse_anthropic_web_response(
            response,
            query="FastAPI async documentation",
            date_range=None,
        )


@pytest.mark.parametrize(
    "api_base",
    [
        "http://api.anthropic.com",
        "https://user:pass@api.anthropic.com",
        "https://api.anthropic.com?route=messages",
        "https://api.anthropic.com#messages",
    ],
)
def test_anthropic_web_rejects_unsafe_api_base(api_base):
    with pytest.raises(ValueError, match="must be an HTTPS URL"):
        AnthropicWebSearchEngine(
            api_base=api_base,
            api_key="private-key",
            model="claude-opus-4-8",
        )


def test_search_engine_factory_configures_anthropic_web(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("API_BASE", "https://api.anthropic.com")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_NAME", "claude-opus-4-8")
    monkeypatch.setenv("SEARCH_PROVIDER", "anthropic_web")
    get_settings.cache_clear()
    get_search_engine.cache_clear()

    engine = get_search_engine()

    assert isinstance(engine, AnthropicWebSearchEngine)
    assert engine.url == "https://api.anthropic.com/v1/messages"
    assert engine.model == "claude-opus-4-8"
    get_settings.cache_clear()
    get_search_engine.cache_clear()


def test_anthropic_web_factory_requires_explicit_proxy_binding(
    monkeypatch,
):
    monkeypatch.setenv("API_KEY", "gateway-model-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "official-provider-key")
    monkeypatch.setenv("API_BASE", "https://gateway.example/v1")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_NAME", "claude-opus-4-8")
    monkeypatch.setenv("SEARCH_PROVIDER", "anthropic_web")
    monkeypatch.delenv(
        "ANTHROPIC_WEB_SEARCH_API_BASE",
        raising=False,
    )
    monkeypatch.delenv(
        "ANTHROPIC_WEB_SEARCH_API_KEY",
        raising=False,
    )
    get_settings.cache_clear()
    get_search_engine.cache_clear()

    assert get_search_engine() is None
    get_settings.cache_clear()
    get_search_engine.cache_clear()


def test_anthropic_web_factory_uses_dedicated_proxy_binding_only(
    monkeypatch,
):
    monkeypatch.setenv("API_KEY", "global-model-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "official-provider-key")
    monkeypatch.setenv("API_BASE", "https://gateway.example/v1")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_NAME", "claude-opus-4-8")
    monkeypatch.setenv("SEARCH_PROVIDER", "anthropic_web")
    monkeypatch.setenv(
        "ANTHROPIC_WEB_SEARCH_API_BASE",
        "https://search-gateway.example/v1",
    )
    monkeypatch.setenv(
        "ANTHROPIC_WEB_SEARCH_API_KEY",
        "dedicated-search-key",
    )
    monkeypatch.setenv(
        "EXTRA_HEADERS",
        '{"Authorization":"must-not-be-forwarded"}',
    )
    get_settings.cache_clear()
    get_search_engine.cache_clear()

    engine = get_search_engine()

    assert isinstance(engine, AnthropicWebSearchEngine)
    assert engine.url == (
        "https://search-gateway.example/v1/messages"
    )
    assert engine.headers["x-api-key"] == "dedicated-search-key"
    assert "Authorization" not in engine.headers
    get_settings.cache_clear()
    get_search_engine.cache_clear()


def test_anthropic_web_factory_defaults_to_official_api(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "official-provider-key")
    monkeypatch.delenv("API_BASE", raising=False)
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_NAME", "claude-opus-4-8")
    monkeypatch.setenv("SEARCH_PROVIDER", "anthropic_web")
    monkeypatch.delenv(
        "ANTHROPIC_WEB_SEARCH_API_BASE",
        raising=False,
    )
    monkeypatch.delenv(
        "ANTHROPIC_WEB_SEARCH_API_KEY",
        raising=False,
    )
    get_settings.cache_clear()
    get_search_engine.cache_clear()

    engine = get_search_engine()

    assert isinstance(engine, AnthropicWebSearchEngine)
    assert engine.url == "https://api.anthropic.com/v1/messages"
    get_settings.cache_clear()
    get_search_engine.cache_clear()


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
        "anthropic_web",
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
