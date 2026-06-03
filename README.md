# AI Serving Backend

FastAPI backend that exposes an OpenAI-compatible subset and routes chat requests to OpenAI or Anthropic providers.

Supported chat fields are `model`, `messages`, `stream`, `temperature`, and `max_tokens`. Additional request fields are rejected with a validation error; unsupported model prefixes return an OpenAI-style error JSON.

## Install

Requires **Python 3.13**. Dependencies are fully pinned in `requirements.txt` for reproducible installs.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and set values. The minimum to boot locally:

```dotenv
API_KEYS=local-test-key
AUTH_MODE=api_key
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
```

`API_KEYS` are service bearer tokens accepted by this backend. They are SHA-256 hashed at startup and only hashes are retained in memory. Provider keys are used only to call upstream SDKs.

`.env.example` lists every variable with a runnable default. The full reference, with code defaults, is below. Comma-separated list values (`AUTH_MODE`, `OPENAI_MODELS`, `PII_TYPES`, etc.) are split on commas and trimmed.

### Settings reference

**Auth**

| Variable | Default | Description |
| --- | --- | --- |
| `API_KEYS` | (empty) | Service bearer tokens, comma-separated. SHA-256 hashed at startup. |
| `AUTH_MODE` | `api_key` | `api_key`, or `api_key,jwt` to also accept OIDC JWTs. |
| `OPENAI_API_KEY` | (none) | Upstream OpenAI key. |
| `ANTHROPIC_API_KEY` | (none) | Upstream Anthropic key. |
| `API_KEY_LAST_USED_MIN_INTERVAL_SECONDS` | `60` | Debounce for `last_used_at` writes on DB API keys. |
| `JWT_ISSUER` | (none) | Expected JWT issuer. |
| `JWT_AUDIENCE` | (none) | Expected JWT audience. |
| `JWT_JWKS_URL` | (none) | JWKS endpoint for signature verification. |
| `JWT_ALGORITHMS` | `RS256` | Allowed JWT signing algorithms (comma-separated). |
| `JWT_SCOPE_CLAIM` | `groups` | JWT claim that holds the user's groups. |
| `JWT_GROUP_SCOPE_MAP` | (empty) | `group=scope` entries, e.g. `ai-user=chat,ai-admin=admin`. |
| `JWT_ORG_CLAIM` | `org_id` | JWT claim that holds the organization id. |

**Storage & rate limiting**

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db` | Async SQLAlchemy DSN; set a Postgres DSN for production. |
| `REDIS_URL` | (none) | Redis DSN, required when `RATE_LIMIT_BACKEND=redis`. |
| `RATE_LIMIT_RPM` | `60` | Requests per minute per API key; `0` disables. |
| `RATE_LIMIT_BACKEND` | `memory` | `memory` or `redis`. |
| `RATE_LIMIT_STRICT` | `false` | When `true`, a missing/unreachable Redis fails closed instead of falling back to in-memory. |
| `PRE_AUTH_RPM_PER_IP` | `30` | Failed/missing-auth attempts per minute per client IP; `0` disables. |
| `TRUSTED_PROXIES` | (empty) | Trusted proxy IPs/CIDRs (comma-separated). |
| `TRUST_FORWARDED_FOR` | `false` | Honor `X-Forwarded-For`; only enable with `TRUSTED_PROXIES` set. |

**Audit, compliance & PII**

| Variable | Default | Description |
| --- | --- | --- |
| `AUDIT_ENABLED` | `true` | Master switch for audit logging. |
| `AUDIT_SYNC` | `false` | `true` awaits the audit insert before responding. |
| `AUDIT_FALLBACK_PATH` | `./data/audit_fallback.jsonl` | JSONL file written when DB audit insert fails. |
| `AUDIT_STORE_MESSAGES` | `false` | Also store PII-masked message bodies in `audit_message`. |
| `POLICY_MODE` | `log_only` | `block`, `log_only`, or `disabled`. |
| `FORBIDDEN_PATTERNS` | (empty) | `rule_id=regex` entries, case-insensitive (no commas inside a regex). |
| `PII_MASKING_ENABLED` | `true` | Redact PII before calling the upstream provider. |
| `PII_TYPES` | `rrn,card,phone,email` | Which PII detectors run. |

**Limits & output**

| Variable | Default | Description |
| --- | --- | --- |
| `DEFAULT_MAX_TOKENS` | `1024` | `max_tokens` applied **when the client omits it**. Anthropic requires `max_tokens`, so an omitted value is filled with this; OpenAI is left to its own default. |
| `MAX_OUTPUT_TOKENS` | `4096` | Hard ceiling: a request's `max_tokens` may not exceed this or it is rejected with a validation error. |
| `MAX_REQUEST_BYTES` | `1048576` | Maximum request body size. |
| `MAX_MESSAGES` | `200` | Maximum messages per request. |
| `MAX_MESSAGE_CHARS` | `100000` | Maximum characters per message. |
| `MAX_MODEL_NAME_CHARS` | `128` | Maximum model name length. |
| `STREAM_MAX_DURATION_SECONDS` | `300` | Maximum streaming response duration. |
| `MAX_CONCURRENT_STREAMS_PER_KEY` | `4` | Concurrent streams allowed per API key. |

**Models & server**

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_MODELS` | `gpt-4o,gpt-4o-mini` | Routable OpenAI model IDs (comma-separated). |
| `ANTHROPIC_MODELS` | `claude-sonnet-4-6,claude-3-5-haiku-latest` | Routable Anthropic model IDs (comma-separated). |
| `LOG_LEVEL` | `INFO` | Application log level. |
| `ALLOWED_HOSTS` | `*` | Allowed `Host` headers; lock down in production. |
| `DOCS_ENABLED` | `true` | Serve `/docs` and `/openapi.json`; disable in production. |

