from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from api.dependencies import get_agent_service
from api.dependencies import get_authenticated_user_id
from api.schemas.requests import AgentCreateRequest
from api.schemas.responses import AgentCreateResponse
from api.schemas.responses import AgentData
from api.schemas.responses import AgentListResponse
from api.schemas.responses import CursorPage
from api.services.agent_service import AgentService
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
