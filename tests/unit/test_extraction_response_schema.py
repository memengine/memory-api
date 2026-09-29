from __future__ import annotations

import pytest

from api.services.extraction_response_schema import build_extraction_response_schema


@pytest.mark.parametrize("proposal_enabled", [False, True])
def test_strict_schema_requires_complete_root_and_shadow_shape(
    proposal_enabled: bool,
) -> None:
    schema = build_extraction_response_schema(
        proposal_confirmation_enabled=proposal_enabled,
        claim_semantics_shadow_enabled=True,
    )

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert ("proposal_confirmation" in schema["properties"]) is proposal_enabled
    shadow_item = schema["properties"]["claim_semantics_shadow"]["items"]
    assert shadow_item["additionalProperties"] is False
    assert set(shadow_item["required"]) == set(shadow_item["properties"])
    assert shadow_item["properties"]["speech_act"]["enum"] == [
        "assertion",
        "correction",
        "retraction",
        "uncertain_change",
        "reaffirmation",
    ]


def test_schema_omits_shadow_when_feature_is_disabled() -> None:
    schema = build_extraction_response_schema(
        proposal_confirmation_enabled=False,
        claim_semantics_shadow_enabled=False,
    )

    assert "claim_semantics_shadow" not in schema["properties"]
    assert "claim_semantics_shadow" not in schema["required"]


def test_proposal_mode_does_not_allow_model_to_emit_confirmed_memory_directly() -> None:
    schema = build_extraction_response_schema(
        proposal_confirmation_enabled=True,
        claim_semantics_shadow_enabled=True,
    )

    relation = schema["properties"]["memories"]["items"]["properties"][
        "evidence_relation"
    ]
    assert relation["enum"] == ["direct_user_statement"]
