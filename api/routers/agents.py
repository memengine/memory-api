from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from sqlalchemy import desc
from sqlalchemy import select

from api.db.models import GlobalAgent
from api.dependencies import DbSession
from api.dependencies import get_agent_service
from api.dependencies import get_authenticated_user_id
from api.dependencies import get_authenticated_tenant_id
from api.errors import APIError
from api.schemas.requests import AgentCreateRequest
from api.schemas.responses import AgentCreateResponse
from api.schemas.responses import AgentData
from api.schemas.responses import AgentListResponse
from api.schemas.responses import CursorPage
from api.schemas.uui_schemas import GlobalAgentCreateRequest
from api.schemas.uui_schemas import GlobalAgentData
from api.schemas.uui_schemas import GlobalAgentListResponse
from api.schemas.uui_schemas import GlobalAgentPublic
from api.schemas.uui_schemas import GlobalAgentPublicResponse
from api.schemas.uui_schemas import GlobalAgentRegistrationData
from api.schemas.uui_schemas import GlobalAgentRegistrationResponse
from api.services.agent_service import AgentService
from api.services.global_agent_service import GlobalAgentService
from api.routers.common import get_request_id
from api.routers.common import utc_now


router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _agent_to_data(agent) -> AgentData:
    return AgentData(
        id=str(agent.id),
        name=agent.name,
        description=agent.description,
        memory_scope=agent.memory_scope.value,
        created_at=agent.created_at,
    )


def _global_agent_to_data(agent, *, raw_agent_api_key: str | None = None) -> GlobalAgentData | GlobalAgentRegistrationData:
    payload = dict(
        id=agent.id,
        owner_tenant_id=agent.owner_tenant_id,
        name=agent.name,
        description=agent.description,
        logo_url=agent.logo_url,
        website_url=agent.website_url,
        default_categories_requested=list(agent.default_categories_requested or []),
        redirect_uri=getattr(agent, "redirect_uri", "") or "",
        is_verified=bool(agent.is_verified),
        is_public=bool(agent.is_public),
        created_at=agent.created_at,
        is_active=bool(agent.is_active),
    )
    if raw_agent_api_key is None:
        return GlobalAgentData(**payload)
    return GlobalAgentRegistrationData(raw_agent_api_key=raw_agent_api_key, **payload)


@router.get("/global/{agent_id}", response_model=GlobalAgentPublicResponse)
async def get_global_agent_profile(
    request: Request,
    agent_id: str,
    session: DbSession,
) -> GlobalAgentPublicResponse:
    profile = await GlobalAgentService(session=session).get_public_profile(agent_id)
    if profile is None:
        raise APIError(status_code=404, code="AGN_404", error="global_agent_not_found")
    return GlobalAgentPublicResponse(
        data=GlobalAgentPublic.model_validate(profile, from_attributes=True),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/global", response_model=GlobalAgentListResponse)
async def list_global_agents(
    request: Request,
    session: DbSession,
    authenticated_tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> GlobalAgentListResponse:
    result = await session.execute(
        select(GlobalAgent)
        .where(GlobalAgent.owner_tenant_id == uuid.UUID(authenticated_tenant_id))
        .order_by(desc(GlobalAgent.created_at))
        .limit(100)
    )
    agents = result.scalars().all()
    return GlobalAgentListResponse(
        data=[_global_agent_to_data(agent) for agent in agents],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/global", response_model=GlobalAgentRegistrationResponse)
async def register_global_agent(
    request: Request,
    payload: GlobalAgentCreateRequest,
    session: DbSession,
    authenticated_tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> GlobalAgentRegistrationResponse:
    agent, raw_agent_api_key = await GlobalAgentService(session=session).register(
        tenant_id=authenticated_tenant_id,
        name=payload.name,
        description=payload.description,
        logo_url=payload.logo_url,
        website_url=payload.website_url,
        default_categories_requested=list(payload.default_categories_requested),
        redirect_uri=payload.redirect_uri,
    )
    return GlobalAgentRegistrationResponse(
        data=_global_agent_to_data(agent, raw_agent_api_key=raw_agent_api_key),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
    cursor: str | None = Query(default=None, description="Cursor from the previous page."),
    limit: int = Query(default=10, ge=1, le=50, description="Maximum number of agents to return."),
) -> AgentListResponse:
    """List registered agents for the authenticated user.

    Parameters: cursor for pagination and a page limit up to 50.
    Responses: paginated agent list in the standard envelope.
    """
    agents, next_cursor, total = await agent_service.list_agents(
        authenticated_user_id=authenticated_user_id,
        cursor=cursor,
        limit=limit,
    )
    return AgentListResponse(
        data=[_agent_to_data(agent) for agent in agents],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("", response_model=AgentCreateResponse)
async def register_agent(
    request: Request,
    payload: AgentCreateRequest,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> AgentCreateResponse:
    """Register a new agent and its memory scope.

    Parameters: agent name, optional description, and memory scope.
    Responses: the created agent record.
    """
    agent = await agent_service.create_agent(
        authenticated_user_id=authenticated_user_id,
        name=payload.name,
        description=payload.description,
        memory_scope=payload.memory_scope,
    )
    return AgentCreateResponse(
        data=_agent_to_data(agent),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
