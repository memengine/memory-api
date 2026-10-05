"""Target bounds are backend projections, not model accuracy or read permission."""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.services.preference_fact_transaction import FactTarget
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _hash,
)
from api.services.preference_intent_contract import PreferenceIntentReply
from api.services.runtime_preference_footprint import (
    RuntimePreferenceFootprint,
    check_footprint,
    decision_footprint,
)
from tests.unit.test_preference_intent_contract import intent, offered, reply


def inputs(raw=None, *, targets=(), plans=(), complete=True, state="validated"):
    snapshot = offered()
    record = SimpleNamespace(state=state, decision_sha256="d" * 64,
        capture_id=uuid.uuid4(), tenant_id=snapshot.tenant_id, proxy_user_id=snapshot.proxy_user_id,
        snapshot=snapshot, transcript_sha256="a" * 64, runtime_source_sha256=None,
        reply=PreferenceIntentReply.model_validate(raw or reply()),
        fact_context=targets, fact_context_complete=complete, fact_plans=plans)
    job = SimpleNamespace(id=uuid.uuid4(), tenant_id=record.tenant_id,
        proxy_user_id=record.proxy_user_id, runtime_withholding_footprint=None)
    return job, record


def test_projection_uses_offered_identity_and_contains_no_customer_text():
    job, record = inputs()
    footprint = decision_footprint(job, record)
    assert footprint.coverage == "targets"
    assert footprint.preference_fingerprints == (record.snapshot.identities[0].fingerprint,)
    assert footprint.job_id == job.id and footprint.decision_sha256 == record.decision_sha256
    encoded = json.dumps(footprint.model_dump(mode="json"))
    assert "Python" not in encoded and "C++" not in encoded and "examples.language" not in encoded
    assert len(encoded.encode()) < 8192
    assert "authority" not in encoded and "quote" not in encoded


@pytest.mark.parametrize("raw,reason", [
    (reply(preferences=[], nonpreferences=[]), "empty_decision"),
    (reply([intent(action="abstain", value=None)]), "unknown_preference"),
    (reply([intent(action="abstain", value=None, property_key=None, scope_ref=None)]), "unknown_preference"),
    (reply([intent(scope_ref="not_offered")]), "unknown_preference"),
])
def test_unknown_coverage_is_user_wide_and_never_empty_targets(raw, reason):
    job, record = inputs(raw)
    projection = decision_footprint(job, record)
    assert projection.coverage == "user_learned_context" and projection.reason == reason
    assert not projection.preference_fingerprints and not projection.fact_targets


FACT = {"content": "my exam moved", "quote": "my exam moved", "turn_index": 0, "category": "fact"}


@pytest.mark.parametrize("complete,targeted,reason", [
    (False, True, "incomplete_fact_context"), (True, False, "unknown_fact_target"),
])
def test_partial_or_unbound_fact_plan_widens_the_entire_mixed_decision(complete, targeted, reason):
    target = FactTarget(memory_id=uuid.uuid4(), version="b" * 64)
    job, record = inputs(reply(nonpreferences=[FACT]), targets=(target,),
        plans=(SimpleNamespace(target_id=target.memory_id if targeted else None),), complete=complete)
    footprint = decision_footprint(job, record)
    assert footprint.reason == reason and footprint.coverage == "user_learned_context"
    assert not footprint.preference_fingerprints and not footprint.fact_targets


def test_mixed_plan_retains_only_backend_target_ids_and_versions():
    target = FactTarget(memory_id=uuid.uuid4(), version="b" * 64)
    job, record = inputs(reply(nonpreferences=[FACT]), targets=(target,),
        plans=(SimpleNamespace(target_id=target.memory_id),))
    result = decision_footprint(job, record)
    assert result.coverage == "targets" and result.fact_targets == (target,)


def test_projection_is_stable_across_application_but_cannot_be_rebound():
    job, record = inputs()
    job.runtime_withholding_footprint = decision_footprint(job, record).model_dump(mode="json")
    before = copy.deepcopy(job.runtime_withholding_footprint)
    record.state = "applied"
    assert check_footprint(job, record).model_dump(mode="json") == before
    record.decision_sha256 = "e" * 64
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_footprint_changed"):
        check_footprint(job, record)
    assert job.runtime_withholding_footprint == before


@pytest.mark.parametrize("change", ["digest", "owner", "job", "targets", "extra", "captured"])
def test_corrupt_foreign_or_unfixed_projection_fails_closed(change):
    job, record = inputs()
    raw = decision_footprint(job, record).model_dump(mode="json")
    if change == "digest":
        raw["projection_sha256"] = "0" * 64
    elif change == "owner":
        job.proxy_user_id = uuid.uuid4()
    elif change == "job":
        job.id = uuid.uuid4()
    elif change == "targets":
        raw["preference_fingerprints"] = []
        raw["projection_sha256"] = _hash({k: v for k, v in raw.items() if k != "projection_sha256"})
    elif change == "extra":
        raw["authority_priority"] = 100
    else:
        record.state = "captured"
    job.runtime_withholding_footprint = raw
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_footprint_changed"):
        check_footprint(job, record)


def test_missing_legacy_projection_is_not_synthesized_on_read():
    job, record = inputs()
    assert check_footprint(job, record) is None and job.runtime_withholding_footprint is None


def test_duplicate_targets_are_invalid_even_with_a_recomputed_digest():
    job, record = inputs()
    raw = decision_footprint(job, record).model_dump(mode="json")
    raw["preference_fingerprints"] *= 2
    raw["projection_sha256"] = _hash({k: v for k, v in raw.items() if k != "projection_sha256"})
    with pytest.raises(ValidationError):
        RuntimePreferenceFootprint.model_validate(raw)
