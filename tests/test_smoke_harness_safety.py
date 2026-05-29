from __future__ import annotations

import importlib


def test_smoke_provider_import_has_no_side_effects(monkeypatch):
    def fail_openai(*args, **kwargs):
        raise AssertionError("OpenAI client must not be constructed at import time")

    def fail_anthropic(*args, **kwargs):
        raise AssertionError("Anthropic client must not be constructed at import time")

    monkeypatch.setattr("app.providers.openai_provider.AsyncOpenAI", fail_openai)
    monkeypatch.setattr("app.providers.anthropic_provider.AsyncAnthropic", fail_anthropic)

    module = importlib.import_module("scripts.smoke_provider")

    assert module.AUTH_TOKEN == "smoke-local-key"


def test_smoke_provider_dry_run_makes_no_provider_or_network_calls(
    monkeypatch,
    capsys,
    tmp_path,
):
    module = importlib.import_module("scripts.smoke_provider")
    constructed = {"openai": 0, "anthropic": 0, "httpx": 0}

    def fail_openai(*args, **kwargs):
        constructed["openai"] += 1
        raise AssertionError("OpenAI client must not be constructed during dry-run")

    def fail_anthropic(*args, **kwargs):
        constructed["anthropic"] += 1
        raise AssertionError("Anthropic client must not be constructed during dry-run")

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            constructed["httpx"] += 1
            raise AssertionError("HTTP client must not be constructed during dry-run")

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-key")
    monkeypatch.setattr("app.providers.openai_provider.AsyncOpenAI", fail_openai)
    monkeypatch.setattr("app.providers.anthropic_provider.AsyncAnthropic", fail_anthropic)
    monkeypatch.setattr(module.httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))

    assert module.main([]) == 0

    output = capsys.readouterr().out
    assert "DRY-RUN only" in output
    assert "WOULD CALL (stream + non-stream)" in output
    assert constructed == {"openai": 0, "anthropic": 0, "httpx": 0}
    assert not list(tmp_path.iterdir())
