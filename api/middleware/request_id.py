from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


LOGGER = logging.getLogger("memoryos.requests")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        started_at = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)

        response.headers["x-request-id"] = request_id
        LOGGER.info(
            json.dumps(
                {
                    "event": "request",
                    "request_id": request_id,
                    "user_id": getattr(request.state, "user_id", None),
                    "endpoint": request.url.path,
                    "method": request.method,
                    "latency_ms": latency_ms,
                    "status_code": response.status_code,
                }
            )
        )
        return response
