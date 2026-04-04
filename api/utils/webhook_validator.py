from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse


def validate_webhook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False

    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    if hostname.endswith(".local"):
        return False

    try:
        address = ip_address(hostname)
    except ValueError:
        return True

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return True
