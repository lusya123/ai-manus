import base64
import ipaddress
import logging
import re
import socket
import unicodedata
from typing import Optional
from urllib.parse import (
    parse_qs,
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
_MAX_QUERY_CHARACTERS = 500
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_RESULT_TITLE_CHARACTERS = 500
_MAX_RESULT_SNIPPET_CHARACTERS = 2000
_BING_HOST = "www.bing.com"
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_GENERIC_QUERY_TERMS = {
    "a",
    "an",
    "and",
    "best",
    "documentation",
    "docs",
    "example",
    "examples",
    "find",
    "for",
    "from",
    "how",
    "in",
    "is",
    "me",
    "near",
    "of",
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
_GENERIC_CJK_TERMS = {"搜索", "网站", "查询"}
_SEPARATE_CJK_CONCEPT_TERMS = {
    "代码",
    "价格",
    "使用",
    "天气",
    "招聘",
    "教程",
    "文档",
    "新闻",
    "更新",
    "用法",
    "示例",
    "股价",
    "评价",
    "课程",
    "财报",
    "下载",
}
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
        if len(term) >= 2 or term.isdigit()
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


def _compact_east_asian(value: str) -> str:
    return "".join(
        re.findall(
            (
                r"[\u3040-\u30ff\u31f0-\u31ff"
                r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                r"\uac00-\ud7af]"
            ),
            _canonical_text(value),
        )
    )


def _query_term_groups(
    value: str,
) -> list[tuple[set[str], int, str | None]]:
    """Build independently-counted query concepts.

    English words are naturally separated concepts. An East Asian concept
    retains its full ordered Han/Kana/Hangul phrase so whitespace or
    punctuation may be ignored without turning shared fragments into false
    matches. Generic intent words are removed before grouping.
    """

    normalized = _canonical_text(value)
    normalized = normalized.replace("c++", "cplusplus").replace("c#", "csharp")
    raw_segments = re.findall(
        r"[a-z0-9]+|[^\W\d_]+",
        normalized,
        flags=re.UNICODE,
    )
    groups: list[tuple[set[str], int, str | None]] = []
    east_asian_pattern = re.compile(
        (
            r"[\u3040-\u30ff\u31f0-\u31ff"
            r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
            r"\uac00-\ud7af]"
        )
    )

    for raw_segment in raw_segments:
        if east_asian_pattern.search(raw_segment):
            remaining = raw_segment
            for generic in sorted(
                _GENERIC_CJK_TERMS,
                key=len,
                reverse=True,
            ):
                remaining = remaining.replace(generic, " ")
            for separate in sorted(
                _SEPARATE_CJK_CONCEPT_TERMS,
                key=len,
                reverse=True,
            ):
                remaining = remaining.replace(
                    separate,
                    f" {separate} ",
                )
            concept_segments = remaining.split()
        else:
            concept_segments = [raw_segment]

        for concept in concept_segments:
            terms = _text_terms(concept, drop_generic=True)
            if not terms:
                continue
            cjk_concept = _compact_east_asian(concept)
            groups.append(
                (
                    terms,
                    1,
                    cjk_concept or None,
                )
            )
    return groups


def _result_matches_query(
    query: str,
    item: SearchResultItem,
) -> bool:
    query_groups = _query_term_groups(query)
    if not query_groups:
        return False

    # Match only human-visible result text. A malicious or SEO-optimized URL
    # path can contain every query term while leading to unrelated content.
    visible_fields = (item.title, item.snippet)
    result_terms = set().union(
        *(
            _text_terms(field, drop_generic=False)
            for field in visible_fields
        )
    )
    compact_east_asian_fields = tuple(
        _compact_east_asian(field) for field in visible_fields
    )
    east_asian_pattern = re.compile(
        (
            r"[\u3040-\u30ff\u31f0-\u31ff"
            r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
            r"\uac00-\ud7af]"
        )
    )
    return all(
        (
            any(
                cjk_concept in compact_field
                for compact_field in compact_east_asian_fields
            )
            and all(
                term in result_terms
                for term in group_terms
                if not east_asian_pattern.search(term)
            )
            if cjk_concept
            else (
                len(group_terms.intersection(result_terms))
                >= required_within_group
            )
        )
        for (
            group_terms,
            required_within_group,
            cjk_concept,
        ) in query_groups
    )


def _filter_results_matching_query(
    query: str,
    search_results: list[SearchResultItem],
) -> list[SearchResultItem]:
    """Return only individually relevant results, preserving source order."""
    return [
        item
        for item in search_results
        if _result_matches_query(query, item)
    ]


def _results_match_query(
    query: str, search_results: list[SearchResultItem]
) -> bool:
    return bool(_filter_results_matching_query(query, search_results[:10]))


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
    if (
        "/ck/a?" in candidate
        or "\\" in candidate
        or any(ord(character) < 0x20 for character in candidate)
    ):
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

    try:
        # IDNA performs compatibility normalization, including full-width or
        # circled digits and non-ASCII dot separators. Canonicalize first and
        # then run every IP check on the exact ASCII host that clients use.
        hostname = (
            hostname_value.lower()
            .encode("idna")
            .decode("ascii")
            .rstrip(".")
        )
    except UnicodeError:
        return ""
    if not hostname:
        return ""

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is None:
        try:
            # inet_aton also recognizes legacy IPv4 forms such as 127.1,
            # octal components, and hexadecimal components.
            address = ipaddress.ip_address(socket.inet_aton(hostname))
        except (OSError, ValueError):
            address = None
    if address is not None:
        if (
            not address.is_global
            or address.is_multicast
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
            or getattr(address, "is_site_local", False)
        ):
            return ""
    else:
        labels = hostname.split(".")
        if (
            len(labels) < 2
            or any(
                not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    label,
                )
                for label in labels
            )
            or hostname == "localhost"
            or hostname.endswith(
                (
                    ".localhost",
                    ".local",
                    ".internal",
                    ".lan",
                    ".home",
                )
            )
        ):
            return ""
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    if port is not None and port != default_port:
        return ""
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
        response = await session.get(
            base_url,
            params=params,
            timeout=30,
            stream=True,
            allow_redirects=False,
        )
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_RESPONSE_BYTES:
                    await response.aclose()
                    raise _UntrustedBingResponse()
            except ValueError:
                pass

        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in response.aiter_content():
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESPONSE_BYTES:
                await response.aclose()
                raise _UntrustedBingResponse()
            chunks.append(chunk)
        response.content = b"".join(chunks)
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
                    title = anchor.get_text(" ", strip=True)[
                        :_MAX_RESULT_TITLE_CHARACTERS
                    ]
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
                    snippet = text[
                        :_MAX_RESULT_SNIPPET_CHARACTERS
                    ]
                    break

            if not snippet:
                for paragraph in item.find_all("p"):
                    text = paragraph.get_text(" ", strip=True)
                    if len(text) > 20:
                        snippet = text[
                            :_MAX_RESULT_SNIPPET_CHARACTERS
                        ]
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

    search_results = _filter_results_matching_query(query, search_results)
    if not search_results:
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
        normalized_query = query.strip()
        if (
            not normalized_query
            or len(normalized_query) > _MAX_QUERY_CHARACTERS
        ):
            return ToolResult(
                success=False,
                message="Bing Web Search query is invalid",
                data=SearchResults(
                    query=query,
                    date_range=date_range,
                    total_results=0,
                    results=[],
                ),
            )

        freshness_filters = {
            "past_hour": 'ex1:"ez1"',
            "past_day": 'ex1:"ez2"',
            "past_week": 'ex1:"ez3"',
            "past_month": 'ex1:"ez4"',
            "past_year": 'ex1:"ez5"',
        }
        if date_range not in (None, "", "all", *freshness_filters):
            return ToolResult(
                success=False,
                message="Bing Web Search date range is invalid",
                data=SearchResults(
                    query=query,
                    date_range=date_range,
                    total_results=0,
                    results=[],
                ),
            )

        params: dict[str, str] = {
            "q": normalized_query,
            "count": "20",
            "mkt": self.market,
            "setlang": self.setlang,
        }

        if date_range and date_range != "all":
            params["filters"] = freshness_filters[date_range]

        last_error: Exception | None = None
        saw_untrusted_response = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await _request_bing(self.base_url, params)
                results = _parse_bing_response(
                    response,
                    query=normalized_query,
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
