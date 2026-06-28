from __future__ import annotations

import json
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.db.models import GlobalAgent
from api.db.models import PermissionGrant
from api.db.models import UniversalUser
from api.services.uui_service import UUIService


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return self._rows[0]

    def scalar_one(self):
        if not self._rows:
            raise AssertionError("Expected one row")
        return self._rows[0]


class FakeExecuteResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return self._rows[0]

    def scalar_one(self):
        if not self._rows:
            raise AssertionError("Expected one row")
        return self._rows[0]


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True

    async def delete(self, key: str):
        self.values.pop(key, None)
        return 1

    async def incr(self, key: str):
        current = int(self.values.get(key, "0"))
        current += 1
        self.values[key] = str(current)
        return current

    async def expire(self, key: str, _seconds: int):
        return 1


class FakeEmailService:
    def __init__(self) -> None:
        self.otp_emails: list[tuple[str, str]] = []

    async def send_otp_email(self, to_email: str, otp: str) -> bool:
        self.otp_emails.append((to_email, otp))
        return True


class FakeSession:
    def __init__(self) -> None:
        self.users: dict[uuid.UUID, UniversalUser] = {}
        self.agents: dict[uuid.UUID, GlobalAgent] = {}
        self.grants: dict[uuid.UUID, PermissionGrant] = {}
        self.commit_calls = 0
        self.refresh_calls = 0

    def add(self, instance) -> None:
        if isinstance(instance, UniversalUser):
            self.users[instance.id] = instance

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, _instance) -> None:
        self.refresh_calls += 1
        return None

    async def get(self, model, key):
        if model is UniversalUser:
            return self.users.get(key)
        if model is GlobalAgent:
            return self.agents.get(key)
        if model is PermissionGrant:
            return self.grants.get(key)
        return None

    async def delete(self, instance) -> None:
        if isinstance(instance, UniversalUser):
            self.users.pop(instance.id, None)

    async def execute(self, query):
        query_text = str(query).lower()

        if "from universal_users" in query_text:
            email = None
            token = None
            for criterion in getattr(query, "_where_criteria", ()):
                left = str(getattr(criterion, "left", ""))
                right = getattr(getattr(criterion, "right", None), "value", None)
                if "universal_users.email" in left:
                    email = right
                if "universal_users.uui_token" in left:
                    token = right
            rows = list(self.users.values())
            if email is not None:
                rows = [user for user in rows if user.email == email and bool(user.is_active)]
            if token is not None:
                rows = [user for user in rows if user.uui_token == token and bool(user.is_active)]
            return FakeExecuteResult(rows)

        if "insert into permission_grants" in query_text:
            params = query.compile().params
            existing = next(
                (
                    grant
                    for grant in self.grants.values()
                    if grant.user_uui_id == params["user_uui_id"] and grant.agent_id == params["agent_id"]
                ),
                None,
            )
            if existing is None:
                grant = PermissionGrant(
                    id=params["id"],
                    user_uui_id=params["user_uui_id"],
                    agent_id=params["agent_id"],
                    categories_allowed=list(params["categories_allowed"]),
                    access_type=params["access_type"],
                    granted_at=datetime.now(UTC),
                    expires_at=params.get("expires_at"),
                    is_active=True,
                    revoked_at=None,
                )
                self.grants[grant.id] = grant
                return FakeExecuteResult([grant.id])

            existing.categories_allowed = list(params["categories_allowed"])
            existing.access_type = params["access_type"]
            existing.is_active = True
            existing.revoked_at = None
            existing.expires_at = params.get("expires_at")
            existing.granted_at = datetime.now(UTC)
            return FakeExecuteResult([existing.id])

        if "from permission_grants" in query_text and "global_agents" in query_text:
            user_uui_id = None
            grant_id = None
            for criterion in getattr(query, "_where_criteria", ()):
                left = str(getattr(criterion, "left", ""))
                right = getattr(getattr(criterion, "right", None), "value", None)
                if "permission_grants.user_uui_id" in left:
                    user_uui_id = right
                if "permission_grants.id" in left:
                    grant_id = right
            rows = []
            for grant in self.grants.values():
                if grant_id is not None and grant.id != grant_id:
                    continue
                if user_uui_id is not None and grant.user_uui_id != user_uui_id:
                    continue
                if user_uui_id is not None and not grant.is_active:
                    continue
                grant.global_agent = self.agents[grant.agent_id]
                rows.append(grant)
            return FakeExecuteResult(rows)

        if "from permission_grants" in query_text and "categories_allowed" in query_text:
            user_uui_id = None
            agent_id = None
            for criterion in getattr(query, "_where_criteria", ()):
                left = str(getattr(criterion, "left", ""))
                right = getattr(getattr(criterion, "right", None), "value", None)
                if "permission_grants.user_uui_id" in left:
                    user_uui_id = right
                elif "permission_grants.agent_id" in left:
                    agent_id = right
            rows = [
                grant.categories_allowed
                for grant in self.grants.values()
                if grant.user_uui_id == user_uui_id and grant.agent_id == agent_id and grant.is_active
            ]
            return FakeExecuteResult(rows)

        raise AssertionError(f"Unexpected query: {query}")


def make_cache_service() -> SimpleNamespace:
    return SimpleNamespace(client=FakeRedisClient(), breaker=MagicMock())


def make_user(*, email: str | None, otp_code: str | None = None, otp_expires_at: datetime | None = None) -> UniversalUser:
    return UniversalUser(
        id=uuid.uuid4(),
        uui_token=f"uui_{uuid.uuid4().hex}{uuid.uuid4().hex[:12]}",
        email=email,
        display_name="Test User",
        otp_code=otp_code,
        otp_expires_at=otp_expires_at,
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=0,
    )


