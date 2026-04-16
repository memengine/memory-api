from __future__ import annotations

import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ApiKey
from api.utils.crypto import api_key_prefix
from api.utils.crypto import hash_api_key


class ApiKeyService:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def list_api_keys(
        self,
        *,
        tenant_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[ApiKey], str | None, int]:
        result = await self.session.execute(
            select(ApiKey)
            .where(
                ApiKey.tenant_id == uuid.UUID(tenant_id),
                ApiKey.is_active.is_(True),
            )
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
        keys = list(result.scalars().all())
        total = len(keys)
        sliced = _slice_with_cursor(keys, cursor=cursor, limit=limit)
        next_cursor = str(sliced[-1].id) if len(sliced) == limit and total > len(sliced) else None
        return sliced, next_cursor, total

    async def create_api_key(
        self,
        *,
        tenant_id: str,
        name: str,
        permissions: list[str],
        rate_limit_per_minute: int,
    ) -> tuple[ApiKey, str]:
        raw_key = f"mem_{secrets.token_urlsafe(24)}"
        api_key = ApiKey(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            user_id=None,
            key_hash=hash_api_key(raw_key),
            key_prefix=api_key_prefix(raw_key),
            name=name,
            permissions=permissions,
            rate_limit_per_minute=rate_limit_per_minute,
            is_active=True,
        )
        self.session.add(api_key)
        await self.session.commit()
        await self.session.refresh(api_key)
        return api_key, raw_key

    async def revoke_api_key(
        self,
        *,
        tenant_id: str,
        api_key_id: str,
    ) -> bool:
        api_key = await self.session.get(ApiKey, uuid.UUID(api_key_id))
        if api_key is None or api_key.tenant_id != uuid.UUID(tenant_id):
            return False
        api_key.is_active = False
        await self.session.commit()
        return True


def _slice_with_cursor(items: list, *, cursor: str | None, limit: int) -> list:
    if not cursor:
        return items[:limit]

    for index, item in enumerate(items):
        if str(item.id) == cursor:
            return items[index + 1 : index + 1 + limit]
    return items[:limit]
