from typing import Optional

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit
from langchain.tools import tool


class PreviewToolkit(BaseToolkit):
    """Tool class for showing user-facing interactive web deliverables."""

    name: str = "preview"

    def __init__(self):
        super().__init__()

    @tool(parse_docstring=True)
    async def preview_show(
        self,
        url: str,
        title: Optional[str] = None,
    ) -> ToolResult:
        """Show an interactive webpage preview to the user.

        Use this only when the user's outcome is an interactive webpage they
        should personally inspect, use, or accept, such as a created/modified
        website, app, dashboard, prototype, game, or local project preview.
        Do not use it for ordinary browsing, research, reading documentation,
        logging in, checking a third-party page, or pages that only the agent
        needs to inspect with browser tools. For local dev servers started in
        the sandbox, pass the browser-accessible local URL, such as
        http://localhost:3000 or http://127.0.0.1:5173.

        Args:
            url: URL of the user-facing webpage or web app to preview.
            title: Optional short title to display above the preview.
        """
        return ToolResult(
            success=True,
            message="OK",
            data={
                "url": url,
                "title": title,
            },
        )