@pytest.mark.asyncio
async def test_register_creates_user_with_uui_token() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    email_service = FakeEmailService()
    service = UUIService(session=session, cache_service=cache_service, email_service=email_service)

    user = await service.register(email=None, display_name="Test User")

    assert user.uui_token.startswith("uui_")
    assert len(user.uui_token) == 52
    assert cache_service.client.values[f"uui:{user.uui_token}:id"] == str(user.id)


@pytest.mark.asyncio
async def test_send_otp_returns_false_for_unknown_email() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    service = UUIService(session=session, cache_service=cache_service, email_service=FakeEmailService())

    sent = await service.send_otp("missing@example.com")

    assert sent is False


@pytest.mark.asyncio
async def test_verify_otp_correct_returns_user() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    email_service = FakeEmailService()
    user = make_user(email="user@example.com")
    session.users[user.id] = user
    service = UUIService(session=session, cache_service=cache_service, email_service=email_service)

    sent = await service.send_otp("user@example.com")
    otp = email_service.otp_emails[0][1]
    verified = await service.verify_otp("user@example.com", otp)

    assert sent is True
    assert verified is not None
    assert verified.id == user.id
    assert user.otp_code is None
    assert user.otp_expires_at is None


@pytest.mark.asyncio
async def test_verify_otp_expired_returns_none() -> None:
    session = FakeSession()
    user = make_user(
        email="user@example.com",
        otp_code="123456",
        otp_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.users[user.id] = user
    service = UUIService(session=session, cache_service=make_cache_service(), email_service=FakeEmailService())

    verified = await service.verify_otp("user@example.com", "123456")

    assert verified is None


@pytest.mark.asyncio
async def test_verify_otp_wrong_returns_none() -> None:
    session = FakeSession()
    user = make_user(
        email="user@example.com",
        otp_code="123456",
        otp_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.users[user.id] = user
    service = UUIService(session=session, cache_service=make_cache_service(), email_service=FakeEmailService())

    verified = await service.verify_otp("user@example.com", "999999")

    assert verified is None


@pytest.mark.asyncio
async def test_check_permission_no_grant_returns_false() -> None:
    session = FakeSession()
    service = UUIService(session=session, cache_service=make_cache_service(), email_service=FakeEmailService())

    allowed = await service.check_permission(str(uuid.uuid4()), str(uuid.uuid4()), "fact")

    assert allowed is False


@pytest.mark.asyncio
async def test_check_permission_grant_exists_returns_true() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    service = UUIService(session=session, cache_service=cache_service, email_service=FakeEmailService())
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session.agents[agent_id] = GlobalAgent(
        id=agent_id,
        owner_tenant_id=uuid.uuid4(),
        name="Agent One",
        description="Test agent",
        logo_url="https://example.com/logo.png",
        website_url="https://example.com",
        default_categories_requested=["fact"],
        redirect_uri="https://example.com/return",
        is_verified=True,
        is_public=True,
        created_at=datetime.now(UTC),
        is_active=True,
    )
    await service.create_grant(str(user_id), str(agent_id), ["fact", "goal"], "read_write")

    allowed = await service.check_permission(str(user_id), str(agent_id), "fact")

    assert allowed is True


@pytest.mark.asyncio
async def test_check_permission_wrong_category_returns_false() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    service = UUIService(session=session, cache_service=cache_service, email_service=FakeEmailService())
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session.agents[agent_id] = GlobalAgent(
        id=agent_id,
        owner_tenant_id=uuid.uuid4(),
        name="Agent One",
        description="Test agent",
        logo_url="https://example.com/logo.png",
        website_url="https://example.com",
        default_categories_requested=["fact"],
        redirect_uri="https://example.com/return",
        is_verified=True,
        is_public=True,
        created_at=datetime.now(UTC),
        is_active=True,
    )
    await service.create_grant(str(user_id), str(agent_id), ["fact"], "read_write")

    allowed = await service.check_permission(str(user_id), str(agent_id), "goal")

    assert allowed is False


@pytest.mark.asyncio
async def test_revoke_clears_redis_cache() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    service = UUIService(session=session, cache_service=cache_service, email_service=FakeEmailService())
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    grant = PermissionGrant(
        id=uuid.uuid4(),
        user_uui_id=user_id,
        agent_id=agent_id,
        categories_allowed=["fact"],
        access_type="read_only",
        granted_at=datetime.now(UTC),
        expires_at=None,
        is_active=True,
        revoked_at=None,
    )
    session.grants[grant.id] = grant
    cache_service.client.values[f"uui_perm:{user_id}:{agent_id}"] = json.dumps({"categories_allowed": ["fact"]})

    revoked = await service.revoke_grant(str(user_id), str(grant.id))

    assert revoked is True
    assert grant.is_active is False
    assert f"uui_perm:{user_id}:{agent_id}" not in cache_service.client.values


@pytest.mark.asyncio
async def test_otp_rate_limit_after_3_requests() -> None:
    session = FakeSession()
    cache_service = make_cache_service()
    email_service = FakeEmailService()
    user = make_user(email="user@example.com")
    session.users[user.id] = user
    service = UUIService(session=session, cache_service=cache_service, email_service=email_service)

    assert await service.send_otp("user@example.com") is True
    assert await service.send_otp("user@example.com") is True
    assert await service.send_otp("user@example.com") is True
    assert await service.send_otp("user@example.com") is False
