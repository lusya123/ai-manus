from types import SimpleNamespace

import pytest

from app.domain.models.mcp_config import MCPConfig
from app.domain.services.tools.mcp import MCPClientManager
from app.infrastructure.external.browser.browser_use_browser import (
    BrowserUseBrowser,
)
from app.infrastructure.external.browser.playwright_browser import (
    PlaywrightBrowser,
)


class _Page:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.close_count = 0
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.close_count += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("signed-cdp-url=must-not-escape")
        self.closed = True


class _Browser:
    def __init__(self, pages, *, failures: int = 0) -> None:
        self.contexts = [SimpleNamespace(pages=pages)]
        self.failures = failures
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("signed-browser-url=must-not-escape")


class _Playwright:
    def __init__(self) -> None:
        self.stop_count = 0

    async def stop(self) -> None:
        self.stop_count += 1


def _playwright_adapter(page: _Page, browser: _Browser, playwright):
    adapter = object.__new__(PlaywrightBrowser)
    adapter.page = page
    adapter.browser = browser
    adapter.playwright = playwright
    adapter._cleanup_pages = []
    return adapter


async def test_playwright_cleanup_retries_failed_page_without_double_closing_layers():
    page = _Page(failures=1)
    browser = _Browser([page])
    playwright = _Playwright()
    adapter = _playwright_adapter(page, browser, playwright)

    with pytest.raises(RuntimeError) as caught:
        await adapter.cleanup()

    assert "must-not-escape" not in str(caught.value)
    assert page.close_count == 1
    assert browser.close_count == 1
    assert playwright.stop_count == 1
    assert adapter.page is page
    assert adapter.browser is None
    assert adapter.playwright is None

    await adapter.cleanup()
    await adapter.cleanup()

    assert page.close_count == 2
    assert browser.close_count == 1
    assert playwright.stop_count == 1
    assert adapter.page is None
    assert adapter._cleanup_pages == []


async def test_playwright_cleanup_retries_only_failed_parent_layer():
    page = _Page()
    browser = _Browser([page], failures=1)
    playwright = _Playwright()
    adapter = _playwright_adapter(page, browser, playwright)

    with pytest.raises(RuntimeError):
        await adapter.cleanup()

    assert page.close_count == 1
    assert browser.close_count == 1
    assert playwright.stop_count == 1
    assert adapter.page is None
    assert adapter.browser is browser
    assert adapter.playwright is None

    await adapter.cleanup()
    await adapter.cleanup()

    assert page.close_count == 1
    assert browser.close_count == 2
    assert playwright.stop_count == 1


class _BrowserUseSession:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.stop_count = 0

    async def stop(self) -> None:
        self.stop_count += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("signed-session-url=must-not-escape")


async def test_browser_use_cleanup_retains_failure_then_is_idempotent():
    session = _BrowserUseSession(failures=1)
    adapter = object.__new__(BrowserUseBrowser)
    adapter._session = session

    with pytest.raises(RuntimeError) as caught:
        await adapter.cleanup()

    assert "must-not-escape" not in str(caught.value)
    assert adapter._session is session

    await adapter.cleanup()
    await adapter.cleanup()

    assert session.stop_count == 2
    assert adapter._session is None


class _RetryingExitStack:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        if self.close_count == 1:
            raise ConnectionError("mcp-token=must-not-escape")


async def test_mcp_cleanup_propagates_safely_and_preserves_state_for_retry():
    manager = MCPClientManager(MCPConfig(mcpServers={}))
    stack = _RetryingExitStack()
    manager._exit_stack = stack
    manager._clients["server"] = object()
    manager._tools_cache["server"] = []
    manager._initialized = True

    with pytest.raises(RuntimeError) as caught:
        await manager.cleanup()

    assert "must-not-escape" not in str(caught.value)
    assert manager._clients
    assert manager._initialized is True

    await manager.cleanup()

    assert stack.close_count == 2
    assert manager._clients == {}
    assert manager._tools_cache == {}
    assert manager._initialized is False
