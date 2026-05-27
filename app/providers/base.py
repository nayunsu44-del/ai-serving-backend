from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedStreamChunk,
)


class AIProvider(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        """Create a non-streaming chat completion."""

    @abstractmethod
    def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        """Stream chat completion chunks."""

