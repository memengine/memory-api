from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlalchemy import select

from api.db.models import CrossUserConflict
from api.db.models import CrossUserConflictStatus
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import SharedContextEntityType
from api.db.models import SharedContextSignal
from api.services.extractor import ExtractedMemory


SEMANTIC_CONFLICT_THRESHOLD = 0.82

MONTH_NAMES = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

LANGUAGE_NAMES = {
    "arabic",
    "assamese",
    "bengali",
    "english",
    "french",
    "german",
    "gujarati",
    "hindi",
    "italian",
    "japanese",
    "kannada",
    "korean",
    "malayalam",
    "marathi",
    "odia",
    "punjabi",
    "sanskrit",
    "spanish",
    "tamil",
    "telugu",
    "urdu",
}

LOCATION_NAMES = {
    "bangalore",
    "bengaluru",
    "chennai",
    "delhi",
    "gurgaon",
    "gurugram",
    "hyderabad",
    "kolkata",
    "mumbai",
    "noida",
    "pune",
    "san francisco",
    "new york",
    "london",
    "singapore",
}

PROCESS_TERMS = {
    "agile",
    "backfill",
    "backlog",
    "ci/cd",
    "code review",
    "daily standup",
    "deployment",
    "incident response",
    "kanban",
    "onboarding",
    "postmortem",
    "qa",
    "release",
    "retro",
    "runbook",
    "scrum",
    "sprint",
    "standup",
    "testing",
}

PRODUCT_TERMS = {
    "api",
    "app",
    "checkout",
    "dashboard",
    "feature",
    "mobile app",
    "onboarding",
    "payment",
    "product",
    "sdk",
    "search",
    "web app",
}

SHARED_CONTEXT_MARKERS = {
    "all of us",
    "company",
    "everyone",
    "our ",
    "our team",
    "team",
    "the company",
    "the organisation",
    "the organization",
    "the project",
    "we ",
}

GENERIC_GOAL_TOKENS = {
    "build",
    "building",
    "current",
    "currently",
    "get",
    "goal",
    "looking",
    "need",
    "needs",
    "opportunities",
    "opportunity",
    "plan",
    "project",
    "serious",
    "want",
    "wants",
}

TECH_NAMES = {
    "airflow",
    "alembic",
    "android",
    "angular",
    "ansible",
    "apache",
    "aws",
    "azure",
    "bash",
    "bigquery",
    "bootstrap",
    "c",
    "c++",
    "c#",
    "cassandra",
    "celery",
    "chakra",
    "clickhouse",
    "cloudflare",
    "css",
    "dart",
    "django",
    "docker",
    "docker compose",
    "dynamodb",
    "elasticsearch",
    "express",
    "fastapi",
    "figma",
    "firebase",
    "flask",
    "flutter",
    "gcp",
    "git",
    "github",
    "gitlab",
    "go",
    "graphql",
    "grpc",
    "hadoop",
    "html",
    "ios",
    "java",
    "javascript",
    "jenkins",
    "jest",
    "kafka",
    "kotlin",
    "kubernetes",
    "laravel",
    "linux",
    "llamaindex",
    "langchain",
    "mariadb",
    "mongodb",
    "mysql",
    "nestjs",
    "next.js",
    "nextjs",
    "nginx",
    "node",
    "node.js",
    "nuxt",
    "openai",
    "oracle",
    "pandas",
    "pinecone",
    "postgres",
    "postgresql",
    "prisma",
    "pytest",
    "python",
    "pytorch",
    "qdrant",
    "rabbitmq",
    "react",
    "redis",
    "redux",
    "ruby",
    "rust",
    "s3",
    "scala",
    "scikit-learn",
    "selenium",
    "snowflake",
    "spark",
    "spring",
    "sql",
    "sqlalchemy",
    "sqlite",
    "svelte",
    "swift",
    "tailwind",
    "tensorflow",
    "terraform",
    "trpc",
    "typescript",
    "vercel",
    "vue",
    "webpack",
}

STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "he",
    "her",
    "his",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "she",
    "that",
    "the",
    "their",
    "they",
    "this",
    "to",
    "user",
    "was",
    "with",
}

DATE_REGEXES = (
    re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})\b"),
)
NUMBER_REGEX = re.compile(r"(?<![\w.])\d+(?:\.\d+)?%?(?![\w.])")
NAME_REGEX = re.compile(r"\b[A-Z][a-z]{2,}\b")
TOKEN_REGEX = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.\-]*")


