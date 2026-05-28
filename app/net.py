from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any

from fastapi import Request


def is_trusted_proxy(remote_ip: str, trusted: list[str]) -> bool:
    try:
        remote = ip_address(remote_ip)
    except ValueError:
        return False

    for item in trusted:
        candidate = item.strip()
        if not candidate:
            continue
        try:
            if remote in ip_network(candidate, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request, settings: Any) -> str:
    remote_ip = request.client.host if request.client else "unknown"
    if not getattr(settings, "trust_forwarded_for", False):
        return remote_ip

    trusted_proxies = getattr(settings, "trusted_proxies", [])
    if not is_trusted_proxy(remote_ip, trusted_proxies):
        return remote_ip

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return remote_ip

    entries = [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]
    if not entries:
        return remote_ip

    for entry in reversed(entries):
        if not is_trusted_proxy(entry, trusted_proxies):
            return entry

    return entries[0]
