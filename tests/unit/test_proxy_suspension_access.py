"""Tenant suspension access and audited recovery; synthetic sessions only."""
import uuid
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.db.models import AuditLog
from api.errors import APIError
from api.middleware.rate_limiter import RateLimiterMiddleware
from api.routers.users import unblock_proxy_user
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService, ProxyUserBlockedError
from tests.unit.test_proxy_user_service import make_proxy_user, FakeResult


@pytest.mark.asyncio
async def test_named_blocked_list_denies_before_count_or_data():
    service = object.__new__(MemoryService)
    service.session = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: Row(is_blocked=True))))
    with pytest.raises(APIError) as err:
        await service.list_memories(requested_user_id=None, authenticated_user_id=None, tenant_id=str(uuid.uuid4()),
            external_user_id="blocked", cursor=None, limit=20, categories=[], agent_id=None)
    assert err.value.status_code == 403 and err.value.code == "PRX_403"
    assert service.session.execute.await_count == 1


@pytest.mark.asyncio
async def test_job_access_rechecks_current_owner_not_cached_status():
    tenant, owner = str(uuid.uuid4()), str(uuid.uuid4())
    row = Row(is_blocked=True)
    service = object.__new__(MemoryService)
    service.session = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: row)))
    job = {"tenant_id": tenant, "proxy_user_id": owner, "status": "completed"}
    with pytest.raises(APIError) as err:
        await service.check_job_owner_access(job)
    assert err.value.code == "PRX_403"
    row.is_blocked = False
    await service.check_job_owner_access(job)
    service.session.execute.assert_awaited()


@pytest.mark.parametrize("job", [{}, {"tenant_id": str(uuid.uuid4()), "proxy_user_id": "invalid"}])
@pytest.mark.asyncio
async def test_unverifiable_job_owner_is_hidden(job):
    service = object.__new__(MemoryService)
    service.session = Row(execute=AsyncMock())
    with pytest.raises(APIError) as err:
        await service.check_job_owner_access(job)
    assert err.value.code == "JOB_404"
    service.session.execute.assert_not_awaited()


@pytest.mark.parametrize("operation", ["block", "unblock", "stats", "export"])
def test_proxy_administration_requires_admin_permission(operation):
    assert RateLimiterMiddleware._required_permission("POST" if operation in {"block","unblock"} else "GET",
        "/v1/users/synthetic/" + operation) == "admin"
    assert RateLimiterMiddleware._required_permission("GET", "/v1/users/me/export") == "read"


@pytest.mark.asyncio
async def test_readwrite_cannot_unblock_and_admin_response_is_explicit():
    service = Row(unblock=AsyncMock(return_value=True))
    request = Row(state=Row(auth_scheme="apikey", api_key_permissions=("read","write"),
        api_key_id=str(uuid.uuid4()), user_id=None, request_id="synthetic"), headers={})
    tenant = str(uuid.uuid4())
    with pytest.raises(APIError) as err:
        await unblock_proxy_user(request, "synthetic", tenant, service)
    assert err.value.code == "AUTH_403"
    service.unblock.assert_not_awaited()
    request.state.api_key_permissions = ("admin",)
    response = await unblock_proxy_user(request, "synthetic", tenant, service)
    assert response.data.blocked is False
    assert service.unblock.await_args.kwargs["actor"]["api_key_id"] == request.state.api_key_id


@pytest.mark.asyncio
async def test_unblock_is_audited_atomic_and_idempotent_without_touching_history():
    owner = make_proxy_user(is_blocked=True)
    rows = []
    session = Row(execute=AsyncMock(return_value=FakeResult(scalar_one_or_none=owner)),
        add=rows.append, commit=AsyncMock(), rollback=AsyncMock())
    service = ProxyUserService(session=session, cache_service=MagicMock())
    service._invalidate_cache = AsyncMock()
    actor = {"api_key_id": str(uuid.uuid4())}
    assert await service.unblock(str(owner.tenant_id), owner.external_user_id, actor=actor)
    assert owner.is_blocked is False
    assert len(rows) == 1 and isinstance(rows[0], AuditLog)
    assert rows[0].old_value == {"is_blocked": True} and rows[0].new_value == {"is_blocked": False}
    assert rows[0].metadata_json["actor"] == actor
    assert rows[0].metadata_json["operation"] == "unblock"
    session.commit.assert_awaited_once()
    assert await service.unblock(str(owner.tenant_id), owner.external_user_id, actor=actor)
    assert len(rows) == 1 and session.commit.await_count == 1
    assert service._invalidate_cache.await_count == 2


