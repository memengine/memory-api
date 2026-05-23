from __future__ import annotations

from api.db.models import Memory
from api.services.conflict_routing.base import BaseEntityRouter
from api.services.conflict_routing.base import ResolutionPath


class HealthcareEntityRouter(BaseEntityRouter):
    USER_SESSION_ENTITIES = {
        "condition",
        "medication",
        "allergy",
        "symptom",
        "personal_health_goal",
        "diet_restriction",
        "exercise_preference",
        "mental_health_note",
        "personal_medical_history",
    }

    TENANT_REVIEW_ENTITIES = {
        "clinic_protocol",
        "standard_dosage",
        "facility_policy",
        "shared_care_plan",
        "department_process",
    }

    def get_domain(self) -> str:
        return "healthcare"

    def classify(
        self,
        entity_type: str,
        memory_a: Memory,
        memory_b: Memory,
    ) -> ResolutionPath | None:
        del memory_a, memory_b
        if entity_type in self.USER_SESSION_ENTITIES:
            return "user_session"
        if entity_type in self.TENANT_REVIEW_ENTITIES:
            return "tenant_review"
        return None
