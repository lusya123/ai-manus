from typing import Optional

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit, tool


class PreviewToolkit(BaseToolkit):
    """Tool class for showing user-facing interactive web deliverables."""

    name: str = "preview"
    instructions: str = """
- For a user-facing interactive website, app, dashboard, prototype, game, or
  local web project, start its server on a reachable host, verify the URL, then
  call preview_show as the final presentation action before complete_step or
  deliver_result
- A browser/VNC view, screenshot, attached source file, or instruction telling
  the user to open a file manually is not a substitute for preview_show when
  the interactive webpage itself is the deliverable
- Do not use preview_show for ordinary research pages, documentation, login
  flows, or third-party pages used only by the agent; those remain browser
  tasks
"""

    def __init__(self):
        super().__init__()

    @tool(parse_docstring=True)
    async def preview_show(
        self,
        url: str,
        title: Optional[str] = None,
    ) -> ToolResult:
        """Show an interactive webpage preview to the user.

        When the user's deliverable is an interactive webpage they should
        inspect, use, or accept (for example a created or modified website,
        app, dashboard, prototype, game, or local web project), call
        preview_show as the final presentation action before complete_step or
        deliver_result. A browser/VNC view, screenshot, attached source file,
        or instruction to open a file manually does not replace this preview.
        For a local server started in the sandbox, pass its browser-accessible
        URL, such as http://localhost:3000 or http://127.0.0.1:5173.

        Do not use this for ordinary browsing, research, documentation, login
        flows, or third-party pages used only by the agent; use browser tools
        for those tasks instead.

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
