"""Private durable contracts; not evidence of natural-language accuracy."""
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.infra import protected_storage
from api.services.preference_intent_admission_service import (
    AdmissionOutcome,
    PreferenceAdmissionUnavailable,
    PreferenceIntentAdmission,
    PreferenceIntentAdmissionService,
    _hash,
)
from api.services.preference_snapshot_service import snapshot_binding_version
from api.tasks.extraction_tasks import _job_status_snapshot
from tests.unit.test_preference_choice_service import request, snapshot
from tests.unit.test_protected_storage import FakeCipher


def captured():
    snap = snapshot()
    snap = replace(snap, binding_version=snapshot_binding_version(snap))
    return PreferenceIntentAdmission(capture_id=uuid.uuid4(), tenant_id=snap.tenant_id,
        proxy_user_id=snap.proxy_user_id, source_conversation_id=uuid.uuid4(),
        transcript_sha256=_hash([{"role": "user", "content": request()["quote"]}]),
        catalog_sha256=_hash(snap.catalog.model_dump(mode="json")), snapshot=snap)


def test_round_trip_is_closed_and_detached():
    record = captured()
    assert PreferenceIntentAdmission.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        record.state = "applied"


@pytest.mark.parametrize("location", ["root", "snapshot", "identity", "target"])
def test_nested_unknown_fields_rejected(location):
    raw = captured().model_dump(mode="json")
    target = {"root": raw, "snapshot": raw["snapshot"],
        "identity": raw["snapshot"]["identities"][0],
        "target": raw["snapshot"]["identities"][0]["target"]}[location]
    target["model_authority"] = 100
    with pytest.raises(ValidationError):
        PreferenceIntentAdmission.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", [
    {"state": "validated"}, {"state": "applied"}, {"requests": [request()]},
    {"decision_sha256": "a" * 64}, {"tenant_id": str(uuid.uuid4())},
])
def test_inconsistent_lifecycle_or_principal_rejected(change):
    raw = {**captured().model_dump(mode="json"), **change}
    with pytest.raises(ValidationError):
        PreferenceIntentAdmission.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", ["missing", "duplicate", "binding", "authority", "strict_authority", "strict_current"])
def test_invalid_persisted_snapshot_rejected(change):
    raw = captured().model_dump(mode="json")
    snap = raw["snapshot"]
    if change == "missing":
        snap["identities"] = []
    elif change == "duplicate":
        snap["identities"] *= 2
    elif change == "binding":
        snap["binding_version"] = "b" * 64
    else:
        snap["identities"][0]["target"][{"authority": "authority", "strict_authority": "authority", "strict_current": "is_current"}[change]] = {
            "authority": 101, "strict_authority": "20", "strict_current": "true"}[change]
    with pytest.raises(ValidationError):
        PreferenceIntentAdmission.model_validate_json(json.dumps(raw))


def test_terminal_outcome_counts_and_strict_counters():
    raw = captured().model_dump(mode="json")
    raw.update(state="applied", requests=[request()], decision_sha256=_hash([request()]),
        outcome={"withheld": 1, "reaffirmed": 0, "deduplicated": 0, "reviews_buffered": 1,
            "reviews_capacity_rejected": 0, "reviews_closed_replays": 0, "rejected": {}})
    assert PreferenceIntentAdmission.model_validate_json(json.dumps(raw)).outcome.withheld == 1
    for field, value in (("withheld", True), ("deduplicated", 1), ("reviews_buffered", 2),
                         ("rejected", {"arbitrary_model_reason": 1})):
        bad = {**raw, "outcome": {**raw["outcome"], field: value}}
        with pytest.raises(ValidationError):
            PreferenceIntentAdmission.model_validate_json(json.dumps(bad))
    with pytest.raises(ValidationError):
        AdmissionOutcome.model_validate({**raw["outcome"], "rejected": {"preference_target_stale": True}})


def test_storage_is_tenant_and_job_bound_and_private(monkeypatch):
    cipher, record = FakeCipher(), captured()
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: cipher)
    job = SimpleNamespace(id=uuid.uuid4(), tenant_id=record.tenant_id,
        preference_intent_admission=None, preference_intent_admission_envelope=None)
    PreferenceIntentAdmissionService._write(job, record)
    assert PreferenceIntentAdmissionService._read(job) == record
    assert cipher.calls[0]["record_type"] == "preference-intent-admission"
    assert cipher.calls[0]["record_id"] == str(job.id)
    assert cipher.calls[0]["tenant_id"] == str(job.tenant_id)
    assert job.preference_intent_admission_envelope == {"version": 1, "ciphertext": "opaque"}
    assert _job_status_snapshot({"job_id": str(job.id),
        "preference_intent_admission": job.preference_intent_admission,
        "preference_intent_admission_envelope": job.preference_intent_admission_envelope}) == {"job_id": str(job.id)}


@pytest.mark.parametrize("raw,envelope", [(None, {}), ({"bad": True}, None), ({"oversized": "x" * 250000}, None)])
def test_unreadable_record_is_not_reset(raw, envelope):
    job = SimpleNamespace(preference_intent_admission=raw, preference_intent_admission_envelope=envelope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="record_inconsistent"):
        PreferenceIntentAdmissionService._read(job)
    assert job.preference_intent_admission is raw