> **`DEFAULT_MAX_TOKENS` vs `MAX_OUTPUT_TOKENS`** — these are different knobs. `MAX_OUTPUT_TOKENS` (4096) is the ceiling a request may ask for; `DEFAULT_MAX_TOKENS` (1024) is the value used when a request omits `max_tokens` entirely. If Anthropic output looks truncated at 1024 tokens, pass an explicit `max_tokens` in the request or raise `DEFAULT_MAX_TOKENS`.

### Supported models

Only the IDs in `OPENAI_MODELS` and `ANTHROPIC_MODELS` are routable; any other model returns an OpenAI-style "model not found" error. Out of the box that is:

- OpenAI: `gpt-4o`, `gpt-4o-mini`
- Anthropic: `claude-sonnet-4-6`, `claude-3-5-haiku-latest`

Override either list via the environment variables above to add or remove models.

## Authentication

API key authentication is the default: `AUTH_MODE=api_key`. `Authorization: Bearer <api-key>` is checked against environment API keys and database API keys, and provider API keys such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are unaffected.

OIDC-style JWT bearer authentication can be enabled alongside API keys with `AUTH_MODE=api_key,jwt`. Configure `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_JWKS_URL`, `JWT_ALGORITHMS`, `JWT_SCOPE_CLAIM`, `JWT_GROUP_SCOPE_MAP`, and `JWT_ORG_CLAIM`. JWT groups are mapped to local scopes with `group=scope` entries such as `ai-user=chat,ai-admin=admin`.

JWT organization claims must match an existing `Organization.id`; the gateway does not auto-provision organizations from JWTs. Audit and rate limiting use the shared principal flow, and audit stores only a hashed principal identifier, never the raw JWT subject, email, or token.

Audit and API key metadata use SQLite by default at `./data/app.db`. With Docker, that path is mounted so audit logs persist. To use Postgres, set `DATABASE_URL` to the commented Postgres DSN in `.env.example` (`postgresql+asyncpg://app:change-me@postgres:5432/ai_serving`).

Rate limiting uses an in-memory token bucket for development and tests by default (`RATE_LIMIT_BACKEND=memory`). Set `RATE_LIMIT_BACKEND=redis` and `REDIS_URL=redis://redis:6379/0` to use Redis in Docker.

`TRUST_FORWARDED_FOR=false` ignores `X-Forwarded-For`; set it to `true` only with `TRUSTED_PROXIES` listing trusted proxy IPs/CIDRs. `PRE_AUTH_RPM_PER_IP` uses that resolved client IP for failed-auth throttling.

`AUDIT_SYNC=false` writes audit rows in the background; set `AUDIT_SYNC=true` to await audit insertion before returning. If audit DB insertion fails, JSONL fallback is written to `AUDIT_FALLBACK_PATH` and can be replayed with `POST /admin/audit/replay` by a `super_admin`.

`PII_MASKING_ENABLED=true` redacts personal data (Korean resident registration numbers, Luhn-valid card numbers, phone numbers, emails) from request messages before they reach the upstream provider. `PII_TYPES` selects which detectors run. Masking is irreversible; only redaction counts are logged, never the raw values.

Compliance events are recorded to `policy_event`; `AUDIT_STORE_MESSAGES=true` additionally stores PII-masked message bodies in `audit_message`, never raw message text.

`ALLOWED_HOSTS` defaults to `*` for local development. Lock it down to the deployed hostnames in production. `/docs` and `/openapi.json` are enabled by default for development; set `DOCS_ENABLED=false` in production.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

## Smoke test (real API keys)

The provider smoke harness is manual and is not collected by normal `pytest` runs. It only makes real paid OpenAI or Anthropic API calls when `--run` is passed.

Run a no-network dry-run first:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_provider.py
```

To actually call selected providers, set `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY` in the environment and add `--run`:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_provider.py --run
```

The harness checks both streaming and non-streaming chat paths through the FastAPI gateway, uses a temporary SQLite audit database, and caps `--max-tokens` at 64 for cost safety.

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

Run against the project virtualenv (no activation required):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Or, with the venv activated, simply `pytest`. Test configuration (async mode, test paths) is pinned in `pytest.ini`. Tests mock provider clients and do not call external APIs.
