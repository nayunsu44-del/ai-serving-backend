from __future__ import annotations

import pytest

from app.errors import ProviderAPIError


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_provider_api_error_maps_provider_failures_to_502(status: int):
    error = ProviderAPIError(
        provider="openai",
        upstream_status=status,
        raw_message="raw provider failure",
    )

    assert error.status_code == 502
    assert error.error_type == "provider_error"
    assert error.code == "provider_error"
    assert error.message == "Upstream provider error"


def test_provider_api_error_maps_rate_limit_with_retry_after():
    error = ProviderAPIError(
        provider="openai",
        upstream_status=429,
        raw_message="raw provider rate limit",
        retry_after="12",
    )

    assert error.status_code == 429
    assert error.error_type == "rate_limit_error"
    assert error.code == "rate_limit_exceeded"
    assert error.headers == {"Retry-After": "12"}


def test_provider_api_error_maps_upstream_model_errors_to_invalid_request():
    error = ProviderAPIError(
        provider="anthropic",
        upstream_status=404,
        raw_message="raw model not found details",
    )

    assert error.status_code == 400
    assert error.error_type == "invalid_request_error"
    assert error.code == "model_not_found"
    assert error.message == "Upstream provider rejected the requested model"


def test_provider_api_error_ignores_raw_positional_message():
    error = ProviderAPIError(
        "OpenAI request failed: raw provider secret",
        provider="openai",
        raw_message="raw provider secret",
    )

    assert error.message == "Upstream provider error"
