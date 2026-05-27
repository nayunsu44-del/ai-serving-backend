from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
)


SupportedRole = Literal["system", "user", "assistant"]


def _settings_from_context(info: ValidationInfo):
    if isinstance(info.context, dict) and info.context.get("settings") is not None:
        return info.context["settings"]

    from app.config import Settings

    return Settings()


class ChatMessage(BaseModel):
    role: SupportedRole
    content: str

    @field_validator("content")
    @classmethod
    def validate_content_length(cls, value: str, info: ValidationInfo) -> str:
        settings = _settings_from_context(info)
        if len(value) > settings.max_message_chars:
            raise ValueError(
                f"String should have at most {settings.max_message_chars} characters"
            )
        return value


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible subset for chat completions.

    Supported fields: model, messages, stream, temperature, max_tokens.
    Additional fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)

    @field_validator("model")
    @classmethod
    def validate_model_length(cls, value: str, info: ValidationInfo) -> str:
        settings = _settings_from_context(info)
        if len(value) > settings.max_model_name_chars:
            raise ValueError(
                f"String should have at most {settings.max_model_name_chars} characters"
            )
        return value

    @field_validator("messages")
    @classmethod
    def validate_messages_length(
        cls, value: list[ChatMessage], info: ValidationInfo
    ) -> list[ChatMessage]:
        settings = _settings_from_context(info)
        if len(value) > settings.max_messages:
            raise ValueError(f"List should have at most {settings.max_messages} items")
        return value

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is None:
            return value
        settings = _settings_from_context(info)
        if value > settings.max_output_tokens:
            raise ValueError(
                f"Input should be less than or equal to {settings.max_output_tokens}"
            )
        return value

    def to_normalized(self) -> NormalizedChatRequest:
        return NormalizedChatRequest(
            model=self.model,
            messages=[
                NormalizedMessage(role=message.role, content=message.content)
                for message in self.messages
            ],
            stream=self.stream,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            ignored_fields=[],
        )


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: str
    content: str


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage

    @classmethod
    def from_normalized(cls, response: NormalizedChatResponse) -> "ChatCompletionResponse":
        return cls(
            id=response.id,
            model=response.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChoiceMessage(
                        role=response.message.role,
                        content=response.message.content,
                    ),
                    finish_reason=response.finish_reason,
                )
            ],
            usage=Usage(**response.usage.model_dump()),
        )


class DeltaMessage(BaseModel):
    role: str | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: DeltaMessage
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: Usage | None = None

    @classmethod
    def from_normalized(
        cls,
        chunk: NormalizedStreamChunk,
        *,
        fallback_id: str,
        fallback_model: str,
    ) -> "ChatCompletionChunk":
        return cls(
            id=chunk.id or fallback_id,
            model=chunk.model or fallback_model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=DeltaMessage(
                        role=chunk.role,
                        content=chunk.delta or None,
                    ),
                    finish_reason=chunk.finish_reason,
                )
            ],
            usage=Usage(**chunk.usage.model_dump()) if chunk.usage else None,
        )


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


def model_dump_json_line(model: BaseModel) -> str:
    return model.model_dump_json(exclude_none=True)