@pytest.mark.asyncio
async def test_stale_unblocked_cache_cannot_override_sql_block():
    owner = make_proxy_user(is_blocked=True)
    service = ProxyUserService(session=Row(execute=AsyncMock(return_value=FakeResult(scalar_one_or_none=owner))),
        cache_service=MagicMock())
    service._get_cached_proxy_user = AsyncMock(return_value=Row(is_blocked=False))
    with pytest.raises(ProxyUserBlockedError):
        await service.resolve(str(owner.tenant_id), owner.external_user_id)


@pytest.mark.parametrize("role,allowed", [(None,False),("org:member",False),("org:admin",True)])
def test_dashboard_proxy_admin_requires_verified_org_role(role, allowed):
    from api.routers.users import _proxy_admin_actor
    request = Row(state=Row(auth_scheme="bearer",auth_method="clerk_jwt",org_role=role, user_id="synthetic"))
    if allowed:
        assert _proxy_admin_actor(request)["user_id"] == "synthetic"
    else:
        with pytest.raises(APIError): _proxy_admin_actor(request)


@pytest.mark.asyncio
async def test_id_reads_and_history_filter_blocked_owner():
    from api.services.version_service import VersionService
    statements = []
    async def execute(statement):
        statements.append(statement)
        # Synthetic empty result models the SQL exclusion of this blocked owner.
        return Row(scalar_one_or_none=lambda: None)
    session = Row(execute=execute)
    service = object.__new__(MemoryService)
    service.session = session
    memory_id, tenant = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(APIError) as err:
        await service.get_memory(authenticated_user_id=None, memory_id=memory_id, tenant_id=tenant)
    assert err.value.code == "MEM_404"
    with pytest.raises(PermissionError):
        await VersionService(session).get_history(memory_id=memory_id, tenant_id=tenant)
    assert all("proxy_users.is_blocked IS false" in str(statement) for statement in statements)


@pytest.mark.asyncio
async def test_job_route_never_skips_suspension_check_after_tenant_check():
    from api.routers.memories import get_memory_job_status
    tenant = str(uuid.uuid4())
    service = Row(get_job_status=AsyncMock(return_value={"tenant_id": tenant}),
        check_job_owner_access=AsyncMock(side_effect=APIError(status_code=403, code="PRX_403",error="proxy_user_blocked")))
    request = Row(state=Row(tenant_id=tenant,user_id=None),headers={})
    with pytest.raises(APIError) as err:
        await get_memory_job_status(request,uuid.uuid4(),service)
    assert err.value.code == "PRX_403"


@pytest.mark.parametrize("version,role", [(1,"org:admin"),(2,"admin"),(2,"member")])
def test_verified_clerk_role_is_taken_from_signed_token(version,role):
    import time
    import fakeredis.aioredis
    from fastapi import Request
    from fastapi.testclient import TestClient
    from jose import jwt
    from tests.unit.test_auth_middleware import build_rsa_jwks, build_test_app, build_http_client, FakeSessionFactory
    private, jwks = build_rsa_jwks()
    org, tenant = "org_synthetic_suspension", uuid.uuid4()
    claims = {"sub":"synthetic", "iss":"https://clerk.example.test", "exp":int(time.time())+3600}
    claims.update({"org_id":org,"org_role":role} if version==1 else {"v":2,"o":{"id":org,"rol":role}})
    token = jwt.encode(claims,private,algorithm="RS256",headers={"kid":"test-key-id"})
    app = build_test_app(session_factory=FakeSessionFactory({org:tenant}),
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),http_client=build_http_client(jwks))
    @app.get("/role-proof")
    async def role_proof(request:Request):
        return {"role":getattr(request.state,"org_role",None), "tenant":request.state.tenant_id}
    with TestClient(app) as client:
        response=client.get("/role-proof",headers={"Authorization":"Bearer "+token})
    assert response.status_code==200
    assert response.json()=={"role":role if version==1 else "org:"+role,"tenant":str(tenant)}
