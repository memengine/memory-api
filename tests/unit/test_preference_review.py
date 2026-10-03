"""Synthetic validation fixtures only; never completed human review evidence."""
import json
import re
from copy import deepcopy

import pytest
from pydantic import ValidationError

from benchmarks.internal import preference_review as review
from benchmarks.internal.preference_review_schema import (
    HumanFinding,
    ReviewAnnotation,
    ReviewWorksheet,
)
from benchmarks.internal.preference_semantic_contract import EvidenceQuote
from benchmarks.internal.preference_semantic_dataset import (
    PACK_DIR,
    DraftCase,
    blank_worksheet,
    context_for,
    load_development_pack,
)


def annotation(**changes):
    return {"findings": [], "no_findings_reason": "not_preference", "representability": "representable",
        "representability_reason": None, "ambiguity": "unambiguous", "ambiguity_reason": None,
        "language_quality": "natural", "language_notes": None, **changes}


def asserted_finding(pack):
    case = DraftCase.model_validate(pack["cases"][0])
    return {"property_key": "examples.language", "scope_ref": "global", "target_ref": None,
        "stance": "asserted", "evidence": [{"turn_index": 1, "quote": case.turns[1].content}],
        "value": {"turn_index": 1, "quote": "Python"}}


@pytest.fixture(scope="module")
def materials():
    pack, _ = load_development_pack()
    manifest = json.loads((PACK_DIR / "manifest-v1.json").read_text(encoding="utf-8"))
    return pack, manifest["inputs_sha256_utf8_lf"]


def sheet_fixture(materials, *, status="completed", review_pass="a"):
    pack, inputs_hash = materials
    sheet = blank_worksheet(pack, review_pass=review_pass, inputs_hash=inputs_hash)
    if status != "pending":
        sheet.update(status=status, reviewer_identity=f"synthetic-test-reviewer-{review_pass}", reviewer_is_human=True)
        if status == "completed":
            # Deliberately semantically wrong on many cases. Completion checks
            # cannot verify a human's judgment, and must not pretend otherwise.
            sheet["annotations"] = {case_id: annotation() for case_id in sheet["annotations"]}
        else:
            sheet["annotations"]["d0001"] = annotation()
    return sheet


def test_blank_templates_are_typed_but_not_completed(materials):
    pack, inputs_hash = materials
    for review_pass in ("a", "b"):
        raw = json.loads((PACK_DIR / f"review-{review_pass}.blank.json").read_text(encoding="utf-8"))
        assert ReviewWorksheet.model_validate(raw).status == "pending"
        review.validate_worksheet(raw, pack=pack, inputs_hash=inputs_hash, require_completed=False)
        with pytest.raises(ValueError, match="not completed"):
            review.validate_worksheet(raw, pack=pack, inputs_hash=inputs_hash)


def test_asserted_annotation_has_grounded_value_and_required_nullable_fields(materials):
    pack, _ = materials
    case = DraftCase.model_validate(pack["cases"][0])
    finding = asserted_finding(pack)
    record = ReviewAnnotation.model_validate(annotation(findings=[finding], no_findings_reason=None))
    review.validate_annotation(record, context_for(pack, case))
    for required in ("property_key", "scope_ref", "target_ref", "stance", "evidence", "value"):
        raw = deepcopy(finding)
        raw.pop(required)
        with pytest.raises(ValidationError):
            HumanFinding.model_validate(raw)


def test_ambiguous_unrepresentable_meaning_is_preserved_not_forced_to_abstain(materials):
    pack, _ = materials
    case = next(DraftCase.model_validate(raw) for raw in pack["cases"] if raw["case_id"] == "d0271")
    finding = {"property_key": "examples.language", "scope_ref": None, "scope_description": "General or Atlas, unspecified.",
        "target_ref": None, "stance": "asserted", "evidence": [{"turn_index": 1, "quote": case.turns[1].content}],
        "value": None, "value_description": "The setting referred to as 'that', without a quoted value."}
    raw = annotation(findings=[finding], no_findings_reason=None, representability="unrepresentable",
        representability_reason="The adopted value and target scope are not identified in the user quote.",
        ambiguity="ambiguous", ambiguity_reason="Both general and Atlas entries were discussed.",
        alternative_interpretations=["Keep the general default.", "Keep the Atlas default."])
    review.validate_annotation(ReviewAnnotation.model_validate(raw), context_for(pack, case))
    raw.update(representability="representable", representability_reason=None)
    with pytest.raises(ValueError, match="bind an offered"):
        review.validate_annotation(ReviewAnnotation.model_validate(raw), context_for(pack, case))


