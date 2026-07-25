import base64
import logging
import re
import unicodedata
from typing import Optional
from urllib.parse import (
    parse_qs,
    unquote,
    urlparse,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from app.domain.external.search import SearchEngine
from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_BING_HOST = "www.bing.com"
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_GENERIC_QUERY_TERMS = {
    "a",
    "an",
    "and",
    "best",
    "current",
    "documentation",
    "docs",
    "find",
    "for",
    "from",
    "how",
    "in",
    "is",
    "latest",
    "me",
    "near",
    "of",
    "official",
    "on",
    "search",
    "site",
    "the",
    "to",
    "use",
    "used",
    "website",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
}
_GENERIC_CJK_TERMS = {"官网", "官方", "搜索", "网站", "最新", "查询"}
_NO_RESULTS_PHRASES = (
    "there are no results for",
    "no results found for",
    "we couldn't find any results",
    "we could not find any results",
    "check your spelling or try different keywords",
)
_CHALLENGE_PHRASES = (
    "verify you are human",
    "unusual traffic",
    "complete the challenge",
)


class _UntrustedBingResponse(RuntimeError):
    """A successful HTTP response that cannot be trusted as search output."""


def _canonical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _text_terms(value: str, *, drop_generic: bool) -> set[str]:
    normalized = _canonical_text(value)
    normalized = normalized.replace("c++", "cplusplus").replace("c#", "csharp")

    terms = {
        term
        for term in re.findall(r"[a-z0-9]+", normalized)
        if len(term) >= 2
    }
    terms.update(
        term
        for term in re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
        if len(term) >= 2
        and not re.fullmatch(r"[a-z]+", term)
        and not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", term)
    )

    for sequence in re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", normalized
    ):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )

    if drop_generic:
        terms.difference_update(_GENERIC_QUERY_TERMS)
        terms.difference_update(_GENERIC_CJK_TERMS)
    return terms


def _query_term_groups(value: str) -> list[tuple[set[str], int]]:
    """Build independently-counted query concepts.

    English words are naturally separated concepts. A contiguous CJK phrase
    needs overlapping bigrams for matching, but those bigrams must never cast
    multiple votes toward the *overall* query threshold. Generic intent words
    are removed before grouping so ``苹果公司 最新新闻`` becomes the two
    concepts ``苹果公司`` and ``新闻`` rather than a pile of correlated
    n-grams.
    """

    normalized = _canonical_text(value)
    normalized = normalized.replace("c++", "cplusplus").replace("c#", "csharp")
    raw_segments = re.findall(
        r"[a-z0-9]+|[^\W\d_]+",
        normalized,
        flags=re.UNICODE,
    )
    groups: list[tuple[set[str], int]] = []
    cjk_pattern = re.compile(
        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
    )

    for raw_segment in raw_segments:
        if cjk_pattern.search(raw_segment):
            remaining = raw_segment
            for generic in sorted(
                _GENERIC_CJK_TERMS,
                key=len,
                reverse=True,
            ):
                remaining = remaining.replace(generic, " ")
            concept_segments = remaining.split()
        else:
            concept_segments = [raw_segment]

        for concept in concept_segments:
            terms = _text_terms(concept, drop_generic=True)
            if not terms:
                continue
            # One CJK concept may be written without spaces. Requiring roughly
            # two-thirds of its bigrams allows ``北京 天气`` to match
            # ``北京天气`` while rejecting an entity-only result for
            # ``苹果公司新闻``.
            if cjk_pattern.search(concept) and len(terms) > 1:
                required_within_group = max(
                    2,
                    (2 * len(terms) + 2) // 3,
                )
            else:
                required_within_group = 1
            groups.append((terms, required_within_group))
    return groups


