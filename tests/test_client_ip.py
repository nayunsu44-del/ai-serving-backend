from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.net import client_ip, is_trusted_proxy


def _request(remote_ip: str, forwarded_for: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (remote_ip, 12345),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_untrusted_remote_ignores_x_forwarded_for() -> None:
    settings = Settings(
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.1"],
    )

    assert client_ip(_request("192.0.2.10", "1.2.3.4"), settings) == "192.0.2.10"


def test_trusted_remote_uses_rightmost_untrusted_xff_entry() -> None:
    settings = Settings(
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.0/8"],
    )

    assert (
        client_ip(_request("10.0.0.1", "1.2.3.4, 10.0.0.2"), settings)
        == "1.2.3.4"
    )


def test_trusted_proxy_supports_cidr() -> None:
    settings = Settings(
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.0/8"],
    )

    assert is_trusted_proxy("10.1.2.3", ["10.0.0.0/8"]) is True
    assert (
        client_ip(_request("10.1.2.3", "1.2.3.4, 10.1.2.4"), settings)
        == "1.2.3.4"
    )


def test_xff_spoofed_leftmost_is_skipped_when_followed_by_real_client() -> None:
    settings = Settings(
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.0/8"],
    )

    assert (
        client_ip(_request("10.0.0.5", "evil-spoof, 1.2.3.4, 10.0.0.4"), settings)
        == "1.2.3.4"
    )


def test_xff_all_trusted_returns_leftmost() -> None:
    settings = Settings(
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.0/8"],
    )

    assert (
        client_ip(_request("10.0.0.3", "10.0.0.1, 10.0.0.2"), settings)
        == "10.0.0.1"
    )


def test_forwarded_for_is_ignored_by_default() -> None:
    settings = Settings(trusted_proxies=["10.0.0.1"])

    assert client_ip(_request("10.0.0.1", "1.2.3.4"), settings) == "10.0.0.1"
