from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from api.services.edtech.edtech_schema import ALLOWED_LEARNER_TYPES


@dataclass(slots=True)
class LearnerTypeDetection:
    learner_type: str
    confidence: str
    scores: dict[str, int]
    matched_signals: dict[str, list[str]]


class LearnerTypeDetector:
    DETECTION_SIGNALS: dict[str, tuple[str, ...]] = {
        "school_student": (
            "class",
            "cbse",
            "icse",
            "board exam",
            "jee",
            "neet",
            "grade",
            "school",
            "12th",
            "10th",
            "board",
            "state board",
            "ncert",
        ),
        "competitive_exam": (
            "ssc",
            "upsc",
            "cgl",
            "chsl",
            "ias",
            "ips",
            "gate",
            "cat",
            "gmat",
            "gre",
            "banking",
            "ibps",
            "prelims",
            "mains",
            "cut off",
            "attempt",
            "tier",
        ),
        "higher_education": (
            "btech",
            "betech",
            "b tech",
            "b.tech",
            "engineering",
            "semester",
            "sem",
            "college",
            "university",
            "degree",
            "cpi",
            "cgpa",
            "sgpa",
            "backlog",
            "placement",
            "internship",
            "campus",
            "iit",
            "iiit",
            "nit",
            "year student",
            "1st year",
            "2nd year",
            "3rd year",
            "4th year",
            "first year",
            "second year",
            "third year",
            "fourth year",
            "ece",
            "cse",
        ),
        "professional_cert": (
            "ca",
            "cfa",
            "cpa",
            "aws",
            "azure",
            "certification",
            "chartered",
            "icai",
            "foundation",
            "intermediate",
            "final",
            "paper",
            "attempt number",
        ),
        "skill_learner": (
            "learning python",
            "learning javascript",
            "dsa",
            "data structures",
            "leetcode",
            "coding",
            "programming",
            "web dev",
            "full stack",
            "machine learning",
            "portfolio",
            "project",
            "github",
        ),
        "medical_student": (
            "mbbs",
            "md",
            "neet pg",
            "neet-pg",
            "usmle",
            "plab",
            "medicine",
            "surgery",
            "clinical",
            "hospital",
            "rotation",
            "ward",
            "anatomy",
            "physiology",
            "pathology",
            "doctor",
            "physician",
        ),
    }

    # If two learner types score equally, prefer the identity-context signal over
    # incidental task signals. Example: a B.Tech student with a GitHub project is
    # still higher_education, not automatically a skill_learner.
    TIE_BREAK_PRIORITY = (
        "medical_student",
        "higher_education",
        "competitive_exam",
        "professional_cert",
        "school_student",
        "skill_learner",
    )

    def detect(
        self,
        messages: list[dict[str, Any]],
        existing_learner_type: str | None = None,
    ) -> str:
        return self.detect_result(messages, existing_learner_type=existing_learner_type).learner_type

    def detect_result(
        self,
        messages: list[dict[str, Any]],
        existing_learner_type: str | None = None,
    ) -> LearnerTypeDetection:
        if existing_learner_type in ALLOWED_LEARNER_TYPES:
            return LearnerTypeDetection(
                learner_type=existing_learner_type,
                confidence="high",
                scores={existing_learner_type: 999},
                matched_signals={existing_learner_type: ["existing_profile"]},
            )

        matched_signals = self.explain_detection(messages)
        scores = {learner_type: len(signals) for learner_type, signals in matched_signals.items()}
        best_score = max(scores.values(), default=0)
        if best_score <= 0:
            return LearnerTypeDetection(
                learner_type="school_student",
                confidence="low",
                scores=scores,
                matched_signals=matched_signals,
            )

        candidates = {learner_type for learner_type, score in scores.items() if score == best_score}
        learner_type = next(
            item for item in self.TIE_BREAK_PRIORITY if item in candidates
        )
        return LearnerTypeDetection(
            learner_type=learner_type,
            confidence="high" if best_score >= 2 else "low",
            scores=scores,
            matched_signals=matched_signals,
        )

    def explain_detection(self, messages: list[dict[str, Any]]) -> dict[str, list[str]]:
        content = " ".join(str(message.get("content") or "").lower() for message in messages)
        return {
            learner_type: [signal for signal in signals if _signal_matches(content, signal)]
            for learner_type, signals in self.DETECTION_SIGNALS.items()
        }


def _signal_matches(content: str, signal: str) -> bool:
    escaped = re.escape(signal.lower()).replace(r"\ ", r"\s+")
    prefix = r"(?<![a-z0-9])" if signal[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if signal[-1].isalnum() else ""
    return re.search(f"{prefix}{escaped}{suffix}", content) is not None


__all__ = ["LearnerTypeDetection", "LearnerTypeDetector"]
