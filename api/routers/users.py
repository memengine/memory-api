from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response

from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_authenticated_user_id
from api.dependencies import get_proxy_user_service
from api.dependencies import get_user_service
from api.schemas.requests import UserSettingsUpdateRequest
from api.schemas.responses import ApiKeyData
from api.schemas.responses import AgentData
from api.schemas.responses import MemoryData
from api.schemas.responses import ProxyUserBlockData
from api.schemas.responses import ProxyUserBlockResponse
from api.schemas.responses import ProxyUserDeleteData
from api.schemas.responses import ProxyUserDeleteResponse
from api.schemas.responses import ProxyUserStatsData
from api.schemas.responses import ProxyUserStatsResponse
from api.schemas.responses import UserDeleteData
from api.schemas.responses import UserDeleteResponse
from api.schemas.responses import UserExportData
from api.schemas.responses import UserExportResponse
from api.schemas.responses import UserProfileData
from api.schemas.responses import UserProfileResponse
from api.schemas.responses import UserSettingsData
from api.schemas.responses import UserSettingsResponse
from api.services.proxy_user_service import ProxyUserService
from api.services.user_service import UserService
from api.middleware.versioning import register_deprecated_field
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.routers.memories import _memory_to_data
from api.routers.tenant import DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE
from api.routers.tenant import DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET


router = APIRouter(prefix="/v1/users", tags=["users"])


def _user_profile_data(user, memory_count: int, storage_bytes: int) -> UserProfileData:
    return UserProfileData(
        id=str(user.id),
        external_id=user.external_id,
        email=user.email,
        settings=user.settings or {},
        memory_count=memory_count,
        storage_bytes=storage_bytes,
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_current_user_profile(
    request: Request,
    user_service: Annotated[UserService, Depends(get_user_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> UserProfileResponse:
    """Fetch the current authenticated user profile.

    Parameters: none beyond auth context.
    Responses: user identity, settings, memory count, and storage usage.
    """
    user, memory_count, storage_bytes = await user_service.get_profile(
        authenticated_user_id=authenticated_user_id
    )
    return UserProfileResponse(
        data=_user_profile_data(user, memory_count, storage_bytes),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/me/settings", response_model=UserSettingsResponse)
async def update_user_settings(
    request: Request,
    payload: UserSettingsUpdateRequest,
    user_service: Annotated[UserService, Depends(get_user_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> UserSettingsResponse:
    """Update user memory settings.

    Parameters: settings object in the request body.
    Responses: the updated user profile snapshot.
    """
    user, memory_count, storage_bytes = await user_service.update_settings(
        authenticated_user_id=authenticated_user_id,
        settings=payload.settings,
    )
    return UserSettingsResponse(
        data=UserSettingsData(settings=user.settings or {}),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me/export", response_model=UserExportResponse)
async def export_user_data(
    request: Request,
    response: Response,
    user_service: Annotated[UserService, Depends(get_user_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> UserExportResponse:
    """Export the authenticated user's data.

    Parameters: none beyond auth context.
    Responses: user profile plus exported memories, API keys, and agents as JSON.
    """
    user, memory_count, storage_bytes, memories, api_keys, agents = await user_service.export_user_data(
        authenticated_user_id=authenticated_user_id
    )
    response.headers["content-disposition"] = 'attachment; filename="memoryos-export.json"'
    return UserExportResponse(
        data=UserExportData(
            user=_user_profile_data(user, memory_count, storage_bytes),
            memories=[_memory_to_data(memory) for memory in memories],
            api_keys=[
                ApiKeyData(
                    id=str(api_key.id),
                    name=api_key.name,
                    permissions=list(api_key.permissions or []),
                    rate_limit_per_minute=int(api_key.rate_limit_per_minute),
                    created_at=api_key.created_at,
                    last_used_at=api_key.last_used_at,
                    is_active=bool(api_key.is_active),
                )
                for api_key in api_keys
            ],
            agents=[
                AgentData(
                    id=str(agent.id),
                    name=agent.name,
                    description=agent.description,
                    memory_scope=agent.memory_scope.value,
                    created_at=agent.created_at,
                )
                for agent in agents
            ],
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/me", response_model=UserDeleteResponse)
async def delete_user(
    request: Request,
    user_service: Annotated[UserService, Depends(get_user_service)],
    authenticated_user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> UserDeleteResponse:
    """Delete the authenticated user and cascade their stored data.

    Parameters: none beyond auth context.
    Responses: deletion status and number of memories removed.
    """
    deleted, memories_removed = await user_service.delete_user(
        authenticated_user_id=authenticated_user_id
    )
    return UserDeleteResponse(
        data=UserDeleteData(deleted=deleted, memories_removed=memories_removed),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/{external_user_id}/stats", response_model=ProxyUserStatsResponse)
async def get_proxy_user_stats(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserStatsResponse:
    """Fetch tenant-scoped stats for a proxy user.

    Parameters: external user id in the path. Tenant identity is derived from API key auth.
    Responses: memory count and activity timestamps for the tenant's proxy user.
    """
    stats = await proxy_user_service.get_stats(tenant_id=tenant_id, external_user_id=external_user_id)
    register_deprecated_field(
        request,
        field_path="GET /v1/users/{external_user_id}/stats response.data.user_id",
        header_field_name="user_id",
        sunset_at=DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET,
        migration_guide_url=DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE,
        replacement_field="external_user_id",
    )
    return ProxyUserStatsResponse(
        data=ProxyUserStatsData(
            external_user_id=external_user_id,
            user_id=external_user_id,
            memory_count=stats.memory_count,
            last_active_at=stats.last_active_at,
            created_at=stats.created_at,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/{external_user_id}", response_model=ProxyUserDeleteResponse)
async def delete_proxy_user(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserDeleteResponse:
    """Delete a tenant-scoped proxy user and all associated memories.

    Parameters: external user id in the path. Tenant identity is derived from API key auth.
    Responses: deletion status and number of memories removed for GDPR cleanup.
    """
    memories_removed = await proxy_user_service.delete_all_memories(
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )
    return ProxyUserDeleteResponse(
        data=ProxyUserDeleteData(
            deleted=True,
            memories_removed=memories_removed,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/{external_user_id}/block", response_model=ProxyUserBlockResponse)
async def block_proxy_user(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserBlockResponse:
    """Block a tenant-scoped proxy user from future memory operations.

    Parameters: external user id in the path. Tenant identity is derived from API key auth.
    Responses: whether the proxy user is now blocked.
    """
    blocked = await proxy_user_service.block(
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )
    return ProxyUserBlockResponse(
        data=ProxyUserBlockData(blocked=blocked),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