class ConflictType(str, Enum):
    PREFERENCE_CHANGE = "preference_change"
    FACT_UPDATE = "fact_update"
    SKILL_PROGRESSION = "skill_progression"
    NEGATION = "negation"
    NUMERIC_UPDATE = "numeric_update"
    TEMPORAL_SHIFT = "temporal_shift"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ConflictCandidate:
    new_memory: ExtractedMemory
    existing_memory: Memory
    detection_strategy: str
    confidence: float
    detected_entities: list[str]


@dataclass(slots=True)
class SharedContextEntity:
    entity_type: SharedContextEntityType
    entity_value: str
    confidence: float = 0.75


@dataclass(slots=True)
class SharedContextConflict:
    new_memory: ExtractedMemory
    conflicting_signal: SharedContextSignal
    conflict_entity: str
    entity_value_a: str
    entity_value_b: str
    description: str


class ConflictDetector:
    def detect_candidates(
        self,
        new_memory: ExtractedMemory,
        existing_memories: list[Memory],
    ) -> list[ConflictCandidate]:
        candidates: dict[str, ConflictCandidate] = {}
        new_entities = extract_entities(new_memory.content)
        new_tokens = topic_tokens(new_memory.content)

        for existing_memory in existing_memories:
            memory_key = str(existing_memory.id)
            existing_entities = extract_entities(existing_memory.content)
            existing_tokens = topic_tokens(existing_memory.content)
            overlap = token_overlap_ratio(new_tokens, existing_tokens)

            score = float(getattr(existing_memory, "_conflict_similarity_score", 0.0) or 0.0)
            if score > SEMANTIC_CONFLICT_THRESHOLD:
                candidates[memory_key] = ConflictCandidate(
                    new_memory=new_memory,
                    existing_memory=existing_memory,
                    detection_strategy="semantic",
                    confidence=min(1.0, score),
                    detected_entities=sorted(new_entities & existing_entities),
                )

            entity_triggers = entity_conflict_triggers(
                new_entities=new_entities,
                existing_entities=existing_entities,
                topic_overlap=overlap,
            )
            if entity_triggers:
                candidates[memory_key] = choose_stronger_candidate(
                    candidates.get(memory_key),
                    ConflictCandidate(
                        new_memory=new_memory,
                        existing_memory=existing_memory,
                        detection_strategy="entity",
                        confidence=max(0.74, min(0.95, 0.65 + overlap)),
                        detected_entities=entity_triggers,
                    ),
                )

            if same_category(new_memory.category, existing_memory.category) and overlap > 0.60:
                candidates[memory_key] = choose_stronger_candidate(
                    candidates.get(memory_key),
                    ConflictCandidate(
                        new_memory=new_memory,
                        existing_memory=existing_memory,
                        detection_strategy="topic_overlap",
                        confidence=min(0.94, 0.60 + overlap),
                        detected_entities=sorted(new_entities & existing_entities),
                    ),
                )

        return sorted(candidates.values(), key=lambda candidate: candidate.confidence, reverse=True)

    def extract_shared_context_entities(self, memory: ExtractedMemory) -> list[SharedContextEntity]:
        return extract_shared_context_entities(memory.content, memory.category)

    def detect_shared_context_conflict(
        self,
        *,
        session,
        new_memory: ExtractedMemory,
        proxy_user_id: str,
        tenant_id: str,
    ) -> list[SharedContextConflict]:
        entities = self.extract_shared_context_entities(new_memory)
        if not entities or not hasattr(session, "execute"):
            return []
        tenant_uuid = uuid.UUID(str(tenant_id))
        proxy_user_uuid = uuid.UUID(str(proxy_user_id))

        entity_types = sorted({entity.entity_type for entity in entities}, key=lambda item: item.value)
        stmt = (
            select(SharedContextSignal)
            .where(
                SharedContextSignal.tenant_id == tenant_uuid,
                SharedContextSignal.entity_type.in_(entity_types),
                SharedContextSignal.is_superseded.is_(False),
                SharedContextSignal.source_proxy_user_id != proxy_user_uuid,
            )
        )
        result = session.execute(stmt)
        signals = result.scalars().all() if hasattr(result, "scalars") else []
        conflicts: list[SharedContextConflict] = []

        for entity in entities:
            for signal in signals:
                if signal.entity_type != entity.entity_type:
                    continue
                if signal.entity_value.lower() == entity.entity_value.lower():
                    continue
                conflicts.append(
                    SharedContextConflict(
                        new_memory=new_memory,
                        conflicting_signal=signal,
                        conflict_entity=entity.entity_type.value,
                        entity_value_a=signal.entity_value,
                        entity_value_b=entity.entity_value,
                        description=(
                            f"Another user says {signal.entity_value}; "
                            f"this user says {entity.entity_value}"
                        ),
                    )
                )
        return conflicts


