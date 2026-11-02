"""Admin-only service correction discovery, explicit preview and CAS decision."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_authenticated_tenant_id, get_db_session
from api.errors import APIError
from api.routers.common import get_request_id, utc_now
from api.schemas.service_corrections import (
    ServiceCorrectionDecision,
    ServiceCorrectionDecisionResponse,
    ServiceCorrectionListResponse,
    ServiceCorrectionPreviewResponse,
)
from api.services.service_correction_service import ServiceCorrectionService

router = APIRouter(prefix="/service-corrections", tags=["service corrections"])


def actor_key(request: Request) -> UUID:
    # This bounded operator API deliberately excludes bearer/ordinary RW keys.
    # SQL below also rechecks active tenant admin permission on every request.
    if getattr(request.state, "auth_scheme", None) != "apikey":
        raise APIError(status_code=403, code="AUTH_403", error="active_tenant_admin_api_key_required")
    try:
        return UUID(str(request.state.api_key_id))
    except (ValueError, AttributeError):
        raise APIError(status_code=403, code="AUTH_403", error="active_tenant_admin_api_key_required") from None


@router.get("", response_model=ServiceCorrectionListResponse)
async def list_service_corrections(request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    external_user_id: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=50), after: UUID | None = None,
    tenant_id: str = Depends(get_authenticated_tenant_id)):
    items, cursor = await ServiceCorrectionService(session).list_pending(tenant_id=UUID(tenant_id),
        actor_id=actor_key(request), external_user_id=external_user_id, limit=limit, after=after)
    return ServiceCorrectionListResponse(data=items, next_cursor=cursor,
        request_id=get_request_id(request), timestamp=utc_now())


@router.get("/{candidate_id}", response_model=ServiceCorrectionPreviewResponse)
async def preview_service_correction(request: Request, candidate_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)], target_memory_id: UUID,
    external_user_id: str = Query(min_length=1, max_length=255),
    tenant_id: str = Depends(get_authenticated_tenant_id)):
    view = await ServiceCorrectionService(session).preview(tenant_id=UUID(tenant_id),
        actor_id=actor_key(request), external_user_id=external_user_id,
        candidate_id=candidate_id, target_id=target_memory_id)
    return ServiceCorrectionPreviewResponse(data=view, request_id=get_request_id(request), timestamp=utc_now())


@router.post("/{candidate_id}/decision", response_model=ServiceCorrectionDecisionResponse)
async def decide_service_correction(request: Request, candidate_id: UUID, payload: ServiceCorrectionDecision,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: str = Depends(get_authenticated_tenant_id)):
    result = await ServiceCorrectionService(session).resolve(tenant_id=UUID(tenant_id),
        actor_id=actor_key(request), candidate_id=candidate_id, payload=payload)
    return ServiceCorrectionDecisionResponse(data=result, request_id=get_request_id(request), timestamp=utc_now())
