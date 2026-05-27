from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NormalizedMessage(BaseModel):
    role: str
    content: str


class NormalizedChatRequest(BaseModel):
    """Internal provider-agnostic chat request."""

    model: str
    messages: list[NormalizedMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    ignored_fields: list[str] = Field(default_factory=list)


class NormalizedUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class NormalizedChatResponse(BaseModel):
    """Internal provider-agnostic non-streaming chat response."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    model: str
    message: NormalizedMessage
    finish_reason: str | None = None
    usage: NormalizedUsage = Field(default_factory=NormalizedUsage)


class NormalizedStreamChunk(BaseModel):
    """Internal provider-agnostic streaming chunk."""

    id: str | None = None
    model: str | None = None
    role: str | None = None
    delta: str = ""
    finish_reason: str | None = None
    usage: NormalizedUsage | None = None

