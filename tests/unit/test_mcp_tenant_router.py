from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from starlette.requests import Request

from api.errors import APIError
from api.routers.mcp import (
    TenantMCPClarificationAnswerRequest,
    TenantMCPRememberRequest,
    TenantMCPSessionContextRequest,
    _is_self_scoped_public_clarification,
    _mcp_memory_explanation,
    _mcp_memory_summary,
    _public_tenant_mcp_external_user_id,
    job_status_for_public_tenant_mcp,
    memories_for_public_tenant_mcp,
    remember_for_public_tenant_mcp,
    session_context_for_public_tenant_mcp,
)
from api.services.provenance_service import build_provenance_snapshot


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


def test_public_mcp_remember_uses_server_owned_assertion_classification() -> None:
    request = _request()
    request.state.api_key_id = None
    payload = TenantMCPRememberRequest(
        messages=[{"role": "user", "content": "Use UTC incident timestamps."}],
        conversation_id="opaque-client-reference",
    )
    with patch("api.routers.mcp.add_memories", new_callable=AsyncMock, return_value=object()) as add:
        result = asyncio.run(
            remember_for_public_tenant_mcp(
                request=request,
                response=SimpleNamespace(),
                payload=payload,
                memory_service=object(),
                proxy_user_service=object(),
                quality_gate_service=object(),
                idempotency_key="idempotency-1",
            )
        )

    assert result is add.return_value
    forwarded = add.await_args.kwargs["payload"]
    assert forwarded.metadata == {}
    assert forwarded.evidence_mode == "client_assertion"
    assert add.await_args.kwargs["trusted_submission_kind"] == "mcp_client_assertion"


def test_public_mcp_remember_rejects_unstructured_metadata() -> None:
    with pytest.raises(ValueError):
        TenantMCPRememberRequest(
            messages=[{"role": "user", "content": "Remember this."}],
            metadata={"attestation": "memoryos_attested"},
        )


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
                "result_memory_ids": ["memory-created-by-job"],
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
    assert response.data.result_memory_ids == ["memory-created-by-job"]


def _memory_for_mcp_summary(*, content: str, provenance: dict[str, object]) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        category=SimpleNamespace(value="preference"),
        content=content,
        created_at=now,
        updated_at=now,
        is_archived=False,
        importance_score=5.0,
        confidence_score=0.8,
        source_conversation_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
        metadata_json={"provenance": provenance, "unbounded": "x" * 50_000},
    )


def test_mcp_memory_summary_excludes_unbounded_metadata_and_processing() -> None:
    memory = _memory_for_mcp_summary(
        content="x" * 500,
        provenance={
            "attestation": "client_asserted",
            "external_conversation_id": "release-check-001",
            "processing": {"extraction_metadata": "x" * 50_000},
            "payload_hash": "sensitive-internal-value",
        },
    )

    summary = _mcp_memory_summary(memory)

    assert len(summary.content_preview) == 241
    assert summary.content_truncated is True
    assert summary.provenance_summary is not None
    assert summary.provenance_summary.attestation == "client_asserted"
    assert "processing" not in summary.provenance_summary.model_dump(exclude_none=True)
    assert "payload_hash" not in summary.provenance_summary.model_dump(exclude_none=True)


def test_mcp_memory_explanation_does_not_expose_internal_source_ids() -> None:
    memory = _memory_for_mcp_summary(
        content="Use an incident timestamp.",
        provenance={"source_event_id": "event-public", "service": "memoryos-mcp"},
    )

    explanation = _mcp_memory_explanation(memory)

    assert "source_conversation_id" not in explanation.model_dump(exclude_none=True)
    assert "source_event_id" not in explanation.model_dump(exclude_none=True)
    assert explanation.provenance_summary.source_event_id == "event-public"


def test_server_owned_mcp_source_event_carries_client_assertion_authority() -> None:
    now = datetime.now(UTC)
    event = SimpleNamespace(
        id=uuid.uuid4(),
        source_event_id="job-1",
        source_service="memoryos-mcp",
        writer_id=None,
        writer=SimpleNamespace(authority_rules={"default_priority": 90}),
        observed_at=now,
        received_at=now,
        payload_hash="hash",
        scope={},
        evidence_refs=[],
        processing_metadata={
            "trusted_evidence_policy": {
                "attestation": "client_asserted",
                "authority_priority": 20,
                "authority_rules": {"default_priority": 20},
            }
        },
    )

    snapshot = build_provenance_snapshot(event)

    assert snapshot["service"] == "memoryos-mcp"
    assert snapshot["attestation"] == "client_asserted"
    assert snapshot["authority_priority"] == 20
    assert snapshot["authority_rules"] == {"default_priority": 20}


def test_public_mcp_memory_list_enforces_a_total_inline_payload_budget() -> None:
    request = _request()
    memories = [
        _memory_for_mcp_summary(
            content="x" * 10_000,
            provenance={
                "attestation": "client_asserted",
                "external_conversation_id": "conversation-" + ("x" * 10_000),
                "processing": {"raw": "x" * 50_000},
            },
        )
        for _ in range(10)
    ]
    service = SimpleNamespace(list_memories=AsyncMock(return_value=(memories, None, 10)))

    response = asyncio.run(
        memories_for_public_tenant_mcp(
            request=request,
            memory_service=service,
            cursor=None,
            limit=10,
            categories=None,
        )
    )

    assert len(response.model_dump_json().encode("utf-8")) < 8_000
    assert 1 <= len(response.data) <= 10
    assert all(item.content_truncated for item in response.data)


def test_public_mcp_memory_list_preserves_paging_when_the_budget_stops_early() -> None:
    request = _request()
    memories = [
        _memory_for_mcp_summary(
            content="x" * 10_000,
            provenance={
                "attestation": "x" * 10_000,
                "external_conversation_id": "x" * 10_000,
                "source_event_id": "x" * 10_000,
                "service": "x" * 10_000,
                "writer_id": "x" * 10_000,
                "observed_at": "x" * 10_000,
                "received_at": "x" * 10_000,
            },
        )
        for _ in range(10)
    ]
    service = SimpleNamespace(list_memories=AsyncMock(return_value=(memories, "backend-next", 20)))

    response = asyncio.run(
        memories_for_public_tenant_mcp(
            request=request,
            memory_service=service,
            cursor=None,
            limit=10,
            categories=None,
        )
    )

    assert len(response.data) < len(memories)
    assert response.pagination.next_cursor == response.data[-1].id


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


def test_public_mcp_job_status_redacts_internal_failure_details() -> None:
    request = _request()
    job_id = UUID("9897dc48-6bb3-4d3b-bb16-a23233db2711")
    service = SimpleNamespace(
        get_job_status=AsyncMock(
            return_value={
                "tenant_id": "tenant_a",
                "proxy_user_id": "proxy-own",
                "job_id": str(job_id),
                "status": "failed",
                "error": "postgresql://secret@database/private",
                "error_summary": "Connection refused for internal-host",
            }
        )
    )
    proxy_user_service = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(id="proxy-own")))

    response = asyncio.run(
        job_status_for_public_tenant_mcp(
            request=request,
            job_id=job_id,
            memory_service=service,
            proxy_user_service=proxy_user_service,
        )
    )

    assert response.data.error == "memory_processing_failed"
    assert "internal-host" not in response.data.error_summary


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
