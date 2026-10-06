"""Per-item writer outcomes and a separate residual projection; no model calls."""
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.services.preference_fact_transaction import FactReadOutcome, FactTarget
from api.services.runtime_fact_visibility import residual_footprint
from api.services.runtime_preference_footprint import decision_footprint
from tests.unit.test_preference_intent_contract import reply
from tests.unit.test_runtime_preference_footprint import inputs


def context(outcomes, *, complete=True):
    target = FactTarget(memory_id=uuid.UUID(int=9), version="a" * 64)
    raw = reply(preferences=[], nonpreferences=[{"category": "fact", "content": "exam changed",
        "quote": "exam changed", "turn_index": 0}])
    job, record = inputs(raw, targets=(target,), plans=(SimpleNamespace(target_id=target.memory_id),), state="applied", complete=complete)
    job.runtime_withholding_footprint = decision_footprint(job, record).model_dump(mode="json")
    record.receipt = SimpleNamespace(deferred_facts=1, fact_outcomes=outcomes)
    return job, record, target


def test_known_residual_contains_only_unresolved_fact_not_resolved_preferences():
    job, record, target = context(())
    record.receipt.fact_outcomes = (FactReadOutcome(index=0, state="pending", target=target),)
    residual = residual_footprint(job, record)
    assert residual.coverage == "targets" and residual.fact_targets == (target,) and not residual.preference_fingerprints


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_or_legacy_item_coverage_is_not_guessed(legacy):
    job, record, _ = context(None if legacy else (FactReadOutcome(index=0, state="pending", target=None),))
    assert residual_footprint(job, record).coverage == "user_learned_context"


def test_actual_authority_veto_has_no_residual_hold():
    job, record, target = context(())
    record.receipt.fact_outcomes = (FactReadOutcome(index=0, state="authority_protected", target=target),)
    assert residual_footprint(job, record) is None


@pytest.mark.parametrize("state,target", [("stored", "present"), ("rejected", "present"), ("authority_protected", None)])
def test_closed_outcome_cannot_synthesize_wrong_target_shape(state, target):
    with pytest.raises(ValidationError):
        FactReadOutcome(index=0, state=state, target={"memory_id": str(uuid.uuid4()), "version": "a" * 64} if target else None)


def test_counts_cannot_clear_legacy_deferred_unknown_without_per_item_proof():
    job, record, _ = context(None)
    assert residual_footprint(job, record).reason == "unknown_fact_target"
