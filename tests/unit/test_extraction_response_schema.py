from __future__ import annotations

import pytest

from api.services.extraction_response_schema import build_extraction_response_schema


@pytest.mark.parametrize("proposal_enabled", [False, True])
def test_strict_schema_requires_complete_root_shape(
    proposal_enabled: bool,
) -> None:
    schema = build_extraction_response_schema(
        proposal_confirmation_enabled=proposal_enabled,
    )

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert ("proposal_confirmation" in schema["properties"]) is proposal_enabled
    assert "claim_semantics_shadow" not in schema["properties"]
    assert "claim_semantics_shadow" not in schema["required"]
    claim_state = schema["properties"]["memories"]["items"]["properties"][
        "claim_state"
    ]
    assert claim_state["enum"] == ["asserted", "correction", "uncertain_change"]
    memory_schema = schema["properties"]["memories"]["items"]
    assert "evidence_spans" in memory_schema["required"]
    span = memory_schema["properties"]["evidence_spans"]["items"]
    assert set(span["required"]) == {"turn_index", "quote"}
    assert span["additionalProperties"] is False


def test_proposal_mode_does_not_allow_model_to_emit_confirmed_memory_directly() -> None:
    schema = build_extraction_response_schema(
        proposal_confirmation_enabled=True,
    )

    relation = schema["properties"]["memories"]["items"]["properties"][
        "evidence_relation"
    ]
    assert relation["enum"] == ["direct_user_statement"]
