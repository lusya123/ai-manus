"""Per-agent LLM gateway construction.

Unlike ``get_llm()``, which remains a backwards-compatible process singleton,
this factory creates a gateway from the persisted Agent model.  It is used by
the shared task-runner factory in both local and Celery execution paths.
"""
from functools import lru_cache
import logging
from typing import Optional

from app.core.config import Settings, get_settings
from app.domain.external.llm import LLM, LLMFactory
from app.domain.models.agent import Agent
from app.infrastructure.external.llm.langchain_llm import LangchainLLM
from app.infrastructure.external.llm.openai_llm import OpenAILLM
from app.infrastructure.external.llm.anthropic_llm import AnthropicLLM
from app.infrastructure.external.llm.security import (
    provider_api_base,
    provider_api_key,
    resolve_public_model_endpoint,
)

logger = logging.getLogger(__name__)


class ConfigurableLLMFactory(LLMFactory):
    """Create the configured gateway with optional per-agent credentials."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or get_settings()

    def _settings_for_agent(self, agent: Optional[Agent]) -> Settings:
        if agent is None:
            return self._settings

        if agent.is_byok:
            if not all((agent.model_name, agent.model_provider, agent.api_base, agent.api_key)):
                raise RuntimeError("Persisted BYOK model configuration is incomplete")
            # Re-resolve persisted hostnames whenever a client is constructed.
            # This catches DNS changes and tampered records instead of trusting
            # validation performed only at session creation time.
            resolved_endpoint = resolve_public_model_endpoint(agent.api_base)
            return self._settings.model_copy(
                update={
                    "model_name": agent.model_name,
                    "model_provider": agent.model_provider,
                    "api_base": resolved_endpoint.url,
                    # Pin one of the validated answers into the actual socket
                    # transport; the HTTP client must not resolve DNS again.
                    "byok_pinned_ip": resolved_endpoint.addresses[0],
                    "api_key": agent.api_key,
                    # A BYOK Anthropic key must not be shadowed by the
                    # deployment's ANTHROPIC_API_KEY.
                    "anthropic_api_key": None,
                    "extra_headers": None,
                    "temperature": agent.temperature,
                    "max_tokens": agent.max_tokens,
                }
            )

        if agent.model_id:
            selected = next(
                (
                    option
                    for option in self._settings.available_models
                    if option.id == agent.model_id
                ),
                None,
            )
            if selected is None:
                raise RuntimeError(
                    f"Configured model option is no longer available: {agent.model_id}"
                )
            provider = selected.model_provider.lower()
            api_base = provider_api_base(
                self._settings, provider, selected.api_base
            )
            api_key = provider_api_key(
                self._settings, provider, selected.api_key
            )
            model_name = selected.model_name
        else:
            # A non-BYOK record without a catalog model_id is the deployment
            # default. Never combine the current server key with a persisted
            # legacy provider/base/model tuple: an old endpoint may have been
            # retired, reassigned, or compromised since the record was saved.
            provider = self._settings.model_provider.lower()
            api_base = provider_api_base(self._settings, provider)
            api_key = provider_api_key(self._settings, provider)
            model_name = self._settings.model_name

        if not api_key:
            raise RuntimeError(f"No API key is configured for model provider: {provider}")
        return self._settings.model_copy(
            update={
                "model_name": model_name,
                "model_provider": provider,
                "api_base": api_base,
                "api_key": api_key,
                "anthropic_api_key": api_key if provider == "anthropic" else None,
                "temperature": agent.temperature,
                "max_tokens": agent.max_tokens,
            }
        )

    def create(self, agent: Optional[Agent] = None) -> LLM:
        settings = self._settings_for_agent(agent)
        if agent is not None and agent.is_byok:
            if settings.model_provider.lower() == "anthropic":
                return AnthropicLLM(settings=settings)
            # Supported non-Anthropic BYOK providers use OpenAI-compatible
            # chat completions. The native adapter accepts our pinned client.
            return OpenAILLM(settings=settings)
        provider = (settings.llm_provider or "langchain").lower()
        if provider == "openai" and settings.model_provider.lower() != "anthropic":
            return OpenAILLM(settings=settings)
        if provider == "openai":
            logger.info(
                "Using LangChain gateway because native Anthropic is not "
                "OpenAI-wire-compatible"
            )
            return LangchainLLM(settings=settings)
        if provider != "langchain":
            logger.warning(
                "Unknown LLM_PROVIDER '%s', falling back to 'langchain'", provider
            )
        return LangchainLLM(settings=settings)


@lru_cache()
def get_llm_factory() -> ConfigurableLLMFactory:
    return ConfigurableLLMFactory()
