"""Internal operator record; never a user confirmation or an applied receipt."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from api.services.preference_choice_service import Version


class RuntimeReconciliationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    outcome: Literal["quarantined"] = "quarantined"
    hold_scope: Literal["user_learned_context", "affected_context"]
    footprint_sha256: Version | None
    reason_code: Literal["source_expired", "source_redacted", "record_invalid", "source_binding_changed"]
    operator_reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
    actor_credential_class: Literal["admin_secret"] = "admin_secret"
    request_id: str = Field(max_length=128)

    @model_validator(mode="after")
    def consistent_hold(self):
        if ((self.hold_scope == "affected_context") != (self.footprint_sha256 is not None)
            or (self.reason_code in {"record_invalid", "source_binding_changed"}
                and self.hold_scope != "user_learned_context")):
            raise ValueError("reconciliation cannot narrow unknown evidence")
        return self