@pytest.mark.parametrize("changes", [
    {"no_findings_reason": None}, {"representability": "unrepresentable"}, {"representability_reason": "Unnecessary."},
    {"ambiguity": "ambiguous"}, {"ambiguity_reason": "Unnecessary."}, {"alternative_interpretations": ["Unnecessary."]},
    {"language_quality": "needs_revision"}, {"language_notes": "Unnecessary."}, {"no_findings_reason": "unrepresentable"},
    {"no_findings_reason": "anything"}, {"representability": True}, {"authority": 100}, {"confidence": 0.99},
    {"notes": " "}, {"ambiguity": "ambiguous", "ambiguity_reason": "Vague.", "alternative_interpretations": ["Same.", "Same."]},
])
def test_annotation_cross_field_and_enum_rules(changes):
    with pytest.raises(ValidationError):
        ReviewAnnotation.model_validate(annotation(**changes))


def test_language_revision_and_no_preference_cases_are_valid(materials):
    pack, _ = materials
    case = DraftCase.model_validate(pack["cases"][0])
    record = ReviewAnnotation.model_validate(annotation(language_quality="needs_revision", language_notes="Awkward phrasing needs revision."))
    review.validate_annotation(record, context_for(pack, case))
    assert record.findings == ()


@pytest.mark.parametrize("change", ["invented_quote", "wrong_role", "foreign_value", "unknown_scope", "invented_target", "mismatched_target", "bool_index", "oversized_value", "unknown_property", "missing_value"])
def test_finding_checks_reject_invalid_attribution_and_false_shape_claims(materials, change):
    pack, _ = materials
    case = DraftCase.model_validate(pack["cases"][0])
    finding = asserted_finding(pack)
    if change == "invented_quote":
        finding["evidence"][0]["quote"] = "Never said this."
    elif change == "wrong_role":
        finding["evidence"] = [{"turn_index": 0, "quote": case.turns[0].content}]
        finding["value"]["turn_index"] = 0
    elif change == "foreign_value":
        finding["value"]["quote"] = "Ruby"
    elif change == "unknown_scope":
        finding["scope_ref"] = "orion"
    elif change == "invented_target":
        finding["target_ref"] = "invented"
    elif change == "mismatched_target":
        finding["target_ref"] = "t_other"
    elif change == "bool_index":
        finding["evidence"][0]["turn_index"] = True
    elif change == "oversized_value":
        finding["value"]["quote"] = "x" * 257
    elif change == "unknown_property":
        finding.update(property_key="unknown.property", property_description="A new property.")
    else:
        finding.update(value=None, value_description="An adopted option, not a source quote.")
    with pytest.raises(ValueError):
        record = ReviewAnnotation.model_validate(annotation(findings=[finding], no_findings_reason=None))
        review.validate_annotation(record, context_for(pack, case))


def test_cross_turn_interpretation_can_be_recorded_without_inventing_value_evidence(materials):
    pack, _ = materials
    case = DraftCase.model_validate(pack["cases"][0])
    context = context_for(pack, case).model_dump(mode="json")
    context["turns"][1]["content"] = "Choose the second option for future examples."
    finding = asserted_finding(pack)
    finding.update(evidence=[{"turn_index": 1, "quote": context["turns"][1]["content"]}], value=None,
        value_description="Python, named only in the assistant's second option.")
    record = ReviewAnnotation.model_validate(annotation(findings=[finding], no_findings_reason=None,
        representability="unrepresentable", representability_reason="The value is not quoted in a user turn."))
    review.validate_annotation(record, review.ContractContext.model_validate(context))


@pytest.mark.parametrize("change", ["missing_case", "extra_case", "wrong_hash", "null_completed", "false_human", "string_human", "missing_identity", "bad_status", "bad_version", "extra_field"])
def test_completed_worksheet_gate(materials, change):
    pack, inputs_hash = materials
    raw = sheet_fixture(materials)
    if change == "missing_case":
        raw["annotations"].pop("d0001")
    elif change == "extra_case":
        raw["annotations"].pop("d0001")
        raw["annotations"]["d9999"] = annotation()
    elif change == "wrong_hash":
        raw["inputs_sha256_utf8_lf"] = "b" * 64
    elif change == "null_completed":
        raw["annotations"]["d0001"] = None
    elif change == "false_human":
        raw["reviewer_is_human"] = False
    elif change == "string_human":
        raw["reviewer_is_human"] = "true"
    elif change == "missing_identity":
        raw["reviewer_identity"] = " "
    elif change == "bad_status":
        raw["status"] = "approved"
    elif change == "bad_version":
        raw["schema_version"] = "unknown"
    else:
        raw["provider_execution_approved"] = True
    with pytest.raises(ValueError):
        review.validate_worksheet(raw, pack=pack, inputs_hash=inputs_hash)


def test_synthetic_completion_is_not_semantic_correctness_or_identity_verification(materials):
    pack, inputs_hash = materials
    sheet = review.validate_worksheet(sheet_fixture(materials), pack=pack, inputs_hash=inputs_hash)
    result = review.summary(sheet)
    assert result["reviewed_cases"] == 360
    for flag in ("human_identity_verified", "semantic_correctness_verified", "review_independence_verified", "adjudication_complete", "provider_execution_approved", "production_enablement_allowed"):
        assert result[flag] is False


