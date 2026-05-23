from __future__ import annotations

from api.db.models import Memory
from api.services.conflict_routing.base import BaseEntityRouter
from api.services.conflict_routing.base import ResolutionPath


class HRTechEntityRouter(BaseEntityRouter):
    USER_SESSION_ENTITIES = {
        "salary_expectation",
        "career_goal",
        "skill_level",
        "job_preference",
        "interview_feedback",
        "personal_strength",
        "work_style",
        "relocation_preference",
    }

    TENANT_REVIEW_ENTITIES = {
        "role_requirement",
        "team_headcount",
        "hiring_policy",
        "compensation_band",
        "org_structure",
        "team_process",
    }

    def get_domain(self) -> str:
        return "hrtech"

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
