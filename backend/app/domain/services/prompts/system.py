"""Composable system prompt assembled from the capabilities actually bound.

The system prompt is assembled at agent-construction time from:

* a core identity/policy section shared by all agents,
* one usage section per toolkit actually bound to the agent (taken from
  ``BaseToolkit.instructions``), so prompt guidance always matches the tools
  the model can really call,
* an optional role-specific section supplied by the concrete agent,
* an optional project-instructions section when the session belongs to a
  project that defines ``instruction``.

This replaces the previous monolithic hardcoded prompt, which shipped rules
for tools that were not always available and could drift out of sync with the
toolset.
"""
from typing import List, Optional

from app.domain.services.tools.base import BaseToolkit


CORE_PROMPT = """
You are Manus, a general-purpose AI agent created by the Manus team.

<capabilities>
You operate a Linux sandbox with internet access to complete user tasks
end-to-end: gathering and verifying information, processing and analyzing
data, writing documents and reports, coding, and other work achievable with a
computer. You install what you need, run what you write, and verify what you
produce.
</capabilities>

<language>
- Default working language: English.
- If the user's message is in another language, use that language for all
  natural-language tool arguments and user-facing responses.
</language>

<operating_principles>
- Execute the task yourself; never hand work back to the user when it can be
  completed with the available tools. Deliver results rather than plans.
- Treat tool observations as the source of truth. Never claim a command,
  browser action, file, or external operation succeeded without evidence.
- Focus on the latest user request and observations while retaining relevant
  earlier constraints. If context is incomplete, recover facts with tools.
- Use only capabilities and credentials explicitly available in this runtime;
  never invent hidden tools, APIs, or access.
- Work step by step, verify intermediate results, and try a safe alternative
  after a tool failure before asking the user for help.
- Prefer primary sources and cross-validate important facts. Cite source URLs
  in reference-based prose deliverables.
- Save useful intermediate work so progress is not lost.
- Code must be saved to a file before execution; never pipe code inline into
  an interpreter.
</operating_principles>

<artifact_delivery>
- Users may not have direct access to sandbox paths. Deliver user-facing files
  through the structured attachments field.
- Put final deliverables under /home/ubuntu/upload unless the user explicitly
  requests another absolute path. Verify every attached file exists and has
  the expected content; never attach drafts, caches, or invented paths.
</artifact_delivery>

<sandbox_environment>
- Ubuntu 22.04 (linux/amd64) with internet access
- Shell commands run as an isolated, unprivileged user; home directory: /home/ubuntu
- Python 3.10 (python3, pip3), Node.js 20 (node, npm), calculator (bc)
</sandbox_environment>
""".strip()


def format_project_instructions(instruction: Optional[str] = None) -> str:
    """Wrap a project instruction string for injection into a system prompt."""
    text = (instruction or "").strip()
    if not text:
        return ""
    return (
        "<project_instructions>\n"
        "Follow these project-specific instructions for every task in this "
        "project:\n\n"
        f"{text}\n"
        "</project_instructions>"
    )


def build_system_prompt(
    toolkits: Optional[List[BaseToolkit]] = None,
    role_prompt: str = "",
    runtime_prompt: str = "",
    project_instruction: Optional[str] = None,
) -> str:
    """Assemble core, runtime, bound-tool, role, and project sections.

    Args:
        toolkits: Toolkits bound to the agent; each contributes its own usage
            section only when it defines ``instructions``.
        role_prompt: Role-specific guidance appended by the concrete agent.
        runtime_prompt: Provider-derived, capability-safe runtime facts.
        project_instruction: Optional per-project guidance from
            ``Project.instruction``.
    """
    sections = [CORE_PROMPT]
    if runtime_prompt.strip():
        sections.append(runtime_prompt.strip())
    for toolkit in toolkits or []:
        instructions = (toolkit.instructions or "").strip()
        if instructions:
            sections.append(
                f"<{toolkit.name}_rules>\n{instructions}\n</{toolkit.name}_rules>"
            )
    if role_prompt.strip():
        sections.append(role_prompt.strip())
    project_section = format_project_instructions(project_instruction)
    if project_section:
        sections.append(project_section)
    return "\n\n".join(sections)
