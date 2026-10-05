"""Private v2 persistence shape; controlled replies are not semantic evaluation."""
import json
import uuid

import pytest
from pydantic import ValidationError

from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    PreferenceIntentAdmission,
    PreferenceIntentAdmissionService,
    _hash,
)
from api.services.preference_intent_transaction import (
    IntentTransactionRecord,
    PreferenceIntentTransactionService,
)
from tests.unit.test_preference_intent_admission_service import captured
from tests.unit.test_preference_intent_contract import reply


def record_raw():
    record = captured().model_dump(mode="json")
    for key in ("requests", "outcome"):
        record.pop(key)
    return {**record, "schema_version": 2, "reply": None, "receipt": None}


def validated_raw():
    raw = record_raw()
    return {**raw, "state": "validated", "reply": reply(), "decision_sha256": _hash(reply())}


def applied_raw():
    return {**validated_raw(), "state": "applied", "receipt": {
        "created_memory_ids": [str(uuid.uuid4())], "withheld": 0, "reaffirmed": 0,
        "pending_candidates_buffered": 0, "reviews_capacity_rejected": 0, "reviews_closed_replays": 0,
        "deferred_facts": 0, "rejected": {}}}


@pytest.mark.parametrize("raw", [record_raw, validated_raw, applied_raw])
def test_closed_round_trip_and_version_one_is_not_reinterpreted(raw):
    parsed = IntentTransactionRecord.model_validate_json(json.dumps(raw()))
    assert IntentTransactionRecord.model_validate_json(parsed.model_dump_json()) == parsed
    with pytest.raises(ValidationError):
        PreferenceIntentAdmission.model_validate_json(parsed.model_dump_json())
    with pytest.raises(ValidationError):
        parsed.state = "applied"


@pytest.mark.parametrize("changes", [
    {"schema_version": 1}, {"state": "validated"}, {"state": "applied"}, {"state": "rejected"},
    {"reply": reply()}, {"decision_sha256": "a" * 64}, {"model_authority": 100},
    {"tenant_id": str(uuid.uuid4())},
])
def test_invalid_capture_or_state_fails_closed(changes):
    with pytest.raises(ValidationError):
        IntentTransactionRecord.model_validate_json(json.dumps({**record_raw(), **changes}))


@pytest.mark.parametrize("field,value", [
    ("withheld", True), ("withheld", 1), ("deferred_facts", 1), ("pending_candidates_buffered", 2),
    ("reviews_capacity_rejected", 2), ("reviews_closed_replays", 2),
    ("rejected", {"model_said_ok": 1}), ("rejected", {"preference_target_stale": True}),
    ("created_memory_ids", [str(uuid.uuid4())] * 2),
])
def test_receipt_bounds_and_counts(field, value):
    raw = applied_raw()
    raw["receipt"][field] = value
    with pytest.raises(ValidationError):
        IntentTransactionRecord.model_validate_json(json.dumps(raw))


def test_decision_hash_covers_both_lanes():
    raw = validated_raw()
    raw["reply"]["nonpreferences"].append({"content": "exam October 18", "quote": "exam October 18",
        "turn_index": 0, "category": "fact"})
    with pytest.raises(ValidationError, match="decision mismatch"):
        IntentTransactionRecord.model_validate_json(json.dumps(raw))


def test_record_readers_do_not_mix_versions():
    from types import SimpleNamespace
    v1 = SimpleNamespace(preference_intent_admission=captured().model_dump(mode="json"), preference_intent_admission_envelope=None)
    v2 = SimpleNamespace(preference_intent_admission=record_raw(), preference_intent_admission_envelope=None)
    assert PreferenceIntentAdmissionService._read(v1).schema_version == 1
    assert PreferenceIntentTransactionService._read(v2).schema_version == 2
    for service, job in ((PreferenceIntentAdmissionService, v2), (PreferenceIntentTransactionService, v1)):
        with pytest.raises(PreferenceAdmissionUnavailable, match="record_inconsistent"):
            service._read(job)
