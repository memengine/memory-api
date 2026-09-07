from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from api.db.database import SessionLocal
from api.db.models import GlobalAgent, PermissionGrant, UniversalUser
from api.dependencies import get_cache_service
from api.services.global_agent_service import GlobalAgentService
from api.services.mcp_universal_capability_service import (
    verify_universal_mcp_capability,
)
from api.services.uui_service import UUIService


def _cors_allowed_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    origins = [origin.strip() for origin in configured if origin.strip()]
    if origins:
        return origins
    return ["http://localhost:3000", "http://localhost:3001", "http://localhost:3002"]


class UniversalAuthMiddleware:
    def __init__(
        self,
        app,
        *,
        session_factory: Callable[[], Any] | None = None,
        universal_app=None,
        global_agent_service_factory: Callable[..., GlobalAgentService] | None = None,
        uui_service_factory: Callable[..., UUIService] | None = None,
    ) -> None:
        self.app = app
        self.session_factory = session_factory or SessionLocal
        self.universal_app = universal_app
        self.global_agent_service_factory = global_agent_service_factory or GlobalAgentService
        self.uui_service_factory = uui_service_factory or UUIService

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/v1/universal/"):
            await self.app(scope, receive, send)
            return

        # Browser CORS preflight requests do not carry the app API key or UUI
        # token. Let CORSMiddleware handle OPTIONS, then authenticate the real
        # POST/GET request that follows.
        if str(scope.get("method", "")).upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        auth_header = request.headers.get("authorization", "").strip()
        uui_token = request.headers.get("x-memoryos-uui", "").strip()

        if auth_header.lower().startswith("bearer "):
            capability = verify_universal_mcp_capability(auth_header[7:].strip())
            if capability is None:
                await self._forbidden(scope, receive, send)
                return
            try:
                async with self.session_factory() as session:
                    global_agent = await session.get(GlobalAgent, uuid.UUID(capability.agent_id))
                    universal_user = await session.get(UniversalUser, uuid.UUID(capability.user_uui_id))
                    grant = await session.get(PermissionGrant, uuid.UUID(capability.grant_id))
            except Exception:  # noqa: BLE001 - authentication must fail closed on lookup faults.
                global_agent = universal_user = grant = None
            if (
                global_agent is None
                or universal_user is None
                or grant is None
                or not bool(global_agent.is_active)
                or not bool(universal_user.is_active)
                or not bool(grant.is_active)
                or str(grant.user_uui_id) != capability.user_uui_id
                or str(grant.agent_id) != capability.agent_id
                or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC))
            ):
                await self._forbidden(scope, receive, send)
                return
            state = scope.setdefault("state", {})
            state["global_agent"] = global_agent
            state["universal_user"] = universal_user
            state["auth_method"] = "mcp_universal_capability"
            state["auth_scheme"] = "bearer"
            target_app = self.universal_app or getattr(getattr(scope.get("app"), "state", None), "universal_app", None)
            if target_app is not None:
                await target_app(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if not auth_header or not uui_token:
            await self._forbidden(scope, receive, send)
            return

        try:
            scheme, raw_key = auth_header.split(" ", 1)
        except ValueError:
            await self._forbidden(scope, receive, send)
            return

        if scheme.lower() != "apikey" or not raw_key.strip():
            await self._forbidden(scope, receive, send)
            return

        main_app = scope.get("app")
        cache_service = None
        if main_app is not None:
            try:
                cache_service = get_cache_service(request)
            except Exception:  # noqa: BLE001 - preserve the existing no-cache fallback.
                cache_service = getattr(getattr(main_app, "state", None), "cache_service", None)

        try:
            async with self.session_factory() as session:
                agent_service = self.global_agent_service_factory(session=session)
                uui_service = self.uui_service_factory(session=session, cache_service=cache_service)
                global_agent = await agent_service.resolve_from_api_key(raw_key.strip())
                resolve_by_token = getattr(uui_service, "resolve_by_token", None)
                if callable(resolve_by_token):
                    universal_user = await resolve_by_token(uui_token)
                else:
                    universal_user = await uui_service.resolve(uui_token)
        except Exception:  # noqa: BLE001 - authentication must fail closed on lookup faults.
            global_agent = None
            universal_user = None

        if global_agent is None or universal_user is None:
            await self._forbidden(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["global_agent"] = global_agent
        state["universal_user"] = universal_user
        state["uui_token"] = uui_token
        state.setdefault("request_id", str(uuid.uuid4()))
        state["auth_method"] = "cross_agent"
        state["auth_scheme"] = "apikey"

        target_app = self.universal_app
        if target_app is None and main_app is not None:
            target_app = getattr(getattr(main_app, "state", None), "universal_app", None)

        if target_app is not None:
            await target_app(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _forbidden(self, scope, receive, send) -> None:
        request_id = str(scope.setdefault("state", {}).get("request_id") or uuid.uuid4())
        request = Request(scope, receive=receive)
        response = JSONResponse(
            status_code=403,
            content={
                "error": "cross_agent_auth_failed",
                "code": "UAT_001",
                "request_id": request_id,
            },
        )
        origin = request.headers.get("origin")
        if origin and origin in _cors_allowed_origins():
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        await response(scope, receive, send)
