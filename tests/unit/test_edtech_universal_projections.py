from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.services.edtech.projections import build_edtech_universal_projections


def test_edtech_projection_exports_only_portable_summaries() -> None:
    memory = SimpleNamespace(
        id=uuid.uuid4(),
        grade_level="Class 10",
        board_or_curriculum="CBSE",
        exam_name="JEE Main",
        explanation_style={"primary": "worked examples", "anxiety_trigger": "timed tests"},
        language_profile={"explanation_preference": "Hinglish"},
        strong_topics=[{"topic": "quadratic equations", "confidence": 0.9}],
        weak_topics=[
            {
                "topic": "thermodynamics",
                "severity": "severe",
                "specific_gap": "sign convention errors",
            }
        ],
    )

    projections = build_edtech_universal_projections(memory)
    contents = [projection.content for projection in projections]
    source_fields = [projection.source_field for projection in projections]

    assert "Student is in Class 10." in contents
    assert "Student follows CBSE." in contents
    assert "Student is preparing for JEE Main." in contents
    assert "Student learns best through worked examples." in contents
    assert "Student prefers learning explanations in Hinglish." in contents
    assert "Student is strong in quadratic equations." in contents
    assert "Student is working on improving thermodynamics: sign convention errors." in contents
    assert all(projection.source_domain == "edtech" for projection in projections)
    assert all(projection.projection_key.startswith(f"edtech:{memory.id}:") for projection in projections)
    assert all("anxiety" not in field for field in source_fields)


def test_edtech_projection_skips_empty_fields() -> None:
    memory = SimpleNamespace(
        id=uuid.uuid4(),
        grade_level=None,
        board_or_curriculum=None,
        exam_name=None,
        explanation_style=None,
        language_profile=None,
        strong_topics=[],
        weak_topics=[],
    )

    assert build_edtech_universal_projections(memory) == []
