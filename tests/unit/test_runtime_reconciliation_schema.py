"""A reconciliation is a closed quarantine record, never an activation decision."""
import pytest
from pydantic import ValidationError

from api.schemas.runtime_reconciliation import RuntimeReconciliationRecord


def raw(**extra):
    return {"hold_scope": "affected_context", "footprint_sha256": "a" * 64,
        "reason_code": "source_expired", "operator_reason": " source TTL elapsed ",
        "request_id": "isolated-check", **extra}


def test_record_defaults_are_operator_credential_not_human_authority():
    record = RuntimeReconciliationRecord.model_validate(raw())
    assert record.outcome == "quarantined" and record.actor_credential_class == "admin_secret"
    assert record.operator_reason == "source TTL elapsed"


@pytest.mark.parametrize("extra", [
    {"outcome": "applied"}, {"actor_credential_class": "human"}, {"authority": 100},
    {"footprint_sha256": None}, {"hold_scope": "user_learned_context"},
    {"reason_code": "record_invalid"}, {"reason_code": "source_binding_changed"},
    {"operator_reason": " "}, {"operator_reason": "x" * 513}, {"request_id": "x" * 129},
])
def test_closed_schema_rejects_activation_or_ambiguous_narrowing(extra):
    with pytest.raises(ValidationError):
        RuntimeReconciliationRecord.model_validate(raw(**extra))


def test_unknown_evidence_requires_full_hold_without_target_digest():
    record = RuntimeReconciliationRecord.model_validate(raw(hold_scope="user_learned_context",
        footprint_sha256=None, reason_code="record_invalid"))
    assert record.hold_scope == "user_learned_context"
