from __future__ import annotations

from fastapi import APIRouter, Depends

from app.providers.registry import ProviderRegistry, get_provider_registry
from app.ratelimit.base import enforce_rate_limit
from app.schemas import ModelsResponse

router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    _principal=Depends(enforce_rate_limit),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> ModelsResponse:
    return ModelsResponse(data=registry.list_models())

