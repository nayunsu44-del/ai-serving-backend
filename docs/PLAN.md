# AI Serving Backend - Plan

## Goal
Build a production-ready FastAPI backend that abstracts AI providers (OpenAI, Anthropic) behind a unified API.

## Architecture

```
Client → FastAPI → Auth → RateLimit → Router → Provider → External API
                                           ↓
                                       Streaming Response
```

## File Structure

```
ai-serving-backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry
│   ├── config.py            # Pydantic Settings (env vars)
│   ├── auth.py              # API key validation (Bearer token)
│   ├── schemas.py           # Pydantic request/response models
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── chat.py          # POST /v1/chat/completions
│   │   └── health.py        # GET /health
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py          # Abstract Provider class
│   │   ├── openai.py        # OpenAI implementation
│   │   └── anthropic.py     # Anthropic implementation
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── ratelimit.py     # In-memory token bucket
│   │   └── logging.py       # Request logging
│   └── errors.py            # Custom exceptions
├── tests/
│   ├── test_auth.py
│   ├── test_chat.py
│   └── test_providers.py
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── PLAN.md
```

## API Endpoints

### POST /v1/chat/completions
OpenAI-compatible chat completion endpoint. Routes to provider based on `model` field.

Request:
```json
{
  "model": "gpt-4o" | "claude-sonnet-4-6" | ...,
  "messages": [{"role": "user", "content": "hello"}],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1024
}
```

Response (non-streaming):
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "gpt-4o",
  "choices": [{"index": 0, "message": {...}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
}
```

Streaming uses SSE (`text/event-stream`).

### GET /health
Liveness check.

### GET /v1/models
List available models.

## Auth
- Header: `Authorization: Bearer <API_KEY>`
- API keys stored in env (`API_KEYS=key1,key2,key3`)
- Returns 401 if missing/invalid

## Rate Limiting
- In-memory token bucket per API key
- Default: 60 req/min per key
- Returns 429 with `Retry-After` header

## Provider Abstraction

```python
class Provider(ABC):
    @abstractmethod
    async def chat(request) -> ChatResponse: ...

    @abstractmethod
    async def chat_stream(request) -> AsyncIterator[Chunk]: ...
```

Model routing map:
- `gpt-*` → OpenAI
- `claude-*` → Anthropic

## Config (.env)

```
API_KEYS=sk-test-1,sk-test-2
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
RATE_LIMIT_RPM=60
LOG_LEVEL=INFO
```

## Stack
- Python 3.11+
- FastAPI + Uvicorn
- httpx (async HTTP client)
- pydantic-settings
- openai, anthropic SDKs (official)
- pytest + pytest-asyncio (tests)

## Success Criteria
1. Server starts via `uvicorn app.main:app`
2. `/health` returns 200
3. `/v1/chat/completions` with valid key + model returns response (mocked or real)
4. Invalid key returns 401
5. Streaming responses work via SSE
6. Rate limit triggers 429 correctly
