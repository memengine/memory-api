"""Closed, sparse historical proofs; no free-text, authority or model escape hatch."""
import json
import uuid

import pytest
from pydantic import ValidationError

from api.schemas.runtime_target_release import (
    RuntimeTargetReleases,
    preference_key,
    read_target_releases,
)
from api.services.preference_intent_admission_service import _hash
from api.services.runtime_preference_footprint import decision_footprint
from tests.unit.test_runtime_preference_footprint import inputs


def projection():
    job, record = inputs()
    foot = decision_footprint(job, record)
    fp = foot.preference_fingerprints[0]
    target = {"kind": "preference", "key": preference_key(fp), "fingerprint": fp,
        "fact_memory_id": None, "fact_version": None, "source_job_id": str(uuid.uuid4()),
        "source_capture_id": str(uuid.uuid4()), "source_sha256": "a" * 64, "decision_sha256": "b" * 64,
        "outcome": "activated", "result_memory_id": str(uuid.uuid4()), "result_version": "c" * 64,
        "claim_id": str(uuid.uuid4()), "revision_id": str(uuid.uuid4()), "audit_id": str(uuid.uuid4())}
    raw = {"schema_version": 1, "job_id": str(foot.job_id), "tenant_id": str(foot.tenant_id),
        "proxy_user_id": str(foot.proxy_user_id), "footprint_sha256": foot.projection_sha256, "targets": [target]}
    return foot, raw


def seal(raw):
    return {**raw, "projection_sha256": _hash(raw)}


def test_bound_proof_is_value_free_and_has_no_semantic_confidence_or_authority():
    foot, raw = projection()
    record = RuntimeTargetReleases.model_validate(seal(raw))
    assert len(read_target_releases(record.model_dump(mode="json"), foot)) == 1
    assert all(word not in json.dumps(record.model_dump(mode="json")) for word in ("Python", "C++", "confidence", "authority"))


@pytest.mark.parametrize("change", ["authority", "outcome", "fact_shape", "key", "same_job", "duplicate", "empty", "owner", "footprint", "digest"])
def test_corruption_or_foreign_binding_cannot_release(change):
    foot, raw = projection()
    if change == "authority":
        raw["targets"][0]["authority"] = 100
    elif change == "outcome":
        raw["targets"][0]["outcome"] = "rejected"
    elif change == "fact_shape":
        raw["targets"][0]["kind"] = "fact"
    elif change == "key":
        raw["targets"][0]["key"] = "0" * 64
    elif change == "same_job":
        raw["targets"][0]["source_job_id"] = raw["job_id"]
    elif change == "duplicate":
        raw["targets"] *= 2
    elif change == "empty":
        raw["targets"] = []
    elif change == "owner":
        raw["proxy_user_id"] = str(uuid.uuid4())
    elif change == "footprint":
        raw["footprint_sha256"] = "f" * 64
    sealed = seal(raw)
    if change == "digest":
        sealed["projection_sha256"] = "0" * 64
    with pytest.raises((ValidationError, ValueError)):
        read_target_releases(sealed, foot)


def test_missing_legacy_proof_is_not_coverage():
    foot, _ = projection()
    assert read_target_releases(None, foot) == ()
