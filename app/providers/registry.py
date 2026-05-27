from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.errors import UnsupportedModelError
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import AIProvider
from app.providers.openai_provider import OpenAIProvider
from app.schemas import ModelInfo


@dataclass(frozen=True)
class ModelRoute:
    id: str
    provider_name: str
    owned_by: str


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers: dict[str, AIProvider] = {
            "openai": OpenAIProvider(settings.openai_api_key),
            "anthropic": AnthropicProvider(
                settings.anthropic_api_key,
                settings.default_max_tokens,
            ),
        }
        self._model_providers: dict[str, str] = {
            model: "openai" for model in settings.openai_models
        }
        self._model_providers.update(
            {model: "anthropic" for model in settings.anthropic_models}
        )

    def provider_for_model(self, model: str) -> AIProvider:
        provider_name = self._model_providers.get(model)
        if provider_name is None:
            raise UnsupportedModelError(f"Model not found: {model}")
        return self._providers[provider_name]

    def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        models.extend(
            ModelInfo(id=model, owned_by="openai") for model in self._settings.openai_models
        )
        models.extend(
            ModelInfo(id=model, owned_by="anthropic")
            for model in self._settings.anthropic_models
        )
        return models


def get_provider_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry
