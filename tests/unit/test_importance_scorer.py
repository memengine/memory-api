from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.extractor import ExtractedMemory
from api.services.importance_scorer import ImportanceScorer
from api.tasks.decay_tasks import DECAY_TASK_BEAT_SCHEDULE
from api.tasks.decay_tasks import run_decay_cycle


class FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class FakeSession:
    def __init__(self, memories):
        self.memories = memories
        self.added = []
        self.commits = 0
        self.closed = False

    def execute(self, _statement):
        return FakeScalarResult(self.memories)

    def add(self, item):
        self.added.append(item)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def make_extracted_memory(
    *,
    category: str = "goal",
    importance_score: float = 6.0,
) -> ExtractedMemory:
    return ExtractedMemory(
        content="User wants to launch the API next month",
        category=category,
        importance_score=importance_score,
        confidence=0.9,
        expiry="temporary",
        reasoning="Priority work",
    )


def make_memory(
    *,
    importance_score: float = 2.5,
    access_count: int = 0,
    last_accessed_at: datetime | None = None,
    is_archived: bool = False,
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="Stored memory",
        category=MemoryCategory.goal,
        importance_score=importance_score,
        confidence_score=0.8,
        embedding_id="memory-embedding-id",
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        access_count=access_count,
        last_accessed_at=last_accessed_at or datetime.now(UTC),
        is_archived=is_archived,
    )


def test_score_combines_llm_category_and_access_signals() -> None:
    scorer = ImportanceScorer()

    score = scorer.score(
        make_extracted_memory(category="goal", importance_score=6.0),
        {"similar_access_count": 50},
    )

    assert score == 7.75


def test_score_caps_at_ten() -> None:
    scorer = ImportanceScorer()

    score = scorer.score(
        make_extracted_memory(category="goal", importance_score=9.8),
        {"similar_access_count": 1000},
    )

    assert score == 10.0


def test_goal_scores_higher_than_fact_for_equivalent_content() -> None:
    scorer = ImportanceScorer()
    goal_memory = make_extracted_memory(category="goal", importance_score=6.0)
    fact_memory = make_extracted_memory(category="fact", importance_score=6.0)

    goal_score = scorer.score(goal_memory, {})
    fact_score = scorer.score(fact_memory, {})

    assert goal_score > fact_score


def test_record_access_updates_count_timestamp_and_score() -> None:
    scorer = ImportanceScorer()
    memory = make_memory(importance_score=5.0, access_count=20)
    before_accessed_at = memory.last_accessed_at

    new_score = scorer.record_access(memory)

    assert memory.access_count == 21
    assert memory.last_accessed_at >= before_accessed_at
    assert new_score > 5.0


def test_access_boost_is_capped_at_half_point() -> None:
    scorer = ImportanceScorer()

    boost = scorer.calculate_access_pattern_boost({"similar_access_count": 500})

    assert boost == 0.5


def test_increment_access_caps_total_importance_boost_at_half_point_after_100_calls() -> None:
    scorer = ImportanceScorer()
    memory = make_memory(importance_score=5.0, access_count=0)
    starting_score = memory.importance_score

    for _ in range(100):
        scorer.increment_access(memory)

    assert memory.access_count == 100
    assert memory.importance_score == starting_score + 0.5


def test_decay_cycle_archives_memory_accessed_40_days_ago_with_importance_two() -> None:
    now = datetime.now(UTC)
    stale_memory = make_memory(
        importance_score=2.0,
        last_accessed_at=now - timedelta(days=40),
    )
    session = FakeSession([stale_memory])

    archived_count = run_decay_cycle(session_factory=lambda: session, now=now)

    assert archived_count == 1
    assert stale_memory.is_archived is True


def test_decay_cycle_archives_only_stale_low_importance_memories() -> None:
    now = datetime.now(UTC)
    stale_low_importance = make_memory(
        importance_score=2.4,
        last_accessed_at=now - timedelta(days=31),
    )
    recent_low_importance = make_memory(
        importance_score=2.1,
        last_accessed_at=now - timedelta(days=10),
    )
    stale_high_importance = make_memory(
        importance_score=4.5,
        last_accessed_at=now - timedelta(days=45),
    )
    session = FakeSession(
        [stale_low_importance, recent_low_importance, stale_high_importance]
    )

    archived_count = run_decay_cycle(session_factory=lambda: session, now=now)

    assert archived_count == 1
    assert stale_low_importance.is_archived is True
    assert recent_low_importance.is_archived is False
    assert stale_high_importance.is_archived is False
    assert session.commits == 1
    assert session.closed is True
    assert any(
        isinstance(item, AuditLog)
        and item.action == AuditAction.archived
        and item.memory_id == stale_low_importance.id
        for item in session.added
    )


def test_decay_schedule_runs_daily_at_two_am() -> None:
    schedule = DECAY_TASK_BEAT_SCHEDULE["archive-stale-low-importance-memories"]

    assert schedule["task"] == "api.tasks.decay_tasks.archive_stale_low_importance_memories"
    assert str(schedule["schedule"]) == "<crontab: 0 2 * * * (m/h/dM/MY/d)>"
