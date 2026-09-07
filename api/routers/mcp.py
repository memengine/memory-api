from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from api.db.cache import CacheService
from api.dependencies import DbSession, get_cache_service
from api.errors import APIError
from api.services.global_agent_service import GlobalAgentService
from api.services.mcp_universal_capability_service import issue_universal_mcp_capability
from api.services.uui_service import UUIService

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


@router.post("/universal/capability")
async def issue_universal_capability(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    x_memoryos_mcp_universal_agent_key: Annotated[
        str | None, Header(alias="X-MemoryOS-MCP-Universal-Agent-Key")
    ] = None,
) -> dict[str, object]:
    """Exchange a verified public-MCP Clerk session for a scoped Universal capability."""
    if request.headers.get("x-memoryos-mcp-client", "").strip().lower() != "public-v1":
        raise APIError(status_code=403, code="MCP_403", error="public_mcp_marker_required")
    email = str(getattr(request.state, "auth_email", "") or "").strip().lower()
    if not email or not bool(getattr(request.state, "auth_email_verified", False)):
        raise APIError(status_code=403, code="MCP_403", error="verified_email_required")
    agent = await GlobalAgentService(session=session, cache_service=cache_service).resolve_from_api_key(
        str(x_memoryos_mcp_universal_agent_key or "").strip()
    )
    if agent is None:
        raise APIError(status_code=403, code="MCP_403", error="mcp_agent_auth_failed")
    uui_service = UUIService(session=session, cache_service=cache_service)
    universal_user = await uui_service.resolve_by_email(email)
    if universal_user is None:
        raise APIError(status_code=403, code="MCP_403", error="universal_profile_required")
    grants = await uui_service.get_grants(str(universal_user.id))
    grant = next((item for item in grants if str(item.agent_id) == str(agent.id)), None)
    if grant is None:
        raise APIError(status_code=403, code="MCP_403", error="universal_consent_required")
    try:
        capability = issue_universal_mcp_capability(
            user_uui_id=str(universal_user.id), agent_id=str(agent.id), grant_id=str(grant.id)
        )
    except ValueError as exc:
        raise APIError(status_code=503, code="MCP_503", error="universal_capability_unavailable") from exc
    return {
        "data": {
            "capability": capability,
            "expires_in": 300,
            "access_type": grant.access_type,
            "categories_allowed": list(grant.categories_allowed or []),
        }
    }
