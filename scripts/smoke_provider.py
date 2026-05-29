from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

AUTH_TOKEN = "smoke-local-key"
MAX_TOKENS_CAP = 64
SELECTED_PROVIDERS = ("openai", "anthropic")


@dataclass(frozen=True)
class ProviderPlan:
    provider: str
    model: str
    has_key: bool
    max_tokens: int


@dataclass
class CheckResult:
    provider: str
    mode: str
    http_status: int | str = "-"
    prompt_tokens: int | str = "-"
    completion_tokens: int | str = "-"
    total_tokens: int | str = "-"
    cost_usd: Decimal | str = "-"
    audit_status: str = "missing"
    status: str = "FAIL"
    text: str = ""
    error: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual smoke-test harness for real OpenAI/Anthropic gateway calls."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Actually make real paid provider calls. Default is a no-network dry-run.",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "anthropic", "all"),
        default="all",
        help="Provider to check.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16,
        help=f"Requested max_tokens; clamped to {MAX_TOKENS_CAP}.",
    )
    parser.add_argument("--openai-model", default="gpt-4o-mini")
    parser.add_argument("--anthropic-model", default="claude-3-5-haiku-latest")
    parser.add_argument("--prompt", default="Reply with the single word: pong")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP client timeout in seconds.",
    )
    return parser


def selected_providers(provider: str) -> list[str]:
    if provider == "all":
        return list(SELECTED_PROVIDERS)
    return [provider]


def clamp_max_tokens(value: int) -> tuple[int, str | None]:
    if value > MAX_TOKENS_CAP:
        return MAX_TOKENS_CAP, (
            f"WARNING: --max-tokens {value} exceeds the safety cap; "
            f"using {MAX_TOKENS_CAP}."
        )
    if value < 1:
        return 1, "WARNING: --max-tokens must be at least 1; using 1."
    return value, None


def load_key_presence() -> dict[str, bool]:
    from app.config import Settings

    settings = Settings()
    return {
        "openai": bool((settings.openai_api_key or "").strip()),
        "anthropic": bool((settings.anthropic_api_key or "").strip()),
    }


def build_plans(args: argparse.Namespace, max_tokens: int) -> list[ProviderPlan]:
    models = {
        "openai": args.openai_model,
        "anthropic": args.anthropic_model,
    }
    key_presence = load_key_presence()
    return [
        ProviderPlan(
            provider=provider,
            model=models[provider],
            has_key=key_presence[provider],
            max_tokens=max_tokens,
        )
        for provider in selected_providers(args.provider)
    ]


def build_smoke_settings(args: argparse.Namespace, database_url: str) -> Any:
    from app.config import Settings

    return Settings(
        api_keys=[AUTH_TOKEN],
        audit_sync=True,
        database_url=database_url,
        openai_models=[args.openai_model],
        anthropic_models=[args.anthropic_model],
        max_output_tokens=MAX_TOKENS_CAP,
    )


def temp_sqlite_url() -> str:
    db_path = Path(tempfile.gettempdir()) / f"ai-serving-smoke-{uuid.uuid4().hex}.db"
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def print_plan(plans: list[ProviderPlan]) -> None:
    rows = [
        [
            plan.provider,
            "yes" if plan.has_key else "no",
            plan.model,
            str(plan.max_tokens),
            "WOULD CALL (stream + non-stream)" if plan.has_key else "SKIP (no key)",
        ]
        for plan in plans
    ]
    print_table(["provider", "key", "model", "max_tokens", "plan"], rows)


def print_results(results: list[CheckResult]) -> None:
    rows = [
        [
            result.provider,
            result.mode,
            str(result.http_status),
            f"{result.prompt_tokens}/{result.completion_tokens}/{result.total_tokens}",
            str(result.cost_usd),
            result.audit_status,
            result.status,
        ]
        for result in results
    ]
    print_table(
        ["provider", "mode", "HTTP", "tokens(p/c/t)", "cost_usd", "audit", "result"],
        rows,
    )
    for result in results:
        if result.text:
            print(f"{result.provider} {result.mode} response: {truncate(result.text)}")
        if result.error:
            print(f"{result.provider} {result.mode} error: {truncate(result.error, 1000)}")


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    all_rows = [headers, *rows]
    widths = [max(len(str(row[index])) for row in all_rows) for index in range(len(headers))]
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))


def truncate(value: str, limit: int = 300) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def chat_payload(plan: ProviderPlan, prompt: str, stream: bool) -> dict[str, Any]:
    return {
        "model": plan.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "max_tokens": plan.max_tokens,
    }


async def fetch_audit(app: Any, request_id: str) -> Any | None:
    from sqlalchemy import select

    from app.db.models import AuditLog

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == request_id)
        )
        return result.scalar_one_or_none()


def validate_audit(
    audit: Any | None,
    *,
    plan: ProviderPlan,
    stream: bool,
) -> tuple[bool, str, int | str, int | str, int | str, Decimal | str]:
    if audit is None:
        return False, "missing", "-", "-", "-", "-"

    matches = (
        audit.provider == plan.provider
        and audit.model == plan.model
        and audit.total_tokens > 0
        and audit.cost_usd >= 0
        and audit.stream is stream
    )
    audit_status = "OK" if matches else "bad"
    return (
        matches,
        audit_status,
        audit.prompt_tokens,
        audit.completion_tokens,
        audit.total_tokens,
        audit.cost_usd,
    )


