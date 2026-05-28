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
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
# DATABASE_URL=postgresql+asyncpg://app:change-me@postgres:5432/ai_serving
RATE_LIMIT_RPM=60
RATE_LIMIT_BACKEND=memory
# REDIS_URL=redis://redis:6379/0
PRE_AUTH_RPM_PER_IP=30
TRUSTED_PROXIES=
TRUST_FORWARDED_FOR=false
AUDIT_SYNC=false
AUDIT_FALLBACK_PATH=./data/audit_fallback.jsonl
MAX_REQUEST_BYTES=1048576
MAX_MESSAGES=200
MAX_MESSAGE_CHARS=100000
MAX_MODEL_NAME_CHARS=128
MAX_OUTPUT_TOKENS=4096
STREAM_MAX_DURATION_SECONDS=300
MAX_CONCURRENT_STREAMS_PER_KEY=4
ALLOWED_HOSTS=*
DOCS_ENABLED=true
POSTGRES_USER=app
POSTGRES_PASSWORD=change-me
POSTGRES_DB=ai_serving
```

`API_KEYS` are service bearer tokens accepted by this backend. They are SHA-256 hashed at startup and only hashes are retained in memory. Provider keys are used only to call upstream SDKs.

Audit and API key metadata use SQLite by default at `./data/app.db`. With Docker, that path is mounted so audit logs persist. To use Postgres, set `DATABASE_URL` to the commented Postgres example.

Rate limiting uses an in-memory token bucket for development and tests by default (`RATE_LIMIT_BACKEND=memory`). Set `RATE_LIMIT_BACKEND=redis` and `REDIS_URL=redis://redis:6379/0` to use Redis in Docker.

`TRUST_FORWARDED_FOR=false` ignores `X-Forwarded-For`; set it to `true` only with `TRUSTED_PROXIES` listing trusted proxy IPs/CIDRs. `PRE_AUTH_RPM_PER_IP` uses that resolved client IP for failed-auth throttling.

`AUDIT_SYNC=false` writes audit rows in the background; set `AUDIT_SYNC=true` to await audit insertion before returning. If audit DB insertion fails, JSONL fallback is written to `AUDIT_FALLBACK_PATH` and can be replayed with `POST /admin/audit/replay` by a `super_admin`.

`ALLOWED_HOSTS` defaults to `*` for local development. Lock it down to the deployed hostnames in production. `/docs` and `/openapi.json` are enabled by default for development; set `DOCS_ENABLED=false` in production.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

## Docker quickstart

```powershell
cp .env.example .env
docker compose up --build
```

The app listens on `http://127.0.0.1:8000`. The compose file starts Redis and Postgres, but the app keeps using SQLite unless you change `DATABASE_URL` in `.env`.

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

Admin usage summary:

```powershell
curl "http://127.0.0.1:8000/admin/usage?group_by=model" `
  -H "Authorization: Bearer local-test-key"
```

Admin endpoints require the `admin` scope and include `/admin/usage`, `/admin/audit`, `/admin/orgs`, and `/admin/keys`. Environment API keys are bootstrap `super_admin` keys; database admin keys are scoped to their own organization unless explicitly granted `super_admin` by an existing super admin.

## Tests

```powershell
pytest
```

Tests mock provider clients and do not call external APIs.
