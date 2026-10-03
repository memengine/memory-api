"""Read-only worksheet validation. No scoring, adjudication, provider or writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from api.services.evidence_policy import eligible_user_turn_indexes
from api.services.extraction_service import ExtractionService
from benchmarks.internal.preference_review_schema import (
    ReviewAnnotation,
    ReviewWorksheet,
)
from benchmarks.internal.preference_semantic_contract import Assessment, ContractContext
from benchmarks.internal.preference_semantic_dataset import (
    PACK_DIR,
    ROOT,
    DraftCase,
    context_for,
    load_development_pack,
)

REVIEW_DIR = ROOT / "artifacts/preference-review"
MAX_WORKSHEET_BYTES = 2 * 1024 * 1024


def validate_annotation(annotation: ReviewAnnotation, context: ContractContext) -> None:
    messages = [turn.model_dump(exclude_none=True) for turn in context.turns]
    eligible = set(eligible_user_turn_indexes(messages))
    properties = {item.key for item in context.properties}
    scopes = {item.ref for item in context.scopes}
    targets = {item.ref: item for item in context.targets}
    identities = set()
    for finding in annotation.findings:
        _, reason = ExtractionService._validate_source_spans(
            [span.model_dump() for span in finding.evidence], messages=messages, eligible_indexes=eligible,
        )
        if reason:
            raise ValueError(f"invalid human evidence: {reason}")
        if finding.value is not None and not any(
            span.turn_index == finding.value.turn_index and finding.value.quote in span.quote
            for span in finding.evidence
        ):
            raise ValueError("value must be quoted within cited eligible user evidence")
        if finding.scope_ref is not None and finding.scope_ref not in scopes:
            raise ValueError("unknown scope reference; describe it with scope_ref=null")
        if finding.property_key is not None and finding.property_key not in properties and finding.property_description is None:
            raise ValueError("unknown property requires property_description")
        if finding.target_ref is not None:
            target = targets.get(finding.target_ref)
            if target is None:
                raise ValueError("invented target reference")
            if finding.property_key != target.property_key or finding.scope_ref != target.scope_ref:
                raise ValueError("target/property/scope mismatch")
        if annotation.representability == "unrepresentable":
            continue
        if finding.property_key not in properties or finding.scope_ref not in scopes:
            raise ValueError("representable finding must bind an offered property and scope")
        identity = (finding.property_key, finding.scope_ref)
        if identity in identities:
            raise ValueError("multiple findings for one identity are not representable in the model contract")
        identities.add(identity)
        if finding.target_ref is None and any((item.property_key, item.scope_ref) == identity for item in context.targets):
            raise ValueError("representable finding omitted an existing target")
        # A representable judgment needs one source quote supporting the model's
        # single-quote value rule. Human evidence itself can span several turns.
        evidence = next((span for span in finding.evidence if finding.value is None or (
            span.turn_index == finding.value.turn_index and finding.value.quote in span.quote
        )), None)
        if evidence is None:
            raise ValueError("no single quote can express this finding in the model contract")
        Assessment.model_validate({"kind": "assessment", "property_key": finding.property_key,
            "scope_ref": finding.scope_ref, "target_ref": finding.target_ref, "stance": finding.stance,
            "evidence": evidence.model_dump(), "value": finding.value.model_dump() if finding.value else None})
    # Intentionally no authority/policy override: these are human labels, not
    # proposals for admission. Descriptions do not become evidence or values.


def validate_worksheet(raw: dict, *, pack: dict, inputs_hash: str, require_completed: bool = True) -> ReviewWorksheet:
    sheet = ReviewWorksheet.model_validate(raw)
    if sheet.pack_id != pack["pack_id"] or sheet.inputs_sha256_utf8_lf != inputs_hash:
        raise ValueError("worksheet is bound to a different input pack/hash")
    cases = {raw_case["case_id"]: DraftCase.model_validate(raw_case) for raw_case in pack["cases"]}
    if set(sheet.annotations) != set(cases):
        raise ValueError("worksheet must contain exactly every development case ID, with no omissions or extras")
    if require_completed and sheet.status != "completed":
        raise ValueError("worksheet is not completed; use --allow-incomplete to check work in progress")
    for case_id, annotation in sheet.annotations.items():
        if annotation is not None:
            try:
                validate_annotation(annotation, context_for(pack, cases[case_id]))
            except ValueError as exc:
                raise ValueError(f"{case_id}: {exc}") from exc
    return sheet


def validate_pair(first: ReviewWorksheet, second: ReviewWorksheet) -> None:
    if first.status != "completed" or second.status != "completed":
        raise ValueError("both review passes must be completed")
    if {first.review_pass, second.review_pass} != {"a", "b"}:
        raise ValueError("pair must contain one pass a and one pass b")
    if first.inputs_sha256_utf8_lf != second.inputs_sha256_utf8_lf or set(first.annotations) != set(second.annotations):
        raise ValueError("review passes do not bind the same inputs/cases")
    if first.reviewer_identity.casefold() == second.reviewer_identity.casefold():
        raise ValueError("the two passes must declare different reviewer identities")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_worksheet(path: Path) -> dict:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()) or not resolved.is_relative_to(REVIEW_DIR.resolve()) or resolved.suffix.lower() != ".json" or any(
        part.lower() == "holdout" for part in resolved.parts
    ):
        raise ValueError("review copies must be JSON files under artifacts/preference-review, never holdouts")
    with resolved.open("rb") as stream:
        data = stream.read(MAX_WORKSHEET_BYTES + 1)
    if len(data) > MAX_WORKSHEET_BYTES:
        raise ValueError("worksheet exceeds the 2 MiB file limit")
    # PowerShell 5 Out-File -Encoding utf8 may emit a BOM.
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_keys)


def summary(sheet: ReviewWorksheet) -> dict:
    filled = [annotation for annotation in sheet.annotations.values() if annotation is not None]
    return {"structurally_valid": True, "review_pass": sheet.review_pass, "status": sheet.status,
        "reviewed_cases": len(filled), "unreviewed_cases": len(sheet.annotations) - len(filled),
        "unrepresentable_cases": sum(item.representability == "unrepresentable" for item in filled),
        "ambiguous_cases": sum(item.ambiguity == "ambiguous" for item in filled),
        "language_revision_cases": sum(item.language_quality == "needs_revision" for item in filled),
        "human_identity_verified": False, "semantic_correctness_verified": False,
        "review_independence_verified": False, "adjudication_complete": False,
        "provider_execution_approved": False, "production_enablement_allowed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--worksheet", type=Path)
    action.add_argument("--schema", action="store_true", help="Print authoritative worksheet JSON Schema.")
    parser.add_argument("--other-worksheet", type=Path, help="Check a completed a/b pair; not independent-review certification.")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    if args.schema:
        if args.other_worksheet or args.allow_incomplete:
            parser.error("--schema cannot be combined with worksheet options")
        print(json.dumps(ReviewWorksheet.model_json_schema(), indent=2))
        return
    try:
        pack, _ = load_development_pack()
        manifest = json.loads((PACK_DIR / "manifest-v1.json").read_text(encoding="utf-8"))
        options = {"pack": pack, "inputs_hash": manifest["inputs_sha256_utf8_lf"], "require_completed": not args.allow_incomplete}
        sheet = validate_worksheet(read_worksheet(args.worksheet), **options)
        result = {"worksheet": summary(sheet)}
        if args.other_worksheet:
            other = validate_worksheet(read_worksheet(args.other_worksheet), **options)
            validate_pair(sheet, other)
            result["other_worksheet"] = summary(other)
            result["distinct_declared_reviewers"] = True
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Invalid review worksheet: {exc}\n")


if __name__ == "__main__":
    main()
