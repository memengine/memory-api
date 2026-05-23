from __future__ import annotations

from api.db.models import Memory
from api.services.conflict_routing.base import BaseEntityRouter
from api.services.conflict_routing.base import ResolutionPath


class EdTechEntityRouter(BaseEntityRouter):
    USER_SESSION_ENTITIES = {
        "exam_date",
        "grade_level",
        "personal_skill",
        "personal_preference",
        "individual_goal",
        "learning_style",
        "personal_fact",
        "marks_target",
        "study_schedule",
        "weak_topic",
        "strong_topic",
        "forgetting_stage",
        "explanation_style",
        "language_profile",
        "session_profile",
        "mock_score",
    }

    TENANT_REVIEW_ENTITIES = {
        "institution_name",
        "curriculum_standard",
        "batch_assignment",
        "cohort_goal",
        "institution_policy",
        "shared_syllabus",
    }

    def get_domain(self) -> str:
        return "edtech"

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