async def run_non_stream_check(
    *,
    app: Any,
    client: httpx.AsyncClient,
    plan: ProviderPlan,
    prompt: str,
) -> CheckResult:
    result = CheckResult(provider=plan.provider, mode="non-stream")
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        json=chat_payload(plan, prompt, stream=False),
    )
    result.http_status = response.status_code
    if response.status_code != 200:
        result.error = response.text
        return result

    body = response.json()
    content = body["choices"][0]["message"]["content"]
    usage = body["usage"]
    request_ok = bool(content) and int(usage["total_tokens"]) > 0
    audit = await fetch_audit(app, response.headers["x-request-id"])
    audit_ok, audit_status, prompt_tokens, completion_tokens, total_tokens, cost_usd = (
        validate_audit(audit, plan=plan, stream=False)
    )
    result.prompt_tokens = prompt_tokens
    result.completion_tokens = completion_tokens
    result.total_tokens = total_tokens
    result.cost_usd = cost_usd
    result.audit_status = audit_status
    result.text = content
    result.status = "PASS" if request_ok and audit_ok else "FAIL"
    if not request_ok:
        result.error = "Response content was empty or usage.total_tokens was not positive."
    return result


async def run_stream_check(
    *,
    app: Any,
    client: httpx.AsyncClient,
    plan: ProviderPlan,
    prompt: str,
) -> CheckResult:
    result = CheckResult(provider=plan.provider, mode="stream")
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        json=chat_payload(plan, prompt, stream=True),
    )
    result.http_status = response.status_code
    if response.status_code != 200:
        result.error = response.text
        return result

    content_type = response.headers.get("content-type", "")
    chunks, saw_done, saw_usage, parse_errors = parse_sse_body(response.text)
    content = "".join(chunks)
    stream_ok = (
        content_type.startswith("text/event-stream")
        and bool(chunks)
        and bool(content)
        and saw_done
        and saw_usage
        and not parse_errors
    )

    audit = await fetch_audit(app, response.headers["x-request-id"])
    audit_ok, audit_status, prompt_tokens, completion_tokens, total_tokens, cost_usd = (
        validate_audit(audit, plan=plan, stream=True)
    )
    result.prompt_tokens = prompt_tokens
    result.completion_tokens = completion_tokens
    result.total_tokens = total_tokens
    result.cost_usd = cost_usd
    result.audit_status = audit_status
    result.text = content
    result.status = "PASS" if stream_ok and audit_ok else "FAIL"
    if not stream_ok:
        usage_note = " usage chunk absent." if not saw_usage else ""
        result.error = (
            "Stream did not contain valid content chunks and [DONE]."
            f"{usage_note} content-type={content_type!r}; parse_errors={parse_errors}"
        )
    return result


def parse_sse_body(body: str) -> tuple[list[str], bool, bool, list[str]]:
    chunks: list[str] = []
    saw_done = False
    saw_usage = False
    parse_errors: list[str] = []

    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            saw_done = True
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue

        if payload.get("usage") is not None:
            saw_usage = True
        for choice in payload.get("choices", []):
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                chunks.append(content)

    return chunks, saw_done, saw_usage, parse_errors


async def run_provider_checks(
    *,
    app: Any,
    client: httpx.AsyncClient,
    plan: ProviderPlan,
    prompt: str,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in (run_non_stream_check, run_stream_check):
        try:
            results.append(await check(app=app, client=client, plan=plan, prompt=prompt))
        except Exception as exc:
            results.append(
                CheckResult(
                    provider=plan.provider,
                    mode="non-stream" if check is run_non_stream_check else "stream",
                    status="FAIL",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


async def run_smoke(args: argparse.Namespace) -> int:
    max_tokens, warning = clamp_max_tokens(args.max_tokens)
    if warning:
        print(warning)

    plans = build_plans(args, max_tokens)
    if not args.run:
        print("DRY-RUN only. No provider clients are constructed and no network calls are made.")
        print_plan(plans)
        return 0

    runnable = [plan for plan in plans if plan.has_key]
    skipped = [plan for plan in plans if not plan.has_key]
    if skipped:
        for plan in skipped:
            print(f"SKIP {plan.provider}: no API key found in environment.")
    if not runnable:
        print("No selected providers have API keys; nothing to run.")
        return 0

    database_url = temp_sqlite_url()
    settings = build_smoke_settings(args, database_url)
    from app.main import create_app

    app = create_app(settings)
    results: list[CheckResult] = []

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=args.timeout,
        ) as client:
            for plan in runnable:
                results.extend(
                    await run_provider_checks(
                        app=app,
                        client=client,
                        plan=plan,
                        prompt=args.prompt,
                    )
                )

    print_results(results)
    return 0 if results and all(result.status == "PASS" for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    raise SystemExit(main())
