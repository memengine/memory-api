"""Read-only checks/projections for the unlabelled preference development pack.

No provider, scoring, label generation, database, write path or holdout loader.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field

from benchmarks.internal.preference_review_schema import REVIEW_SCHEMA_VERSION
from benchmarks.internal.preference_semantic_contract import (
    ClosedModel,
    ContractContext,
    SourceTurn,
    model_input,
)

ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = ROOT / "benchmarks/internal/datasets/preference_semantic/development"
PROTOCOL_PATH = ROOT / "benchmarks/internal/protocols/preference-semantic-admission-v1.json"


class DraftCase(ClosedModel):
    case_id: str = Field(pattern=r"^d[0-9]{4}$")
    scenario_group: str = Field(pattern=r"^g[0-9]{3}$")
    language: Literal["en", "hi", "hinglish"]
    family: str
    reference_slice: str
    fixture: str
    turns: tuple[SourceTurn, ...] = Field(min_length=1, max_length=64)


def context_for(pack: dict, case: DraftCase) -> ContractContext:
    fixture = pack["fixtures"].get(case.fixture)
    if fixture is None or set(fixture) != {"targets"}:
        raise ValueError("unknown fixture or unsupported fixture override")
    return ContractContext.model_validate({**pack["common_context"], **fixture,
        "turns": [turn.model_dump(exclude_none=True) for turn in case.turns]})


def review_context(context: ContractContext) -> dict:
    """Semantic review view: retain roles/scopes, omit backend authority and IDs.

    Unlike model_input, this also displays ineligible/no-call source controls so
    human reviewers can label them. It must never replace the provider prefilter.
    """
    return context.model_dump(mode="json", exclude={
        "principal": True,
        "targets": {"__all__": {"memory_id", "tenant_id", "profile_id", "version", "authority", "is_current"}},
        "turns": {"__all__": {"turn_id"}},
    })


def inspect_pack(pack: dict, protocol: dict) -> dict:
    if set(pack) != {"pack_id", "split", "status", "provenance", "common_context", "fixtures", "cases"}:
        raise ValueError("unexpected pack fields; no oracle labels allowed")
    if pack["pack_id"] != "preference-semantic-development-v1" or pack["split"] != "development" or pack["status"] != "unlabelled-human-review-required":
        raise ValueError("only the unreviewed development split is supported")
    if pack["provenance"] != "codex-authored-synthetic-with-related-language-variants":
        raise ValueError("missing synthetic provenance")
    if set(pack["common_context"]) != {"principal", "properties", "scopes"}:
        raise ValueError("unsupported common context override")
    cases = [DraftCase.model_validate(raw) for raw in pack["cases"]]
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    counts, references, languages, groups = Counter(), Counter(), Counter(), {}
    fingerprints = set()
    no_call = 0
    used_fixtures = set()
    for case in cases:
        if case.family not in protocol["families"] or case.reference_slice not in protocol["reference_slices"]:
            raise ValueError("unknown author coverage tag")
        shown = context_for(pack, case)
        view = review_context(shown)
        # Include state/roles, not UUIDs or formatting. This is exact normalized
        # duplicate detection, not a semantic near-duplicate classifier.
        fingerprint = unicodedata.normalize("NFC", json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if fingerprint in fingerprints:
            raise ValueError("duplicate normalized semantic input")
        fingerprints.add(fingerprint)
        counts[(case.language, case.family)] += 1
        references[(case.language, case.reference_slice)] += 1
        languages[case.language] += 1
        group = groups.setdefault(case.scenario_group, [])
        group.append((case.language, case.family, case.reference_slice, case.fixture))
        used_fixtures.add(case.fixture)
        no_call += model_input(shown) is None
    for language in protocol["languages"]:
        for family in protocol["families"]:
            if counts[(language, family)] < protocol["minimum_development_cases_per_language_family"]:
                raise ValueError("language/family coverage shortfall")
        for reference in protocol["reference_slices"]:
            if references[(language, reference)] < protocol["minimum_development_cases_per_language_reference_slice"]:
                raise ValueError("language/reference coverage shortfall")
    if len(cases) < protocol["minimum_development_cases_total"]:
        raise ValueError("total case shortfall")
    for members in groups.values():
        if Counter(member[0] for member in members) != Counter(protocol["languages"]):
            raise ValueError("each related scenario requires exactly one variant per language")
        if len({member[1:] for member in members}) != 1:
            raise ValueError("related scenario metadata/fixture mismatch")
    if used_fixtures != set(pack["fixtures"]):
        raise ValueError("unused fixture")
    return {
        "case_count": len(cases), "related_scenario_count": len(groups), "languages": dict(languages),
        "language_family_counts": {lang: {family: counts[(lang, family)] for family in protocol["families"]} for lang in protocol["languages"]},
        "language_reference_counts": {lang: {ref: references[(lang, ref)] for ref in protocol["reference_slices"]} for lang in protocol["languages"]},
        "prefilter_no_call_cases": no_call, "human_review_complete": False, "provider_execution_approved": False,
        "semantic_accuracy_measured": False, "author_tag_coverage_only": True, "language_quality_reviewed": False,
    }


def load_development_pack() -> tuple[dict, dict]:
    # Exact fixed public paths only; no directory scan or caller-supplied path.
    text = (PACK_DIR / "inputs-v1.json").read_text(encoding="utf-8")
    manifest = json.loads((PACK_DIR / "manifest-v1.json").read_text(encoding="utf-8"))
    if manifest["status"] != "draft-input-seal-not-reviewed-label-freeze" or manifest["hash_encoding"] != "utf-8-lf":
        raise ValueError("not an unreviewed development manifest")
    if any(manifest[flag] is not False for flag in ("provider_execution_approved", "holdout_execution_approved", "production_wiring_allowed")):
        raise ValueError("dataset preparation cannot authorize execution")
    if manifest["label_status"] != "no-labels-authored-no-human-review-or-adjudication":
        raise ValueError("label review is not part of this draft loader")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != manifest["inputs_sha256_utf8_lf"]:
        raise ValueError("development input hash mismatch")
    protocol_text = PROTOCOL_PATH.read_text(encoding="utf-8")
    if hashlib.sha256(protocol_text.encode("utf-8")).hexdigest() != manifest["protocol_sha256_utf8_lf"]:
        raise ValueError("protocol hash mismatch")
    protocol = json.loads(protocol_text)
    pack = json.loads(text)
    report = inspect_pack(pack, protocol)
    if report != manifest["inspection"] or manifest["protocol_id"] != protocol["protocol_id"] or manifest["pack_id"] != pack["pack_id"]:
        raise ValueError("manifest does not match inspected coverage/protocol")
    return pack, report


def blank_worksheet(pack: dict, *, review_pass: Literal["a", "b"], inputs_hash: str) -> dict:
    if review_pass not in {"a", "b"}:
        raise ValueError("only the two human review passes are defined")
    # Differing deterministic order; neither pass carries author tags or answers.
    cases = sorted(pack["cases"], key=lambda raw: hashlib.sha256((review_pass + raw["case_id"]).encode()).hexdigest())
    return {"schema_version": REVIEW_SCHEMA_VERSION, "pack_id": pack["pack_id"], "inputs_sha256_utf8_lf": inputs_hash, "review_pass": review_pass,
        "reviewer_identity": None, "reviewer_is_human": None, "status": "pending",
        "annotations": {raw["case_id"]: None for raw in cases}}


def review_packets(pack: dict) -> list[dict]:
    cases = sorted((DraftCase.model_validate(raw) for raw in pack["cases"]),
        key=lambda case: hashlib.sha256(case.case_id.encode()).hexdigest())
    return [{"case_id": case.case_id, "context": review_context(context_for(pack, case))} for case in cases]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-inputs", action="store_true", help="Print tag-free human review inputs, not a provider request.")
    args = parser.parse_args()
    try:
        pack, report = load_development_pack()
    except ValueError as exc:
        parser.exit(1, f"Invalid development pack: {exc}\n")
    print(json.dumps(review_packets(pack) if args.review_inputs else report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