def test_in_progress_requires_explicit_incomplete_mode(materials):
    pack, inputs_hash = materials
    raw = sheet_fixture(materials, status="in_progress")
    with pytest.raises(ValueError, match="not completed"):
        review.validate_worksheet(raw, pack=pack, inputs_hash=inputs_hash)
    sheet = review.validate_worksheet(raw, pack=pack, inputs_hash=inputs_hash, require_completed=False)
    assert review.summary(sheet)["unreviewed_cases"] == 359


def test_completed_pair_requires_distinct_declared_people_and_passes(materials):
    pack, inputs_hash = materials
    options = {"pack": pack, "inputs_hash": inputs_hash}
    first = review.validate_worksheet(sheet_fixture(materials), **options)
    raw = sheet_fixture(materials, review_pass="b")
    second = review.validate_worksheet(raw, **options)
    review.validate_pair(first, second)
    raw["reviewer_identity"] = " SYNTHETIC-TEST-REVIEWER-A "
    with pytest.raises(ValueError, match="different reviewer"):
        review.validate_pair(first, review.validate_worksheet(raw, **options))
    with pytest.raises(ValueError, match="one pass"):
        review.validate_pair(first, first)


def test_file_reader_supports_utf8_bom_and_rejects_duplicate_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    monkeypatch.setattr(review, "REVIEW_DIR", tmp_path)
    path = tmp_path / "a.json"
    path.write_bytes(b'\xef\xbb\xbf{"annotations": {}}')
    assert review.read_worksheet(path) == {"annotations": {}}
    path.write_text('{"annotations":{},"annotations":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        review.read_worksheet(path)
    monkeypatch.setattr(review, "MAX_WORKSHEET_BYTES", 1)
    with pytest.raises(ValueError, match="file limit"):
        review.read_worksheet(path)


def test_forbidden_path_is_rejected_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    monkeypatch.setattr(review, "REVIEW_DIR", tmp_path / "reviews")
    with pytest.raises(ValueError, match="never holdouts"):
        review.read_worksheet(tmp_path / "holdout" / "private.json")


def test_cli_validates_completed_file_and_schema_only_without_writes(materials, monkeypatch, capsys):
    monkeypatch.setattr(review, "read_worksheet", lambda path: sheet_fixture(materials))
    monkeypatch.setattr("sys.argv", ["preference_review", "--worksheet", "artifacts/preference-review/a.json"])
    review.main()
    assert json.loads(capsys.readouterr().out)["worksheet"]["reviewed_cases"] == 360
    monkeypatch.setattr("sys.argv", ["preference_review", "--schema"])
    monkeypatch.setattr(review, "load_development_pack", lambda: pytest.fail("schema printing must not load any data"))
    review.main()
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert "findings" in schema["$defs"]["ReviewAnnotation"]["required"]


def test_generated_schema_exposes_exact_required_and_optional_fields():
    schema = ReviewWorksheet.model_json_schema()
    assert set(schema["required"]) == {
        "schema_version", "pack_id", "inputs_sha256_utf8_lf", "review_pass",
        "reviewer_identity", "reviewer_is_human", "status", "annotations",
    }
    record = schema["$defs"]["ReviewAnnotation"]
    assert set(record["required"]) == {
        "findings", "no_findings_reason", "representability", "representability_reason",
        "ambiguity", "ambiguity_reason", "language_quality", "language_notes",
    }
    assert set(record["properties"]) - set(record["required"]) == {"alternative_interpretations", "notes"}
    finding = schema["$defs"]["HumanFinding"]
    assert set(finding["required"]) == {"property_key", "scope_ref", "target_ref", "stance", "evidence", "value"}
    assert set(finding["properties"]) - set(finding["required"]) == {
        "property_description", "scope_description", "value_description",
    }
    assert finding["properties"]["stance"] == review.Assessment.model_json_schema()["properties"]["stance"]


def test_documented_json_examples_remain_schema_and_source_valid(materials):
    pack, _ = materials
    guide = (review.ROOT / "docs/preference_human_review_schema.md").read_text(encoding="utf-8")
    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", guide, re.DOTALL)]
    assert len(blocks) == 4  # One evidence object and three full annotations.
    EvidenceQuote.model_validate(blocks[0])
    cases = {raw["case_id"]: DraftCase.model_validate(raw) for raw in pack["cases"]}
    for record, case_id in zip(blocks[1:], ("d0001", "d0001", "d0271"), strict=True):
        review.validate_annotation(ReviewAnnotation.model_validate(record), context_for(pack, cases[case_id]))


def test_unrepresentability_cannot_bypass_evidence_attribution(materials):
    pack, _ = materials
    case = DraftCase.model_validate(pack["cases"][0])
    finding = asserted_finding(pack)
    finding.update(evidence=[{"turn_index": 0, "quote": case.turns[0].content}], value=None,
        value_description="A value appearing only in assistant text.")
    record = ReviewAnnotation.model_validate(annotation(findings=[finding], no_findings_reason=None,
        representability="unrepresentable", representability_reason="No user value quote."))
    with pytest.raises(ValueError, match="invalid human evidence"):
        review.validate_annotation(record, context_for(pack, case))
