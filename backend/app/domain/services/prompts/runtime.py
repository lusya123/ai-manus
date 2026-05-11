from typing import Any

from app.core.config import get_settings


def _line(label: str, value: Any) -> str:
    return f"- {label}: `{value}`" if value else f"- {label}: not configured"


def build_runtime_environment_prompt(sandbox: Any = None) -> str:
    """Build deployment/runtime context that can change per environment."""
    settings = get_settings()

    sandbox_id = getattr(sandbox, "id", None) if sandbox else None
    sandbox_api_url = getattr(sandbox, "base_url", None) if sandbox else None
    sandbox_cdp_url = getattr(sandbox, "cdp_url", None) if sandbox else None
    sandbox_vnc_url = getattr(sandbox, "vnc_url", None) if sandbox else None

    lines = [
        "<runtime_environment>",
        "Deployment:",
        _line("Environment", settings.deployment_environment),
        _line("Docker/container network", settings.sandbox_network),
        _line("Host gateway from containers", settings.host_gateway_url),
        "",
        "Service URLs and ports:",
        _line("Frontend public URL for the user/local browser", settings.frontend_public_url),
        _line("Backend public URL for the user/local browser", settings.backend_public_url),
        _line("Frontend internal/container URL", settings.frontend_internal_url),
        _line("Backend internal/container URL", settings.backend_internal_url),
        _line("Frontend URL reachable from sandbox browser/shell", settings.frontend_sandbox_url),
        _line("Backend URL reachable from sandbox browser/shell", settings.backend_sandbox_url),
        _line("Claw public URL", settings.claw_public_url),
        _line("Claw internal/container URL", settings.claw_internal_url),
        "",
        "Current sandbox:",
        _line("Sandbox ID", sandbox_id),
        _line("Sandbox API URL used by backend", sandbox_api_url),
        _line("Sandbox API port", settings.sandbox_api_port),
        _line("Sandbox Chrome CDP URL", sandbox_cdp_url),
        _line("Sandbox Chrome CDP port", settings.sandbox_cdp_port),
        _line("Sandbox VNC URL", sandbox_vnc_url),
        _line("Sandbox VNC port", settings.sandbox_vnc_port),
        "",
        "Networking rules:",
        "- Do not assume `localhost` or `127.0.0.1` means the host machine. Inside a container or sandbox, it usually means that same container.",
        "- When testing this project's frontend/backend from inside the sandbox browser or shell, prefer the configured sandbox-reachable URLs above.",
        "- When telling the user what to open on their own machine, prefer the configured public URLs above.",
        "- If a configured URL is missing or fails, verify the actual listener and port with tools before guessing.",
        "- For cloud deployment, trust the configured public/internal URLs for that environment instead of local development defaults.",
        "</runtime_environment>",
    ]
    return "\n".join(lines)
