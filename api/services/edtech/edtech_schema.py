from __future__ import annotations

from typing import Any


LearnerField = dict[str, Any]

ALLOWED_LEARNER_TYPES = (
    "school_student",
    "competitive_exam",
    "higher_education",
    "professional_cert",
    "skill_learner",
    "medical_student",
)

BASE_FIELDS: dict[str, LearnerField] = {
    "learner_type": {
        "category": "fact",
        "content_template": "Learner type: {value}",
        "allowed_values": list(ALLOWED_LEARNER_TYPES),
        "importance_base": 10.0,
        "description": "What kind of learner this person is.",
    },
    "primary_goal": {
        "category": "goal",
        "content_template": "Goal: {value}",
        "importance_base": 9.5,
        "description": "What this learner is ultimately trying to achieve.",
    },
    "weak_areas": {
        "category": "expertise",
        "content_template": "Weak in {topic} - {severity}",
        "metadata_keys": ["topic", "severity", "specific_gap", "attempts", "subject"],
        "importance_base": 9.5,
    },
    "strong_areas": {
        "category": "expertise",
        "content_template": "Strong in {topic}",
        "metadata_keys": ["topic", "confidence", "subject"],
        "importance_base": 7.0,
    },
    "deadline": {
        "category": "goal",
        "content_template": "Deadline: {event} on {date}",
        "metadata_keys": ["event", "date", "days_remaining", "urgency"],
        "importance_base": 9.5,
        "description": "Any time-bound target.",
    },
    "explanation_style": {
        "category": "preference",
        "content_template": "Learns via {primary}",
        "metadata_keys": ["primary", "avoid", "needs_step_by_step", "anxiety_trigger"],
        "importance_base": 9.0,
    },
    "language_profile": {
        "category": "preference",
        "content_template": "Language: {primary}, comfort: {comfort}",
        "metadata_keys": ["primary", "comfort", "technical_terms"],
        "importance_base": 9.0,
    },
    "progress_trend": {
        "category": "fact",
        "content_template": "Progress: {trend} - {note}",
        "metadata_keys": ["trend", "note", "subject"],
        "allowed_values": ["improving", "stable", "declining", "unknown"],
        "importance_base": 7.5,
    },
    "forgetting_stage": {
        "category": "fact",
        "content_template": "Topic {topic} stage: {stage}",
        "metadata_keys": ["topic", "stage", "last_reviewed", "days_since"],
        "importance_base": 8.5,
    },
}

SCHOOL_STUDENT_FIELDS: dict[str, LearnerField] = {
    "grade_and_board": {
        "category": "fact",
        "content_template": "Class {grade} {board}",
        "metadata_keys": ["grade", "board", "stream"],
        "importance_base": 9.0,
    },
    "exam_target": {
        "category": "goal",
        "content_template": "Target: {exam_name} | Score: {target_score}",
        "metadata_keys": ["exam_name", "target_score", "minimum_passing"],
        "importance_base": 9.0,
    },
    "mock_score": {
        "category": "fact",
        "content_template": "Mock {subject}: {score}/{max}",
        "metadata_keys": ["subject", "score", "max", "date", "test_name"],
        "importance_base": 7.5,
    },
}

COMPETITIVE_EXAM_FIELDS: dict[str, LearnerField] = {
    "exam_details": {
        "category": "goal",
        "content_template": "Preparing for {exam_name} - attempt {attempt_number}",
        "metadata_keys": ["exam_name", "attempt_number", "tier", "post_preference"],
        "importance_base": 9.5,
        "examples": "SSC CGL Tier 1, UPSC Prelims, GATE CS",
    },
    "cut_off_target": {
        "category": "goal",
        "content_template": "Target cut-off: {score} for {category}",
        "metadata_keys": ["score", "category", "previous_score", "gap"],
        "importance_base": 9.0,
    },
    "subject_strategy": {
        "category": "procedure",
        "content_template": "Strategy: high time on {high_time}, skip {skip}",
        "metadata_keys": ["high_time", "skip", "exam_specific_tip"],
        "importance_base": 8.0,
    },
    "current_resource": {
        "category": "procedure",
        "content_template": "Using {resource} for {subject}",
        "metadata_keys": ["resource", "subject", "stage"],
        "importance_base": 6.5,
    },
}

HIGHER_EDUCATION_FIELDS: dict[str, LearnerField] = {
    "academic_context": {
        "category": "fact",
        "content_template": "{degree} {branch} - Semester {semester}",
        "metadata_keys": ["degree", "branch", "semester", "college", "year"],
        "importance_base": 9.0,
        "examples": "B.Tech CSE Semester 4, MBBS 2nd year",
    },
    "backlog_subjects": {
        "category": "expertise",
        "content_template": "Backlog in {subject} - {attempts} attempts",
        "metadata_keys": ["subject", "attempts", "last_score", "next_attempt"],
        "importance_base": 9.0,
    },
    "project_work": {
        "category": "goal",
        "content_template": "Working on {project} using {tech_stack}",
        "metadata_keys": ["project", "tech_stack", "deadline", "stage"],
        "importance_base": 7.5,
    },
    "placement_target": {
        "category": "goal",
        "content_template": "Placement target: {target_role} at {target_company_type}",
        "metadata_keys": ["target_role", "target_company_type", "cgpa", "placement_season"],
        "importance_base": 8.5,
    },
}

