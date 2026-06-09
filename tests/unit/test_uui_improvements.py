from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from api.routers.uui import _mask_uui_token
from api.routers.uui import _normalize_categories
from api.routers.uui import _stored_ago
from api.schemas.uui_schemas import ClarificationAnswerRequest
from api.schemas.uui_schemas import PartialGrantUpdate
from api.schemas.uui_schemas import TokenRegenerateData
from api.schemas.uui_schemas import UserMemoryCorrectRequest
from api.schemas.uui_schemas import UserMemoryFlagRequest
from api.schemas.uui_schemas import UserMemoryView


def test_mask_uui_token_never_exposes_full_secret() -> None:
    token = "uui_" + ("a" * 48)

    masked = _mask_uui_token(token)

    assert masked == "uui_aaaa...aaaa"
    assert token not in masked


def test_normalize_categories_filters_unknown_values() -> None:
    categories = _normalize_categories("expertise,unknown,preference,,fact")

    assert categories == ["expertise", "preference", "fact"]


def test_stored_ago_formats_recent_values() -> None:
    assert _stored_ago(datetime.now(UTC)) == "today"
    assert _stored_ago(datetime.now(UTC) - timedelta(days=2, hours=1)) == "2 days ago"


def test_partial_grant_update_requires_at_least_one_category() -> None:
    payload = PartialGrantUpdate(categories_allowed=["expertise"])

    assert payload.categories_allowed == ["expertise"]


def test_clarification_answer_schema_accepts_expected_answers() -> None:
    assert ClarificationAnswerRequest(answer="A").answer == "A"
    assert ClarificationAnswerRequest(answer="B").answer == "B"
    assert ClarificationAnswerRequest(answer="neither").answer == "neither"


def test_token_regenerate_data_contains_show_once_token() -> None:
    user_token = "uui_" + ("b" * 48)
    data = TokenRegenerateData(
        uui_token=user_token,
        masked_uui_token="uui_bbbb...bbbb",
        regenerated_at=datetime.now(UTC),
    )

    assert data.uui_token == user_token
    assert data.masked_uui_token.endswith("bbbb")


def test_user_memory_flag_schema_accepts_review_reasons() -> None:
    payload = UserMemoryFlagRequest(reason="outdated", correction="I am now in Class 11.")

    assert payload.reason == "outdated"
    assert payload.correction == "I am now in Class 11."


def test_user_memory_correction_requires_useful_content() -> None:
    payload = UserMemoryCorrectRequest(corrected_content="User is now in Class 11.")

    assert payload.corrected_content == "User is now in Class 11."


def test_user_memory_view_exposes_self_service_fields() -> None:
    view = UserMemoryView(
        id="00000000-0000-0000-0000-000000000001",
        content="User prefers concise explanations.",
        category="preference",
        importance_score=7.5,
        importance_trend="rising",
        is_hot=True,
        stored_days_ago=3,
        last_accessed_days_ago=1,
        source_agent_name="Tutor",
        is_flagged=True,
    )

    assert view.is_hot is True
    assert view.is_flagged is True
    assert view.source_agent_name == "Tutor"