def classify_conflict_type(
    new_content: str,
    existing_content: str,
    category: str,
    detected_entities: list[str],
) -> ConflictType:
    new_lower = new_content.lower()
    detected = {entity.lower() for entity in detected_entities}
    new_entities = extract_entities(new_content)
    existing_entities = extract_entities(existing_content)

    if str(category).lower() == "preference":
        return ConflictType.PREFERENCE_CHANGE
    if any(phrase in new_lower for phrase in ("no longer", "stopped", "quit", "moved away from")):
        return ConflictType.NEGATION
    if any(phrase in new_lower for phrase in ("learned", "mastered", "now know", "now knows")):
        return ConflictType.SKILL_PROGRESSION
    if has_numeric_entity(new_entities) and has_numeric_entity(existing_entities):
        return ConflictType.NUMERIC_UPDATE
    if any(entity in MONTH_NAMES for entity in detected) or (
        has_date_entity(new_entities) and has_date_entity(existing_entities)
    ):
        return ConflictType.FACT_UPDATE
    if any(phrase in new_lower for phrase in ("last week", "yesterday", "now", "recently")):
        return ConflictType.TEMPORAL_SHIFT
    return ConflictType.UNKNOWN


def extract_entities(content: str) -> set[str]:
    entities: set[str] = set()
    lower = content.lower()

    for month in MONTH_NAMES:
        if re.search(rf"\b{re.escape(month)}\b", lower):
            entities.add(month)

    for regex in DATE_REGEXES:
        entities.update(match.group(0).lower() for match in regex.finditer(content))

    for number in NUMBER_REGEX.findall(content):
        if "%" in number or len(number) <= 4:
            entities.add(number.lower())

    for phrase in TECH_NAMES:
        if re.search(rf"\b{re.escape(phrase)}\b", lower):
            entities.add(phrase)

    for language in LANGUAGE_NAMES:
        if re.search(rf"\b{re.escape(language)}\b", lower):
            entities.add(language)

    for location in LOCATION_NAMES:
        if re.search(rf"\b{re.escape(location)}\b", lower):
            entities.add(location)

    for name in NAME_REGEX.findall(content):
        if name.lower() not in STOPWORDS and name.lower() not in {"user", "memory"}:
            entities.add(name.lower())

    return entities


def extract_shared_context_entities(content: str, category: str) -> list[SharedContextEntity]:
    lower = content.lower()
    entities: dict[tuple[SharedContextEntityType, str], SharedContextEntity] = {}

    def add(entity_type: SharedContextEntityType, entity_value: str, confidence: float = 0.75) -> None:
        normalized = entity_value.lower().strip()
        if not normalized:
            return
        entities[(entity_type, normalized)] = SharedContextEntity(
            entity_type=entity_type,
            entity_value=normalized,
            confidence=confidence,
        )

    for tech in TECH_NAMES:
        if re.search(rf"\b{re.escape(tech)}\b", lower):
            add(SharedContextEntityType.tech_stack, tech, 0.85)

    for process_term in PROCESS_TERMS:
        if re.search(rf"\b{re.escape(process_term)}\b", lower):
            add(SharedContextEntityType.team_process, process_term, 0.75)

    for product_term in PRODUCT_TERMS:
        if re.search(rf"\b{re.escape(product_term)}\b", lower):
            add(SharedContextEntityType.product_feature, product_term, 0.65)

    if str(category).lower() == "goal" and has_shared_context_marker(lower):
        shared_goal = shared_goal_entity_value(content)
        if shared_goal:
            add(SharedContextEntityType.shared_goal, shared_goal, 0.65)

    company_markers = ("company", "team", "product", "codebase", "stack")
    if any(marker in lower for marker in company_markers):
        for entity in extract_entities(content):
            if entity not in TECH_NAMES:
                add(SharedContextEntityType.company_fact, entity, 0.60)

    return list(entities.values())


