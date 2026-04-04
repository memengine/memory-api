from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response


LOGGER = logging.getLogger("uvicorn.error")
ADMIN_HEADER_NAME = "X-Admin-Secret"
FORBIDDEN_PAYLOAD = {"error": "forbidden", "code": "ADMIN_AUTH_FAILED"}
MIN_ADMIN_SECRET_LENGTH = 32


def validate_admin_secret_config(admin_secret: str | None = None) -> str:
    secret_value = (admin_secret if admin_secret is not None else os.getenv("ADMIN_SECRET", "")).strip()
    if len(secret_value) < MIN_ADMIN_SECRET_LENGTH:
        raise RuntimeError(
            f"ADMIN_SECRET must be at least {MIN_ADMIN_SECRET_LENGTH} characters long."
        )
    return secret_value


class AdminAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, admin_secret: str | None = None) -> None:
        super().__init__(app)
        self.admin_secret = validate_admin_secret_config(admin_secret)

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if not request.url.path.startswith("/v1/internal/"):
            return await call_next(request)

        provided_secret = request.headers.get(ADMIN_HEADER_NAME, "")
        is_authorized = bool(provided_secret) and secrets.compare_digest(
            provided_secret,
            self.admin_secret,
        )
        self._log_admin_access(request, success=is_authorized)

        if not is_authorized:
            return JSONResponse(status_code=403, content=FORBIDDEN_PAYLOAD)

        return await call_next(request)

    @staticmethod
    def _log_admin_access(request: Request, *, success: bool) -> None:
        LOGGER.info(
            json.dumps(
                {
                    "event": "admin_endpoint_access",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "endpoint": request.url.path,
                    "method": request.method,
                    "success": success,
                    "ip_address": (
                        request.client.host
                        if request.client is not None and request.client.host is not None
                        else "unknown"
                    ),
                }
            )
        )
