from __future__ import annotations

from datetime import UTC
from datetime import datetime

from fastapi import Request


def get_request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("x-request-id", "")))


def utc_now() -> datetime:
    return datetime.now(UTC)
