from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import User
from api.errors import APIError


async def get_user_by_identifier(
    session: AsyncSession,
    user_identifier: str,
) -> User | None:
    try:
        user_uuid = uuid.UUID(str(user_identifier))
    except (TypeError, ValueError):
        user_uuid = None

    if user_uuid is not None:
        user = await session.get(User, user_uuid)
        if user is not None:
            return user

    result = await session.execute(
        select(User).where(User.external_id == str(user_identifier))
    )
    return result.scalar_one_or_none()


async def require_user_by_identifier(
    session: AsyncSession,
    user_identifier: str,
) -> User:
    user = await get_user_by_identifier(session, user_identifier)
    if user is None:
        raise APIError(
            status_code=404,
            code="USR_404",
            error="user_not_found",
            details={"user_id": user_identifier},
        )
    return user


async def resolve_authorized_user(
    session: AsyncSession,
    *,
    requested_user_id: str | None,
    authenticated_user_id: str,
) -> User:
    authenticated_user = await require_user_by_identifier(session, authenticated_user_id)
    if requested_user_id is None:
        return authenticated_user

    requested_user = await require_user_by_identifier(session, requested_user_id)
    if requested_user.id != authenticated_user.id:
        raise APIError(
            status_code=403,
            code="AUTH_403",
            error="forbidden",
            details={"requested_user_id": requested_user_id},
        )
    return requested_user
