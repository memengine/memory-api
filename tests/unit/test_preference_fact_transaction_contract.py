"""Closed v3 persistence tests, not classifier-quality measurements."""
import json
import uuid

import pytest
from pydantic import ValidationError

from api.services.preference_fact_transaction import FactTransactionRecord
from api.services.preference_intent_admission_service import (
    PreferenceIntentAdmission,
    _hash,
)
from api.services.preference_intent_transaction import IntentTransactionRecord
from tests.unit.test_preference_intent_transaction_contract import record_raw


def raw(*, state="captured", action="KEEP_BOTH", target=False):
    value = {**record_raw(), "schema_version": 3, "fact_context_complete": True,
             "fact_context": [], "fact_plans": None}
    if target:
        value["fact_context"] = [{"memory_id": str(uuid.uuid4()), "version": "a" * 64}]
    if state != "captured":
        finding = {"category": "fact", "content": "my exam moved to October 18",
                   "quote": "my exam moved to October 18", "turn_index": 0}
        value.update(state=state, reply={"preferences": [], "nonpreferences": [finding]}, fact_plans=[{
            "target_id": value["fact_context"][0]["memory_id"] if target else None,
            "action": action, "reasoning": "Backend parsed decision", "decision_evidence": None}])
        value["decision_sha256"] = _hash({key: value[key] for key in (
            "reply", "fact_plans", "fact_context", "fact_context_complete")})
    if state == "applied":
        value["receipt"] = {"created_memory_ids": [str(uuid.uuid4())], "withheld": 0, "reaffirmed": 0,
            "pending_candidates_buffered": 0, "reviews_capacity_rejected": 0, "reviews_closed_replays": 0,
            "deferred_facts": 0, "rejected": {}, "facts_created": 1, "facts_rejected": 0}
    return value


@pytest.mark.parametrize("state", ["captured", "validated", "applied"])
def test_v3_roundtrip_and_old_versions_not_reinterpreted(state):
    record = FactTransactionRecord.model_validate_json(json.dumps(raw(state=state)))
    assert FactTransactionRecord.model_validate_json(record.model_dump_json()) == record
    for old in (IntentTransactionRecord, PreferenceIntentAdmission):
        with pytest.raises(ValidationError):
            old.model_validate_json(record.model_dump_json())


@pytest.mark.parametrize("field,value", [
    ("facts_created", 2), ("facts_created", True), ("facts_rejected", 1), ("deferred_facts", 1),
    ("created_memory_ids", []), ("pending_candidates_buffered", 2), ("model_authority", 100),
])
def test_invalid_receipt_counts_or_extra_fields(field, value):
    data = raw(state="applied")
    data["receipt"][field] = value
    with pytest.raises(ValidationError):
        FactTransactionRecord.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field,value", [
    ("action", "MERGE"), ("action", "activate"), ("target_id", str(uuid.uuid4())),
    ("reasoning", "x" * 513), ("decision_evidence", {"raw": "x" * 8192}), ("authority", 100),
])
def test_fact_plan_shape_and_hash_are_closed(field, value):
    data = raw(state="validated")
    data["fact_plans"][0][field] = value
    with pytest.raises(ValidationError):
        FactTransactionRecord.model_validate_json(json.dumps(data))


def test_incomplete_context_never_authorizes_fact_absence():
    data = raw(state="validated")
    data["fact_context_complete"] = False
    data["decision_sha256"] = _hash({key: data[key] for key in (
        "reply", "fact_plans", "fact_context", "fact_context_complete")})
    with pytest.raises(ValidationError, match="incomplete"):
        FactTransactionRecord.model_validate_json(json.dumps(data))


def test_correction_requires_captured_target_and_context_digest_is_bound():
    with pytest.raises(ValidationError, match="requires captured target"):
        FactTransactionRecord.model_validate_json(json.dumps(raw(state="validated", action="UPDATE")))
    data = raw(state="validated", action="UPDATE", target=True)
    assert FactTransactionRecord.model_validate_json(json.dumps(data))
    data["fact_context"][0]["version"] = "b" * 64
    with pytest.raises(ValidationError, match="decision mismatch"):
        FactTransactionRecord.model_validate_json(json.dumps(data))
