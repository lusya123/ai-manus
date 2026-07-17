from fastapi import APIRouter

from app.core.config import SUPPORTED_BYOK_PROVIDERS, get_settings
from app.infrastructure.external.llm.security import provider_api_key
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.config import ClientConfigResponse, ModelOptionResponse

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/frontend", response_model=APIResponse[ClientConfigResponse])
async def get_frontend_config() -> APIResponse[ClientConfigResponse]:
    """Get frontend runtime config."""
    settings = get_settings()
    sub2api_base = (
        settings.sub2api_base_url.rstrip("/") if settings.sub2api_base_url else None
    )

    return APIResponse.success(
        ClientConfigResponse(
            auth_provider=settings.auth_provider,
            registration_enabled=settings.registration_enabled,
            sub2api_login_url=settings.sub2api_login_url,
            sub2api_console_url=settings.sub2api_console_url
            or (f"{sub2api_base}/dashboard" if sub2api_base else None),
            sub2api_marketplace_url=settings.sub2api_marketplace_url
            or (f"{sub2api_base}/model-marketplace" if sub2api_base else None),
            sub2api_use_token_url=settings.sub2api_use_token_url
            or (f"{sub2api_base}/use-token" if sub2api_base else None),
            show_github_button=settings.show_github_button,
            github_repository_url=settings.github_repository_url,
            google_analytics_id=settings.google_analytics_id,
            claw_enabled=settings.claw_enabled,
            default_model=ModelOptionResponse(
                id="system-default",
                label=settings.model_name,
                model_name=settings.model_name,
                model_provider=settings.model_provider,
                api_base=settings.api_base,
            ),
            available_models=[
                ModelOptionResponse(
                    id=model.id,
                    label=model.label,
                    model_name=model.model_name,
                    model_provider=model.model_provider,
                    api_base=model.api_base,
                )
                for model in settings.available_models
                if provider_api_key(
                    settings, model.model_provider, model.api_key
                )
            ],
            supported_byok_providers=list(SUPPORTED_BYOK_PROVIDERS),
        )
    )