PROFESSIONAL_CERT_FIELDS: dict[str, LearnerField] = {
    "certification_context": {
        "category": "fact",
        "content_template": "{cert_name} - attempt {attempt_number}",
        "metadata_keys": ["cert_name", "cert_body", "attempt_number", "level", "passing_marks"],
        "importance_base": 9.5,
        "examples": "CA Foundation, CFA Level 1, AWS SAA",
    },
    "paper_strategy": {
        "category": "procedure",
        "content_template": "Paper {paper}: focus on {focus_areas}",
        "metadata_keys": ["paper", "focus_areas", "skip_areas", "time_allocation"],
        "importance_base": 8.0,
    },
    "study_group": {
        "category": "relationship",
        "content_template": "Studies with {group_type} for {cert_name}",
        "metadata_keys": ["group_type", "cert_name", "meeting_frequency"],
        "importance_base": 5.5,
    },
}

SKILL_LEARNER_FIELDS: dict[str, LearnerField] = {
    "current_skill_level": {
        "category": "expertise",
        "content_template": "Level: {skill} - {level}",
        "metadata_keys": ["skill", "level", "time_learning_months"],
        "importance_base": 8.5,
        "allowed_levels": ["complete_beginner", "beginner", "intermediate", "advanced"],
    },
    "current_project": {
        "category": "goal",
        "content_template": "Building: {project} with {tech_stack}",
        "metadata_keys": ["project", "tech_stack", "stuck_on", "completion_pct"],
        "importance_base": 9.0,
    },
    "learning_path_stage": {
        "category": "procedure",
        "content_template": "Learning path: {path} - at {current_stage}",
        "metadata_keys": ["path", "current_stage", "next_milestone", "pace"],
        "importance_base": 8.0,
    },
    "error_patterns": {
        "category": "expertise",
        "content_template": "Recurring error: {error_type} in {context}",
        "metadata_keys": ["error_type", "context", "frequency", "resolved"],
        "importance_base": 8.5,
    },
    "job_target": {
        "category": "goal",
        "content_template": "Target: {role} at {company_type}",
        "metadata_keys": ["role", "company_type", "timeline", "current_gap"],
        "importance_base": 8.5,
    },
}

MEDICAL_STUDENT_FIELDS: dict[str, LearnerField] = {
    "medical_context": {
        "category": "fact",
        "content_template": "{degree} {year} - {specialty_focus}",
        "metadata_keys": ["degree", "year", "specialty_focus", "rotation"],
        "importance_base": 9.0,
        "examples": "MBBS 3rd year, MD Cardiology, NEET-PG prep",
    },
    "pg_entrance_target": {
        "category": "goal",
        "content_template": "PG target: {exam} - rank {target_rank}",
        "metadata_keys": ["exam", "target_rank", "target_specialty", "attempt_number"],
        "importance_base": 9.5,
        "examples": "NEET-PG, USMLE Step 1, PLAB",
    },
    "high_yield_subjects": {
        "category": "expertise",
        "content_template": "High yield for {exam}: {subject}",
        "metadata_keys": ["exam", "subject", "completion_pct", "revision_count"],
        "importance_base": 8.5,
    },
    "clinical_context": {
        "category": "procedure",
        "content_template": "Clinical: {rotation} - {key_learning}",
        "metadata_keys": ["rotation", "key_learning", "duration_weeks"],
        "importance_base": 7.0,
    },
}

LEARNER_TYPE_TO_EXTENSIONS: dict[str, dict[str, LearnerField]] = {
    "school_student": SCHOOL_STUDENT_FIELDS,
    "competitive_exam": COMPETITIVE_EXAM_FIELDS,
    "higher_education": HIGHER_EDUCATION_FIELDS,
    "professional_cert": PROFESSIONAL_CERT_FIELDS,
    "skill_learner": SKILL_LEARNER_FIELDS,
    "medical_student": MEDICAL_STUDENT_FIELDS,
}

NEVER_STORE = (
    "Specific patient details.",
    "Patient names.",
    "Patient case numbers.",
    "Diagnosis or treatment details of a specific patient.",
)


def active_fields_for(learner_type: str | None) -> dict[str, LearnerField]:
    normalized = learner_type if learner_type in LEARNER_TYPE_TO_EXTENSIONS else "school_student"
    return {**BASE_FIELDS, **LEARNER_TYPE_TO_EXTENSIONS[normalized]}


__all__ = [
    "ALLOWED_LEARNER_TYPES",
    "BASE_FIELDS",
    "COMPETITIVE_EXAM_FIELDS",
    "HIGHER_EDUCATION_FIELDS",
    "LEARNER_TYPE_TO_EXTENSIONS",
    "MEDICAL_STUDENT_FIELDS",
    "NEVER_STORE",
    "PROFESSIONAL_CERT_FIELDS",
    "SCHOOL_STUDENT_FIELDS",
    "SKILL_LEARNER_FIELDS",
    "active_fields_for",
]
