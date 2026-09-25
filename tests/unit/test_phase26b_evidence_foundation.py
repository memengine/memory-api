from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.db.models import ConversationEvidenceTurn, MemoryProposal
from api.schemas.requests import ConversationMessageRequest, ProposedMemoryRequest
from api.services.memory_service import MemoryService
from api.tasks.extraction_tasks import (
    _active_proposal_context,
    queue_wait_ms,
    retry_countdown_seconds,
)


def test_evidence_and_proposal_models_have_scope_integrity_constraints() -> None:
    evidence_constraints = {constraint.name for constraint in ConversationEvidenceTurn.__table__.constraints}
    proposal_constraints = {constraint.name for constraint in MemoryProposal.__table__.constraints}
    evidence_indexes = {index.name for index in ConversationEvidenceTurn.__table__.indexes}
    proposal_indexes = {index.name for index in MemoryProposal.__table__.indexes}

    assert "uq_conversation_evidence_turn_scope" in evidence_constraints
    assert "uq_memory_proposals_assistant_turn" in proposal_constraints
    assert "uq_memory_proposals_group_ordinal" in proposal_constraints
    assert "ck_memory_proposals_status" in proposal_constraints
    assert "ix_conversation_evidence_turns_scope_created" in evidence_indexes
    assert "ix_memory_proposals_active_scope" in proposal_indexes


def test_memory_proposal_marker_requires_explicit_assistant_output() -> None:
    proposal = ConversationMessageRequest(
        role="assistant",
        content="I can remember that you prefer a one-line diagnosis.",
        source_kind="assistant_output",
        is_memory_proposal=True,
    )

    assert proposal.is_memory_proposal is True

    with pytest.raises(ValidationError):
        ConversationMessageRequest(
            role="user",
            content="Please remember this preference.",
            source_kind="direct_user_input",
            is_memory_proposal=True,
        )


def test_structured_proposal_claim_must_be_visible_and_proposal_scoped() -> None:
    proposal = ConversationMessageRequest(
        role="assistant",
        content="I can remember this durable claim: User prefers concise answers.",
        source_kind="assistant_output",
        is_memory_proposal=True,
        proposed_memory=ProposedMemoryRequest(
            content="User prefers concise answers.",
            category="preference",
        ),
    )

    assert proposal.proposed_memory is not None
    assert proposal.proposed_memory.content == "User prefers concise answers."
    with pytest.raises(ValidationError, match="appear verbatim"):
        ConversationMessageRequest(
            role="assistant",
            content="I can remember a different claim.",
            source_kind="assistant_output",
            is_memory_proposal=True,
            proposed_memory=ProposedMemoryRequest(
                content="User prefers concise answers.",
                category="preference",
            ),
        )
    with pytest.raises(ValidationError, match="is_memory_proposal"):
        ConversationMessageRequest(
            role="assistant",
            content="User prefers concise answers.",
            source_kind="assistant_output",
            proposed_memory=ProposedMemoryRequest(
                content="User prefers concise answers.",
                category="preference",
            ),
        )


def test_memory_proposal_model_has_structured_claim_columns() -> None:
    assert MemoryProposal.__table__.c.proposed_memory_content.type.length == 500
    assert MemoryProposal.__table__.c.proposed_memory_category.type.length == 50


def test_worker_context_preserves_structured_proposal_claim() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    content_hash = "a" * 64
    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        proposal_group_id=str(job_id),
        proposal_ordinal=1,
        assistant_turn_id="proposal-turn-1",
        assistant_content_sha256=content_hash,
        proposed_memory_content="User prefers concise answers.",
        proposed_memory_category="preference",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [proposal]

    class _Session:
        def execute(self, _statement):
            return _Result()

    messages = [
        {
            "role": "assistant",
            "source_kind": "assistant_output",
            "turn_id": "proposal-turn-1",
            "turn_content_sha256": content_hash,
        }
    ]

    context = _active_proposal_context(
        _Session(),
        job_payload={
            "tenant_id": str(tenant_id),
            "proxy_user_id": str(proxy_user_id),
            "job_id": str(job_id),
            "external_conversation_id": "chat-1",
        },
        messages=messages,
    )

    assert context[0]["memory_content"] == "User prefers concise answers."
    assert context[0]["memory_category"] == "preference"
    assert context[0]["turn_index"] == 0
    assert messages[0]["_registered_memory_proposal"] is True


def test_conversation_scope_uses_external_id_or_job_fallback() -> None:
    assert MemoryService._conversation_scope_id(
        {"job_id": "job-a", "external_conversation_id": "chat-42"}
    ) == "external:chat-42"
    assert MemoryService._conversation_scope_id({"job_id": "job-a"}) == "job:job-a"


@pytest.mark.parametrize("limits", ["openai=invalid", "openai=0", "gemni=2", "openai=2,openai=3"])
def test_invalid_provider_limits_fail_configuration(limits):
    from api.settings import Settings
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LLM_PROVIDER_CONCURRENCY_LIMITS=limits)


def test_retry_countdown_is_bounded_and_deterministic_per_job() -> None:
    first = retry_countdown_seconds(job_id="job-a", attempts=2)
    repeat = retry_countdown_seconds(job_id="job-a", attempts=2)
    other = retry_countdown_seconds(job_id="job-b", attempts=2)

    assert first == repeat
    assert 120 <= first <= 144
    assert 120 <= other <= 144


def test_queue_wait_uses_persisted_enqueue_time_and_never_goes_negative() -> None:
    started_at = datetime(2026, 9, 16, 10, 0, 5, tzinfo=UTC)
    queued_at = started_at - timedelta(seconds=2, milliseconds=250)

    assert queue_wait_ms({"queued_at": queued_at.isoformat()}, started_at=started_at) == 2250
    assert queue_wait_ms({"queued_at": (started_at + timedelta(seconds=1)).isoformat()}, started_at=started_at) == 0
    assert queue_wait_ms({"queued_at": "not-a-timestamp"}, started_at=started_at) == 0
