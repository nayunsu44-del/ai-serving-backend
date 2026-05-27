# AI Serving Backend

FastAPI backend that exposes an OpenAI-compatible subset and routes chat requests to OpenAI or Anthropic providers.

Supported chat fields are `model`, `messages`, `stream`, `temperature`, and `max_tokens`. Additional request fields are rejected with a validation error; unsupported model prefixes return an OpenAI-style error JSON.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and set values:

```dotenv
API_KEYS=local-test-key
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
RATE_LIMIT_RPM=60
RATE_LIMIT_BACKEND=memory
PRE_AUTH_RPM_PER_IP=30
MAX_REQUEST_BYTES=1048576
MAX_MESSAGES=200
MAX_MESSAGE_CHARS=100000
MAX_MODEL_NAME_CHARS=128
MAX_OUTPUT_TOKENS=4096
STREAM_MAX_DURATION_SECONDS=300
MAX_CONCURRENT_STREAMS_PER_KEY=4
ALLOWED_HOSTS=*
DOCS_ENABLED=true
```

`API_KEYS` are service bearer tokens accepted by this backend. They are SHA-256 hashed at startup and only hashes are retained in memory. Provider keys are used only to call upstream SDKs.

Rate limiting uses an in-memory token bucket for development and tests by default (`RATE_LIMIT_BACKEND=memory`). Production should plug `RateLimitBackend` into a shared backend such as Redis so limits work across processes and hosts.

`ALLOWED_HOSTS` defaults to `*` for local development. Lock it down to the deployed hostnames in production. `/docs` and `/openapi.json` are enabled by default for development; set `DOCS_ENABLED=false` in production.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

## Curl Examples

Health:

```powershell
curl http://127.0.0.1:8000/health
```

List models:

```powershell
curl http://127.0.0.1:8000/v1/models `
  -H "Authorization: Bearer local-test-key"
```

Non-streaming chat:

```powershell
curl http://127.0.0.1:8000/v1/chat/completions `
  -H "Authorization: Bearer local-test-key" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":64}"
```

Streaming chat:

```powershell
curl -N http://127.0.0.1:8000/v1/chat/completions `
  -H "Authorization: Bearer local-test-key" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"claude-sonnet-4-6\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"stream\":true,\"max_tokens\":64}"
```

## Tests

```powershell
pytest
```

Tests mock provider clients and do not call external APIs.
