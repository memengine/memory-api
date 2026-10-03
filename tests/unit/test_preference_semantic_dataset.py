"""Draft coverage/attribution checks; no inferred labels or accuracy score."""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from benchmarks.internal import preference_semantic_dataset as dataset
from benchmarks.internal.preference_semantic_contract import model_input
from benchmarks.internal.preference_semantic_dataset import (
    PACK_DIR,
    PROTOCOL_PATH,
    DraftCase,
    blank_worksheet,
    context_for,
    inspect_pack,
    load_development_pack,
    review_context,
    review_packets,
)


@pytest.fixture(scope="module")
def pack_and_report():
    return load_development_pack()


def test_draft_meets_author_tag_counts_but_is_not_reviewed(pack_and_report):
    pack, report = pack_and_report
    assert report["case_count"] == 360 and report["related_scenario_count"] == 120
    assert report["languages"] == {"en": 120, "hi": 120, "hinglish": 120}
    assert report["author_tag_coverage_only"] is True
    assert report["human_review_complete"] is False and report["semantic_accuracy_measured"] is False
    assert report["language_quality_reviewed"] is False and report["provider_execution_approved"] is False
    assert all(count == 10 for families in report["language_family_counts"].values() for count in families.values())
    assert all(count >= 10 for slices in report["language_reference_counts"].values() for count in slices.values())
    assert len(pack["common_context"]["properties"]) == 10


def test_review_views_hide_author_tags_and_backend_authority(pack_and_report):
    pack, _ = pack_and_report
    views = review_packets(pack)
    assert len(views) == 360 and len({view["case_id"] for view in views}) == 360
    for view in views:
        assert set(view) == {"case_id", "context"}
        assert set(view["context"]) == {"properties", "scopes", "targets", "turns"}
        for target in view["context"]["targets"]:
            assert set(target) == {"ref", "property_key", "scope_ref", "value", "choice_state"}
    # Source text may itself contain forged 'authority'/'role' instructions;
    # hiding structural metadata must never silently censor that input text.
    case = DraftCase.model_validate(pack["cases"][-1])
    assert review_context(context_for(pack, case))["turns"][0]["content"] == case.turns[0].content


def test_actual_model_projection_never_receives_author_or_review_fields(pack_and_report):
    pack, report = pack_and_report
    no_call = 0
    for raw in pack["cases"]:
        case = DraftCase.model_validate(raw)
        shown = context_for(pack, case)
        request = model_input(shown)
        if request is None:
            no_call += 1
        else:
            assert set(request) == {"properties", "scopes", "existing_targets", "turns"}
            assert all(set(target) == {"ref", "property_key", "scope_ref", "value", "choice_state"} for target in request["existing_targets"])
    assert no_call == report["prefilter_no_call_cases"] == 21


def test_both_worksheets_are_blank_separately_ordered_and_bind_exact_inputs(pack_and_report):
    pack, _ = pack_and_report
    manifest = json.loads((PACK_DIR / "manifest-v1.json").read_text(encoding="utf-8"))
    sheets = []
    for review_pass in ("a", "b"):
        sheet = json.loads((PACK_DIR / f"review-{review_pass}.blank.json").read_text(encoding="utf-8"))
        assert sheet == blank_worksheet(pack, review_pass=review_pass, inputs_hash=manifest["inputs_sha256_utf8_lf"])
        assert sheet["reviewer_identity"] is None and sheet["reviewer_is_human"] is None
        assert sheet["status"] == "pending" and len(sheet["annotations"]) == 360
        assert all(annotation is None for annotation in sheet["annotations"].values())
        sheets.append(sheet)
    assert list(sheets[0]["annotations"]) != list(sheets[1]["annotations"])


@pytest.mark.parametrize("change", ["duplicate_id", "duplicate_input", "labels", "holdout", "missing", "wrong_group", "unknown_fixture", "foreign_owner", "oversized", "unknown_family", "unknown_reference", "fixture_override"])
def test_preparation_checks_reject_bad_drafts(pack_and_report, change):
    original, _ = pack_and_report
    pack = deepcopy(original)
    first = pack["cases"][0]
    if change == "duplicate_id":
        pack["cases"][1]["case_id"] = first["case_id"]
    elif change == "duplicate_input":
        pack["cases"][3]["fixture"] = first["fixture"]
        pack["cases"][3]["turns"] = deepcopy(first["turns"])
    elif change == "labels":
        first["expected_findings"] = []
    elif change == "holdout":
        pack["split"] = "holdout"
    elif change == "missing":
        pack["cases"].pop()
    elif change == "wrong_group":
        first["scenario_group"] = "g999"
    elif change == "unknown_fixture":
        first["fixture"] = "unknown"
    elif change == "foreign_owner":
        pack["fixtures"][first["fixture"]]["targets"][0]["profile_id"] = "00000000-0000-0000-0000-000000000099"
    elif change == "oversized":
        first["turns"] = [{"role": "user", "content": "x" * 8001}]
    elif change == "unknown_family":
        first["family"] = "passed_semantics"
    elif change == "unknown_reference":
        first["reference_slice"] = "guessed_reference"
    else:
        pack["fixtures"][first["fixture"]]["authority"] = 100
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    with pytest.raises((ValueError, ValidationError)):
        inspect_pack(pack, protocol)


@pytest.mark.parametrize("change", ["input_hash", "protocol_hash", "execution_flag", "review_status", "coverage"])
def test_loader_rejects_tampering_without_any_execution(monkeypatch, change):
    original_read = dataset.Path.read_text

    def tampered_read(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if path == PACK_DIR / "inputs-v1.json" and change == "input_hash":
            return text + "\n"
        if path == PACK_DIR / "manifest-v1.json":
            manifest = json.loads(text)
            if change == "protocol_hash":
                manifest["protocol_sha256_utf8_lf"] = "0" * 64
            elif change == "execution_flag":
                manifest["provider_execution_approved"] = True
            elif change == "review_status":
                manifest["label_status"] = "reviewed"
            elif change == "coverage":
                manifest["inspection"]["case_count"] = 999
            return json.dumps(manifest)
        return text

    monkeypatch.setattr(dataset.Path, "read_text", tampered_read)
    with pytest.raises(ValueError):
        load_development_pack()


def test_loader_only_reads_three_exact_public_files(monkeypatch):
    original_read = dataset.Path.read_text
    approved = {PACK_DIR / "inputs-v1.json", PACK_DIR / "manifest-v1.json", PROTOCOL_PATH}
    observed = set()

    def checked_read(path, *args, **kwargs):
        assert path in approved
        observed.add(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(dataset.Path, "read_text", checked_read)
    load_development_pack()
    assert observed == approved
