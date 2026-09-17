from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from pydantic import ValidationError

from api.db.models import ConversationEvidenceTurn
from api.db.models import MemoryProposal
from api.schemas.requests import ConversationMessageRequest
from api.services.memory_service import MemoryService
from api.tasks.extraction_tasks import queue_wait_ms
from api.tasks.extraction_tasks import retry_countdown_seconds


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
