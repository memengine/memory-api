"""Server-written, value-free evidence of actual committed target outcomes."""
import uuid
from typing import Literal

from pydantic import Field, model_validator

from api.services.preference_choice_service import Version
from api.services.preference_intent_admission_service import _Closed, _hash, _json


def preference_key(fingerprint):
    return _hash({"preference": fingerprint})


def fact_key(memory_id, version):
    return _hash({"fact": str(memory_id), "version": version})


class TargetRelease(_Closed):
    kind: Literal["preference", "fact"]
    key: Version
    fingerprint: Version | None
    fact_memory_id: uuid.UUID | None
    fact_version: Version | None
    source_job_id: uuid.UUID
    source_capture_id: uuid.UUID
    source_sha256: Version
    decision_sha256: Version
    outcome: Literal["activated", "reaffirmed", "superseded"]
    result_memory_id: uuid.UUID
    result_version: Version
    claim_id: uuid.UUID | None
    revision_id: uuid.UUID | None
    audit_id: uuid.UUID

    @model_validator(mode="after")
    def consistent_target(self):
        if self.kind == "preference":
            if (self.fingerprint is None or self.fact_memory_id is not None or self.fact_version is not None
                or self.claim_id is None or self.revision_id is None or self.outcome == "superseded"
                or self.key != preference_key(self.fingerprint)):
                raise ValueError("invalid preference outcome")
        elif (self.fingerprint is not None or self.fact_memory_id is None or self.fact_version is None
            or self.claim_id is not None or self.revision_id is not None or self.outcome != "superseded"
            or self.result_memory_id == self.fact_memory_id or self.key != fact_key(self.fact_memory_id, self.fact_version)):
            raise ValueError("invalid fact outcome")
        return self


class RuntimeTargetReleases(_Closed):
    schema_version: Literal[1] = 1
    job_id: uuid.UUID
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    footprint_sha256: Version
    targets: tuple[TargetRelease, ...] = Field(min_length=1, max_length=16)
    projection_sha256: Version

    @model_validator(mode="after")
    def consistent_release(self):
        raw = self.model_dump(mode="json")
        if (len({t.key for t in self.targets}) != len(self.targets)
            or any(t.source_job_id == self.job_id for t in self.targets)
            or len(_json(raw).encode()) > 32768
            or self.projection_sha256 != _hash({k: v for k, v in raw.items() if k != "projection_sha256"})):
            raise ValueError("invalid release projection")
        return self


def footprint_keys(footprint):
    return ({preference_key(f) for f in footprint.preference_fingerprints}
        | {fact_key(t.memory_id, t.version) for t in footprint.fact_targets})


def read_target_releases(raw, footprint):
    if raw is None:
        return ()
    record = RuntimeTargetReleases.model_validate(raw)
    if (record.job_id != footprint.job_id or record.tenant_id != footprint.tenant_id
        or record.proxy_user_id != footprint.proxy_user_id or record.footprint_sha256 != footprint.projection_sha256
        or not {t.key for t in record.targets} <= footprint_keys(footprint)):
        raise ValueError("release binding changed")
    return record.targets