def has_shared_context_marker(lower_content: str) -> bool:
    return any(marker in lower_content for marker in SHARED_CONTEXT_MARKERS)


def shared_goal_entity_value(content: str) -> str | None:
    tokens = [
        token
        for token in topic_tokens(content)
        if token not in TECH_NAMES and token not in GENERIC_GOAL_TOKENS
    ]
    if not tokens:
        return None
    return " ".join(sorted(tokens)[:5])


def build_cross_user_conflict_row(
    *,
    tenant_id: str,
    user_a_memory_id: str | None,
    user_b_memory_id: str | None,
    entity_type: SharedContextEntityType,
    entity_value_a: str,
    entity_value_b: str,
) -> CrossUserConflict:
    return CrossUserConflict(
        tenant_id=uuid.UUID(str(tenant_id)),
        user_a_memory_id=uuid.UUID(str(user_a_memory_id)) if user_a_memory_id else None,
        user_b_memory_id=uuid.UUID(str(user_b_memory_id)) if user_b_memory_id else None,
        entity_type=entity_type,
        entity_value_a=entity_value_a,
        entity_value_b=entity_value_b,
        status=CrossUserConflictStatus.pending,
    )


def topic_tokens(content: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_REGEX.findall(content)
        if len(token) > 2 and token.lower() not in STOPWORDS
    }


def token_overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def entity_conflict_triggers(
    *,
    new_entities: set[str],
    existing_entities: set[str],
    topic_overlap: float,
) -> list[str]:
    common = new_entities & existing_entities
    if common:
        return sorted(common)

    new_dates = {entity for entity in new_entities if is_date_entity(entity)}
    existing_dates = {entity for entity in existing_entities if is_date_entity(entity)}
    if new_dates and existing_dates and topic_overlap >= 0.30:
        return sorted(new_dates | existing_dates)

    new_numbers = {entity for entity in new_entities if is_numeric_entity(entity)}
    existing_numbers = {entity for entity in existing_entities if is_numeric_entity(entity)}
    if new_numbers and existing_numbers and topic_overlap >= 0.30:
        return sorted(new_numbers | existing_numbers)

    return []


def has_date_entity(entities: Iterable[str]) -> bool:
    return any(is_date_entity(entity) for entity in entities)


def is_date_entity(entity: str) -> bool:
    return (
        entity.lower() in MONTH_NAMES
        or any(regex.fullmatch(entity) for regex in DATE_REGEXES)
    )


def has_numeric_entity(entities: Iterable[str]) -> bool:
    return any(is_numeric_entity(entity) for entity in entities)


def is_numeric_entity(entity: str) -> bool:
    return bool(NUMBER_REGEX.fullmatch(entity))


def same_category(left: str, right: str | MemoryCategory) -> bool:
    right_value = right.value if isinstance(right, MemoryCategory) else str(right)
    return str(left).lower() == right_value.lower()


def choose_stronger_candidate(
    current: ConflictCandidate | None,
    new_candidate: ConflictCandidate,
) -> ConflictCandidate:
    if current is None:
        return new_candidate
    if current.detection_strategy == "entity" and any(
        is_date_entity(entity) or is_numeric_entity(entity)
        for entity in current.detected_entities
    ):
        return current
    if new_candidate.detection_strategy == "entity" and any(
        is_date_entity(entity) or is_numeric_entity(entity)
        for entity in new_candidate.detected_entities
    ):
        return new_candidate
    strategy_priority = {
        "semantic": 3,
        "topic_overlap": 2,
        "entity": 1,
    }
    current_priority = strategy_priority.get(current.detection_strategy, 0)
    new_priority = strategy_priority.get(new_candidate.detection_strategy, 0)
    if new_priority > current_priority:
        return new_candidate
    if new_priority < current_priority:
        return current
    if new_candidate.confidence > current.confidence:
        return new_candidate
    return current


__all__ = [
    "ConflictCandidate",
    "ConflictDetector",
    "ConflictType",
    "SEMANTIC_CONFLICT_THRESHOLD",
    "SharedContextConflict",
    "SharedContextEntity",
    "build_cross_user_conflict_row",
    "classify_conflict_type",
    "extract_entities",
    "extract_shared_context_entities",
]
