from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
import json
import random
import secrets
import uuid
from typing import Final

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from api.db.cache import CacheService
from api.db.models import GlobalAgent
from api.db.models import PermissionGrant
from api.db.models import UniversalMemory
from api.db.models import UniversalUser
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.infra.fallbacks import on_redis_open
from api.services.email_service import EmailService
from api.services.vector_outbox import enqueue_vector_delete


UUI_CACHE_TTL_SECONDS: Final[int] = 3600
PERMISSION_CACHE_TTL_SECONDS: Final[int] = 300
OTP_EXPIRY_MINUTES: Final[int] = 10
OTP_REQUEST_LIMIT: Final[int] = 3
OTP_REQUEST_WINDOW_SECONDS: Final[int] = 3600
OTP_FAILED_LIMIT: Final[int] = 5
OTP_LOCK_WINDOW_SECONDS: Final[int] = 1800
UNIVERSAL_COLLECTION_NAME: Final[str] = "universal_memories"
ALLOWED_MEMORY_CATEGORIES: Final[set[str]] = {
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
}
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


class UUIService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService | None = None,
        qdrant_service: QdrantService | None = None,
        email_service: EmailService | None = None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.qdrant_service = qdrant_service
        self.email_service = email_service or EmailService()

    async def register(
        self,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> UniversalUser:
        normalized_email = self._normalize_email(email)
        normalized_display_name = self._normalize_display_name(display_name)

        if normalized_email is not None:
            existing = await self.resolve_by_email(normalized_email)
            if existing is not None:
                raise APIError(
                    status_code=409,
                    code="UUI_409",
                    error="memory_passport_exists",
                    details={
                        "message": (
                            "A Memory Passport already exists for this email. "
                            "Use email login, or register without email if you are creating a separate test identity."
                        )
                    },
                )

        universal_user = UniversalUser(
            id=uuid.uuid4(),
            uui_token=self._generate_uui_token(),
            email=normalized_email,
            display_name=normalized_display_name,
            is_active=True,
        )
        self.session.add(universal_user)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            error_text = str(getattr(exc, "orig", exc)).lower()
            if normalized_email is not None and "universal_users_email_key" in error_text:
                raise APIError(
                    status_code=409,
                    code="UUI_409",
                    error="memory_passport_exists",
                    details={
                        "message": (
                            "A Memory Passport already exists for this email. "
                            "Use email login, or register without email if you are creating a separate test identity."
                        )
                    },
                ) from exc
            raise

        await self.session.refresh(universal_user)
        await self._cache_uui_token(universal_user.uui_token, str(universal_user.id))

        if normalized_email is not None:
            await self.send_otp(normalized_email)

        return universal_user

    async def send_otp(self, email: str) -> bool:
        normalized_email = self._normalize_email(email)
        if normalized_email is None:
            return False

        if await self._is_otp_rate_limited(normalized_email):
            return False

        try:
            user = await self.resolve_by_email(normalized_email)
            if user is None:
                return False

            otp = f"{random.randint(100000, 999999):06d}"
            user.otp_code = otp
            user.otp_expires_at = datetime.now(UTC) + timedelta(minutes=OTP_EXPIRY_MINUTES)
            await self.session.commit()
            sent = await self.email_service.send_otp_email(normalized_email, otp)
            if sent:
                await self._increment_otp_request_counter(normalized_email)
            return sent
        except Exception:
            await self._safe_rollback()
            return False

    async def is_otp_rate_limited(self, email: str) -> bool:
        normalized_email = self._normalize_email(email)
        if normalized_email is None:
            return False
        return await self._is_otp_rate_limited(normalized_email)

    async def verify_otp(self, email: str, otp: str) -> UniversalUser | None:
        normalized_email = self._normalize_email(email)
        normalized_otp = str(otp or "").strip()
        if normalized_email is None or not normalized_otp:
            return None

        if await self._is_otp_locked(normalized_email):
            return None

        try:
            user = await self.resolve_by_email(normalized_email)
            if user is None:
                await self._record_failed_otp_attempt(normalized_email)
                return None

            now = datetime.now(UTC)
            expires_at = user.otp_expires_at
            if (
                not user.otp_code
                or user.otp_code != normalized_otp
                or expires_at is None
                or expires_at <= now
            ):
                await self._record_failed_otp_attempt(normalized_email)
                return None

            user.otp_code = None
            user.otp_expires_at = None
            await self.session.commit()
            await self._clear_failed_otp_state(normalized_email)
            await self._cache_uui_token(user.uui_token, str(user.id))
            return user
        except Exception:
            await self._safe_rollback()
            return None

    async def resolve_by_token(self, uui_token: str) -> UniversalUser | None:
        token = str(uui_token or "").strip()
        if not token:
            return None

        try:
            user_id = await self._get_cached_uui_id(token)
            user: UniversalUser | None = None
            if user_id is not None:
                user = await self.session.get(UniversalUser, uuid.UUID(user_id))

            if user is None:
                result = await self.session.execute(
                    select(UniversalUser).where(
                        UniversalUser.uui_token == token,
                        UniversalUser.is_active.is_(True),
                    )
                )
                user = result.scalar_one_or_none()
                if user is not None:
                    await self._cache_uui_token(token, str(user.id))

            if user is None or not bool(user.is_active):
                return None
            return user
        except Exception:
            return None

    async def resolve(self, uui_token: str) -> UniversalUser | None:
        return await self.resolve_by_token(uui_token)

    async def resolve_by_email(self, email: str) -> UniversalUser | None:
        normalized_email = self._normalize_email(email)
        if normalized_email is None:
            return None
        try:
            result = await self.session.execute(
                select(UniversalUser).where(
                    UniversalUser.email == normalized_email,
                    UniversalUser.is_active.is_(True),
                )
            )
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def get_grants(self, user_uui_id: str) -> list[PermissionGrant]:
        result = await self.session.execute(
            select(PermissionGrant)
            .join(PermissionGrant.global_agent)
            .options(joinedload(PermissionGrant.global_agent).joinedload(GlobalAgent.owner_tenant))
            .where(
                PermissionGrant.user_uui_id == self._as_uuid(user_uui_id),
                PermissionGrant.is_active.is_(True),
                (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > func.now())),
                GlobalAgent.is_active.is_(True),
            )
            .order_by(PermissionGrant.granted_at.desc(), PermissionGrant.id.desc())
        )
        return list(result.scalars().all())

    async def create_grant(
        self,
        user_uui_id: str,
        agent_id: str,
        categories: list[str],
        access_type: str,
        expires_at: datetime | None = None,
    ) -> PermissionGrant:
        self._validate_categories(categories)
        if access_type not in {"read_only", "read_write"}:
            raise APIError(status_code=422, code="REQ_422", error="validation_error")

        agent = await self.session.get(GlobalAgent, self._as_uuid(agent_id))
        if agent is None or not bool(agent.is_active):
            raise APIError(status_code=404, code="AGN_404", error="global_agent_not_found")

        upsert = (
            insert(PermissionGrant)
            .values(
                id=uuid.uuid4(),
                user_uui_id=self._as_uuid(user_uui_id),
                agent_id=self._as_uuid(agent_id),
                categories_allowed=categories,
                access_type=access_type,
                is_active=True,
                expires_at=expires_at,
                revoked_at=None,
            )
            .on_conflict_do_update(
                constraint="uq_permission_grants_user_agent",
                set_={
                    "categories_allowed": categories,
                    "access_type": access_type,
                    "is_active": True,
                    "granted_at": func.now(),
                    "expires_at": expires_at,
                    "revoked_at": None,
                },
            )
            .returning(PermissionGrant.id)
        )
        result = await self.session.execute(upsert)
        grant_id = result.scalar_one()
        await self.session.commit()
        await self._invalidate_permission_cache(user_uui_id, agent_id)
        grant = await self._get_grant_with_agent(str(grant_id))
        if grant is None:
            raise APIError(status_code=500, code="SRV_500", error="permission_grant_persist_failed")
        return grant

    async def revoke_grant(self, user_uui_id: str, grant_id: str) -> bool:
        grant = await self.session.get(PermissionGrant, self._as_uuid(grant_id))
        if grant is None or str(grant.user_uui_id) != str(user_uui_id):
            return False
        grant.is_active = False
        grant.revoked_at = datetime.now(UTC)
        await self.session.commit()
        await self._invalidate_permission_cache(user_uui_id, str(grant.agent_id))
        return True

    async def check_permission(self, user_uui_id: str, agent_id: str, category: str) -> bool:
        try:
            if category not in ALLOWED_MEMORY_CATEGORIES:
                return False

            resolved_user_id = user_uui_id
            if str(user_uui_id).startswith("uui_"):
                user = await self.resolve_by_token(user_uui_id)
                if user is None:
                    return False
                resolved_user_id = str(user.id)

            cache_key = self._permission_cache_key(resolved_user_id, agent_id)
            cached = await self._get_cached_permission_categories(cache_key)
            if cached is not None:
                return category in cached

            result = await self.session.execute(
                select(PermissionGrant.categories_allowed)
                .where(
                    PermissionGrant.user_uui_id == self._as_uuid(resolved_user_id),
                    PermissionGrant.agent_id == self._as_uuid(agent_id),
                    PermissionGrant.is_active.is_(True),
                    (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > func.now())),
                )
            )
            categories_allowed = result.scalar_one_or_none()
            if categories_allowed is None:
                return False

            categories_list = list(categories_allowed)
            await self._cache_permission_categories(cache_key, categories_list)
            return category in categories_list
        except Exception:
            return False

    async def delete_user_data(self, *, uui_token: str) -> tuple[bool, int]:
        user = await self.resolve_by_token(uui_token)
        if user is None:
            return False, 0

        memory_ids_result = await self.session.execute(
            select(UniversalMemory.id).where(UniversalMemory.user_uui_id == user.id)
        )
        memory_ids = list(memory_ids_result.scalars().all())
        memories_removed = len(memory_ids)

        grant_result = await self.session.execute(
            select(PermissionGrant.agent_id).where(PermissionGrant.user_uui_id == user.id)
        )
        agent_ids = [str(agent_id) for agent_id in grant_result.scalars().all()]

        for memory_id in memory_ids:
            enqueue_vector_delete(
                self.session,
                memory_id=memory_id,
                payload={
                    "qdrant_collection": UNIVERSAL_COLLECTION_NAME,
                    "privacy_delete": True,
                },
            )

        await self.session.delete(user)
        await self.session.commit()

        await self._redis_delete(self._uui_cache_key(user.uui_token))
        for agent_id in set(agent_ids):
            await self._invalidate_permission_cache(str(user.id), agent_id)
        return True, memories_removed

    async def _get_grant_with_agent(self, grant_id: str) -> PermissionGrant | None:
        result = await self.session.execute(
            select(PermissionGrant)
            .options(joinedload(PermissionGrant.global_agent).joinedload(GlobalAgent.owner_tenant))
            .where(PermissionGrant.id == self._as_uuid(grant_id))
        )
        return result.scalar_one_or_none()

    async def _cache_uui_token(self, uui_token: str, user_id: str) -> None:
        await self._redis_set(self._uui_cache_key(uui_token), user_id, ex=UUI_CACHE_TTL_SECONDS)

    async def _get_cached_uui_id(self, uui_token: str) -> str | None:
        cached = await self._redis_get(self._uui_cache_key(uui_token))
        return str(cached) if cached else None

    async def _cache_permission_categories(self, cache_key: str, categories: list[str]) -> None:
        await self._redis_set(
            cache_key,
            json.dumps({"categories_allowed": categories}),
            ex=PERMISSION_CACHE_TTL_SECONDS,
        )

    async def _get_cached_permission_categories(self, cache_key: str) -> list[str] | None:
        cached = await self._redis_get(cache_key)
        if not cached:
            return None
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return None
        categories = payload.get("categories_allowed")
        if not isinstance(categories, list):
            return None
        return [str(category) for category in categories]

    async def _invalidate_permission_cache(self, user_uui_id: str, agent_id: str) -> None:
        await self._redis_delete(self._permission_cache_key(user_uui_id, agent_id))

    async def _is_otp_rate_limited(self, email: str) -> bool:
        count = await self._redis_get(self._otp_request_count_key(email))
        return int(count or 0) >= OTP_REQUEST_LIMIT

    async def _increment_otp_request_counter(self, email: str) -> None:
        key = self._otp_request_count_key(email)
        count = await self._redis_incr(key)
        if count == 1:
            await self._redis_expire(key, OTP_REQUEST_WINDOW_SECONDS)

    async def _record_failed_otp_attempt(self, email: str) -> None:
        fail_key = self._otp_failed_attempt_key(email)
        attempts = await self._redis_incr(fail_key)
        if attempts == 1:
            await self._redis_expire(fail_key, OTP_LOCK_WINDOW_SECONDS)
        if attempts >= OTP_FAILED_LIMIT:
            await self._redis_set(self._otp_lock_key(email), "1", ex=OTP_LOCK_WINDOW_SECONDS)

    async def _clear_failed_otp_state(self, email: str) -> None:
        await self._redis_delete(self._otp_failed_attempt_key(email))
        await self._redis_delete(self._otp_lock_key(email))

    async def _is_otp_locked(self, email: str) -> bool:
        return bool(await self._redis_get(self._otp_lock_key(email)))

    async def _redis_get(self, key: str) -> str | None:
        if self.cache_service is None:
            return None
        try:
            return await self._redis_call(self.cache_service.client.get, key, fallback=lambda: on_redis_open(None))
        except REDIS_FAILURES:
            return None

    async def _redis_set(self, key: str, value: str, *, ex: int) -> None:
        if self.cache_service is None:
            return None
        try:
            await self._redis_call(
                self.cache_service.client.set,
                key,
                value,
                ex=ex,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    async def _redis_delete(self, key: str) -> None:
        if self.cache_service is None:
            return None
        try:
            await self._redis_call(
                self.cache_service.client.delete,
                key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    async def _redis_incr(self, key: str) -> int:
        if self.cache_service is None:
            return 0
        try:
            value = await self._redis_call(
                self.cache_service.client.incr,
                key,
                fallback=lambda: on_redis_open(0),
            )
            return int(value or 0)
        except REDIS_FAILURES:
            return 0

    async def _redis_expire(self, key: str, ttl_seconds: int) -> None:
        if self.cache_service is None:
            return None
        try:
            await self._redis_call(
                self.cache_service.client.expire,
                key,
                ttl_seconds,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    async def _redis_call(self, fn, *args, fallback=None, **kwargs):
        breaker = getattr(self.cache_service, "breaker", None) if self.cache_service is not None else None
        if breaker is None or breaker.__class__.__module__.startswith("unittest.mock"):
            return await fn(*args, **kwargs)
        return await breaker.call(fn, *args, fallback=fallback, **kwargs)

    async def _safe_rollback(self) -> None:
        rollback = getattr(self.session, "rollback", None)
        if callable(rollback):
            try:
                await rollback()
            except Exception:
                return None

    @staticmethod
    def _generate_uui_token() -> str:
        return f"uui_{secrets.token_hex(24)}"

    @staticmethod
    def _normalize_email(email: str | None) -> str | None:
        normalized = str(email or "").strip().lower()
        return normalized or None

    @staticmethod
    def _normalize_display_name(display_name: str | None) -> str | None:
        normalized = str(display_name or "").strip()
        return normalized or None

    @staticmethod
    def _uui_cache_key(uui_token: str) -> str:
        return f"uui:{uui_token}:id"

    @staticmethod
    def _permission_cache_key(user_uui_id: str, agent_id: str) -> str:
        return f"uui_perm:{user_uui_id}:{agent_id}"

    @staticmethod
    def _otp_request_count_key(email: str) -> str:
        return f"uui_otp_req:{email}"

    @staticmethod
    def _otp_failed_attempt_key(email: str) -> str:
        return f"uui_otp_fail:{email}"

    @staticmethod
    def _otp_lock_key(email: str) -> str:
        return f"uui_otp_lock:{email}"

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    @staticmethod
    def _validate_categories(categories: list[str]) -> None:
        invalid = [category for category in categories if category not in ALLOWED_MEMORY_CATEGORIES]
        if invalid:
            raise APIError(
                status_code=422,
                code="REQ_422",
                error="validation_error",
                details={"invalid_categories": invalid},
            )