def _results_match_query(
    query: str, search_results: list[SearchResultItem]
) -> bool:
    query_groups = _query_term_groups(query)
    if not query_groups:
        return False

    # One incidental word such as "example", "api", or the Chinese bigram
    # "公司" is not enough to trust an otherwise unrelated 200 response. Each
    # independently separated concept contributes at most one vote, so the
    # overlapping bigrams of one Chinese entity cannot satisfy another concept
    # such as "新闻". All required concepts must still co-occur in one result.
    required_group_matches = (
        1
        if len(query_groups) == 1
        else max(2, (len(query_groups) + 1) // 2)
    )
    for item in search_results[:10]:
        parsed_link = urlsplit(item.link)
        link_text = f"{parsed_link.hostname or ''} {unquote(parsed_link.path)}"
        result_terms = _text_terms(
            f"{item.title} {link_text} {item.snippet}",
            drop_generic=False,
        )
        matched_groups = sum(
            len(group_terms.intersection(result_terms))
            >= required_within_group
            for group_terms, required_within_group in query_groups
        )
        if matched_groups >= required_group_matches:
            return True
    return False


def _decode_bing_redirect(url: str) -> str:
    """Extract the real destination URL from a Bing /ck/a tracking redirect."""
    try:
        parsed = urlparse(url)
        if (
            parsed.hostname is None
            or not parsed.hostname.lower().endswith(".bing.com")
            or parsed.path != "/ck/a"
        ):
            return url
        u_values = parse_qs(parsed.query).get("u", [])
        if u_values and u_values[0].startswith("a1"):
            encoded = u_values[0][2:]
            encoded += "=" * (-len(encoded) % 4)
            return base64.urlsafe_b64decode(encoded).decode("utf-8")
    except Exception:
        pass
    return url


def _normalize_result_url(url: str) -> str:
    candidate = _decode_bing_redirect(url) if "/ck/a?" in url else url
    if "/ck/a?" in candidate:
        return ""

    try:
        parsed = urlsplit(candidate)
        hostname_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname_value
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""

    hostname = hostname_value.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is not None:
        hostname = f"{hostname}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            hostname,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _is_challenge_page(soup: BeautifulSoup) -> bool:
    if soup.select_one(
        "#b_captcha, .b_captcha, form[action*='captcha'], "
        "input[name*='captcha'], #challenge-form"
    ):
        return True
    page_text = _canonical_text(soup.get_text(" ", strip=True))
    return any(phrase in page_text for phrase in _CHALLENGE_PHRASES)


def _is_explicit_no_results(soup: BeautifulSoup) -> bool:
    results_root = soup.select_one("#b_results")
    if not results_root:
        return False
    if results_root.select_one(".b_no, li.b_no"):
        return True
    results_text = _canonical_text(results_root.get_text(" ", strip=True))
    return any(phrase in results_text for phrase in _NO_RESULTS_PHRASES)


async def _request_bing(
    base_url: str,
    params: dict[str, str],
):
    async with AsyncSession(impersonate="chrome") as session:
        response = await session.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        return response


def _parse_bing_response(
    response,
    *,
    query: str,
    date_range: Optional[str],
) -> SearchResults:
    response_url = urlparse(str(response.url))
    response_host = (response_url.hostname or "").lower()
    if (
        response_url.scheme != "https"
        or (
            response_host != _BING_HOST
            and not response_host.endswith(".bing.com")
        )
    ):
        raise _UntrustedBingResponse()

    content_type = (
        response.headers.get("content-type", "")
        .partition(";")[0]
        .strip()
        .lower()
    )
    if content_type not in _HTML_CONTENT_TYPES:
        raise _UntrustedBingResponse()

    soup = BeautifulSoup(response.text, "html.parser")
    if _is_challenge_page(soup):
        raise _UntrustedBingResponse()

    query_input = soup.select_one("input[name='q']")
    echoed_query = (
        query_input.get("value") if query_input is not None else None
    )
    if (
        not isinstance(echoed_query, str)
        or _canonical_text(echoed_query) != _canonical_text(query)
    ):
        raise _UntrustedBingResponse()

    search_results: list[SearchResultItem] = []
    seen_links: set[str] = set()
    for item in soup.find_all("li", class_="b_algo")[:20]:
        try:
            title, link = "", ""

            h2 = item.find("h2")
            if h2:
                anchor = h2.find("a")
                if anchor:
                    title = anchor.get_text(" ", strip=True)
                    href = anchor.get("href", "")
                    if isinstance(href, str):
                        link = _normalize_result_url(href)

            if not title or not link or link in seen_links:
                continue

            snippet = ""
            for tag in item.find_all(
                ["p", "div"],
                class_=re.compile(
                    r"b_lineclamp|b_descript|b_caption|b_paractl"
                ),
            ):
                text = tag.get_text(" ", strip=True)
                if len(text) > 20:
                    snippet = text
                    break

            if not snippet:
                for paragraph in item.find_all("p"):
                    text = paragraph.get_text(" ", strip=True)
                    if len(text) > 20:
                        snippet = text
                        break

            search_results.append(
                SearchResultItem(
                    title=title,
                    link=link,
                    snippet=snippet,
                )
            )
            seen_links.add(link)
        except Exception as e:
            logger.warning(
                "Failed to parse Bing search result item: %s",
                safe_exception_summary(e),
            )

    if not search_results:
        if _is_explicit_no_results(soup):
            return SearchResults(
                query=query,
                date_range=date_range,
                total_results=0,
                results=[],
            )
        raise _UntrustedBingResponse()

    if not _results_match_query(query, search_results):
        raise _UntrustedBingResponse()

    total_results = 0
    for elem in soup.find_all(
        ["span", "div"],
        class_=re.compile(r"sb_count|b_focusTextMedium"),
    ):
        match = re.search(
            r"([\d,]+)\s*results?", elem.get_text(" ", strip=True)
        )
        if match:
            try:
                total_results = int(match.group(1).replace(",", ""))
                break
            except ValueError:
                continue

    return SearchResults(
        query=query,
        date_range=date_range,
        total_results=total_results or len(search_results),
        results=search_results,
    )


class BingWebSearchEngine(SearchEngine):
    """Bing search engine implementation using web scraping with browser impersonation"""

    def __init__(
        self,
        market: str = "en-US",
        setlang: str = "en",
    ):
        self.base_url = "https://www.bing.com/search"
        self.market = market.strip() or "en-US"
        self.setlang = setlang.strip() or "en"

    async def search(
        self,
        query: str,
        date_range: Optional[str] = None,
    ) -> ToolResult[SearchResults]:
        """Search web pages by scraping Bing search results.

        Args:
            query: Search query, using 3-5 keywords
            date_range: (Optional) Time range filter for search results

        Returns:
            Search results
        """
        params: dict[str, str] = {
            "q": query,
            "count": "20",
            "mkt": self.market,
            "setlang": self.setlang,
        }

        if date_range and date_range != "all":
            freshness_filters = {
                "past_hour": 'ex1:"ez1"',
                "past_day": 'ex1:"ez2"',
                "past_week": 'ex1:"ez3"',
                "past_month": 'ex1:"ez4"',
                "past_year": 'ex1:"ez5"',
            }
            f = freshness_filters.get(date_range)
            if f:
                params["filters"] = f

        last_error: Exception | None = None
        saw_untrusted_response = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await _request_bing(self.base_url, params)
                results = _parse_bing_response(
                    response,
                    query=query,
                    date_range=date_range,
                )
                return ToolResult(success=True, data=results)
            except _UntrustedBingResponse:
                saw_untrusted_response = True
                logger.warning(
                    "Bing Web Search received an untrusted response "
                    "(attempt %d/%d)",
                    attempt,
                    _MAX_ATTEMPTS,
                )
            except Exception as e:
                last_error = e
                if attempt < _MAX_ATTEMPTS:
                    logger.warning(
                        "Bing Web Search attempt failed: %s",
                        safe_exception_summary(e),
                    )

        if last_error is not None and not saw_untrusted_response:
            error_summary = safe_exception_summary(last_error)
            logger.error(
                "Bing Web Search failed: %s", error_summary
            )
            error_results = SearchResults(
                query=query,
                date_range=date_range,
                total_results=0,
                results=[],
            )
            return ToolResult(
                success=False,
                message=f"Bing Web Search failed: {error_summary}",
                data=error_results,
            )

        logger.error(
            "Bing Web Search failed after receiving untrusted responses"
        )
        error_results = SearchResults(
            query=query,
            date_range=date_range,
            total_results=0,
            results=[],
        )
        return ToolResult(
            success=False,
            message="Bing Web Search returned an untrusted response",
            data=error_results,
        )


if __name__ == "__main__":
    import asyncio

    async def test():
        engine = BingWebSearchEngine()
        result = await engine.search("Python programming")

        if result.success:
            print(f"Found {len(result.data.results)} results")
            for i, item in enumerate(result.data.results[:5]):
                print(f"{i + 1}. {item.title}")
                print(f"   {item.link}")
                print(f"   {item.snippet[:100]}")
                print()
        else:
            print(f"Search failed: {result.message}")

    asyncio.run(test())
