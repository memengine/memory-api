from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from starlette.requests import Request

from api.errors import APIError
from api.routers.mcp import (
    TenantMCPClarificationAnswerRequest,
    TenantMCPSessionContextRequest,
    _is_self_scoped_public_clarification,
    _public_tenant_mcp_external_user_id,
    job_status_for_public_tenant_mcp,
    session_context_for_public_tenant_mcp,
)


def _request(*, tenant_id: str = "tenant_a", user_id: str = "user_a", marker: str = "public-v1") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/mcp/tenant/context",
            "headers": [(b"x-memoryos-mcp-client", marker.encode())],
        }
    )
    request.state.tenant_id = tenant_id
    request.state.user_id = user_id
    return request


def test_public_tenant_mcp_profile_is_stable_and_tenant_scoped() -> None:
    first = _public_tenant_mcp_external_user_id(_request())
    assert first == _public_tenant_mcp_external_user_id(_request())
    assert first != _public_tenant_mcp_external_user_id(_request(tenant_id="tenant_b"))
    assert first != _public_tenant_mcp_external_user_id(_request(user_id="user_b"))
    assert first.startswith("mcp:")


def test_public_tenant_mcp_profile_requires_public_marker() -> None:
    with pytest.raises(APIError) as error:
        _public_tenant_mcp_external_user_id(_request(marker=""))

    assert error.value.code == "MCP_403"


def test_session_context_request_has_a_small_bounded_default_budget() -> None:
    payload = TenantMCPSessionContextRequest()

    assert payload.context_max_tokens == 180
    with pytest.raises(ValueError):
        TenantMCPSessionContextRequest(context_max_tokens=401)


def test_session_context_uses_a_fixed_query_and_server_derived_identity() -> None:
    request = _request()
    expected = object()
    with patch("api.routers.mcp.retrieve_memories", new_callable=AsyncMock, return_value=expected) as retrieve:
        result = asyncio.run(
            session_context_for_public_tenant_mcp(
                request=request,
                payload=TenantMCPSessionContextRequest(),
                retriever_service=object(),
                proxy_user_service=object(),
                context_builder=object(),
                cache_service=object(),
                session=object(),
            )
        )

    assert result is expected
    payload = retrieve.await_args.kwargs["payload"]
    assert payload.external_user_id == _public_tenant_mcp_external_user_id(request)
    assert payload.query.startswith("Stable user preferences")
    assert payload.context_max_tokens == 180


def test_public_mcp_job_status_requires_the_derived_profile_to_own_the_job() -> None:
    request = _request()
    owned_proxy = SimpleNamespace(id="proxy-own")
    job_id = UUID("9897dc48-6bb3-4d3b-bb16-a23233db2711")
    service = SimpleNamespace(
        get_job_status=AsyncMock(
            return_value={
                "tenant_id": "tenant_a",
                "proxy_user_id": "proxy-own",
                "job_id": str(job_id),
                "status": "completed",
                "memories_created": 1,
            }
        )
    )
    proxy_user_service = SimpleNamespace(resolve=AsyncMock(return_value=owned_proxy))

    response = asyncio.run(
        job_status_for_public_tenant_mcp(
            request=request,
            job_id=job_id,
            memory_service=service,
            proxy_user_service=proxy_user_service,
        )
    )

    assert response.data.job_id == str(job_id)
    assert response.data.memories_created == 1


@pytest.mark.parametrize(
    ("tenant_id", "proxy_user_id"),
    [("tenant_other", "proxy-own"), ("tenant_a", "proxy-other"), (None, None)],
)
def test_public_mcp_job_status_hides_foreign_or_unowned_jobs(
    tenant_id: str | None,
    proxy_user_id: str | None,
) -> None:
    request = _request()
    service = SimpleNamespace(
        get_job_status=AsyncMock(
            return_value={
                "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "job_id": str(uuid.uuid4()),
                "status": "queued",
            }
        )
    )
    proxy_user_service = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(id="proxy-own")))

    with pytest.raises(APIError) as error:
        asyncio.run(
            job_status_for_public_tenant_mcp(
                request=request,
                job_id=uuid.uuid4(),
                memory_service=service,
                proxy_user_service=proxy_user_service,
            )
        )

    assert error.value.status_code == 404
    assert error.value.code == "JOB_404"


def test_public_clarification_requires_both_conflict_memories_to_be_self_owned() -> None:
    own_proxy_id = "proxy_a"
    self_owned = SimpleNamespace(
        proxy_user_id=own_proxy_id,
    )
    clarification = SimpleNamespace(
        proxy_user_id=own_proxy_id,
        conflict=SimpleNamespace(
            resolution_path="user_session",
            user_a_memory=self_owned,
            user_b_memory=self_owned,
        ),
    )
    assert _is_self_scoped_public_clarification(
        clarification,
        proxy_user_id=own_proxy_id,
    )

    clarification.conflict.user_b_memory = SimpleNamespace(proxy_user_id="proxy_other")
    assert not _is_self_scoped_public_clarification(
        clarification,
        proxy_user_id=own_proxy_id,
    )


def test_public_clarification_answer_is_limited_to_explicit_user_choices() -> None:
    assert TenantMCPClarificationAnswerRequest(answer="both").answer == "both"
    with pytest.raises(ValueError):
        TenantMCPClarificationAnswerRequest(answer="keep newest")
