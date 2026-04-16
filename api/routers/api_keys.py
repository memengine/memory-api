from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from api.dependencies import get_api_key_service
from api.dependencies import get_authenticated_tenant_id
from api.schemas.requests import ApiKeyCreateRequest
from api.schemas.responses import ApiKeyCreateData
from api.schemas.responses import ApiKeyCreateResponse
from api.schemas.responses import ApiKeyData
from api.schemas.responses import ApiKeyDeleteData
from api.schemas.responses import ApiKeyDeleteResponse
from api.schemas.responses import ApiKeyListResponse
from api.schemas.responses import CursorPage
from api.services.api_key_service import ApiKeyService
from api.routers.common import get_request_id
from api.routers.common import utc_now


router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


def _api_key_to_data(api_key) -> ApiKeyData:
    return ApiKeyData(
        id=str(api_key.id),
        name=api_key.name,
        permissions=list(api_key.permissions or []),
        rate_limit_per_minute=int(api_key.rate_limit_per_minute),
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        is_active=bool(api_key.is_active),
    )


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    request: Request,
    api_key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    cursor: str | None = Query(default=None, description="Cursor from the previous page."),
    limit: int = Query(default=10, ge=1, le=50, description="Maximum number of API keys to return."),
) -> ApiKeyListResponse:
    """List active API keys for the authenticated tenant.

    Parameters: cursor for pagination and a page limit up to 50.
    Responses: paginated API key metadata without exposing raw secrets.
    """
    api_keys, next_cursor, total = await api_key_service.list_api_keys(
        tenant_id=tenant_id,
        cursor=cursor,
        limit=limit,
    )
    return ApiKeyListResponse(
        data=[_api_key_to_data(api_key) for api_key in api_keys],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("", response_model=ApiKeyCreateResponse)
async def create_api_key(
    request: Request,
    payload: ApiKeyCreateRequest,
    api_key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> ApiKeyCreateResponse:
    """Create a new API key for tenant SDK access.

    Parameters: display name, permissions, and per-minute rate limit.
    Responses: created API key metadata plus the raw key shown once.
    """
    api_key, raw_key = await api_key_service.create_api_key(
        tenant_id=tenant_id,
        name=payload.name,
        permissions=payload.permissions,
        rate_limit_per_minute=payload.rate_limit_per_minute,
    )
    return ApiKeyCreateResponse(
        data=ApiKeyCreateData(**_api_key_to_data(api_key).model_dump(), raw_key=raw_key),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/{api_key_id}", response_model=ApiKeyDeleteResponse)
async def revoke_api_key(
    request: Request,
    api_key_id: str,
    api_key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> ApiKeyDeleteResponse:
    """Revoke an API key by id.

    Parameters: API key id from the path.
    Responses: deletion status in the standard envelope.
    """
    deleted = await api_key_service.revoke_api_key(
        tenant_id=tenant_id,
        api_key_id=api_key_id,
    )
    return ApiKeyDeleteResponse(
        data=ApiKeyDeleteData(deleted=deleted),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
