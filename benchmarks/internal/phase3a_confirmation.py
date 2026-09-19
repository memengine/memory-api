from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.services.extraction_service import ExtractionError, ExtractionService
from api.services.llm_service import AllProvidersFailedError, LLMService, ProviderError
from benchmarks.internal.cases import (
    HOLDOUT_APPROVAL_ENV,
    HOLDOUT_APPROVAL_TOKEN,
    ExpectedMemory,
)
from benchmarks.internal.live_provider import (
    NoopUsageCache,
    RecordingLLMService,
    _estimate_cost,
)
from benchmarks.internal.matching import match_memories
from benchmarks.internal.results import write_run_record

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "phase3a_confirmation"
    / "development"
    / "calibration_v1.json"
)
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "internal-benchmarks" / "phase3a"

PROPOSALS = (
    {
        "content": "I can remember that you prefer one-line diagnoses before numbered troubleshooting steps.",
        "memory": "User prefers one-line diagnoses before numbered troubleshooting steps.",
        "category": "preference",
    },
    {
        "content": "I can remember that you prefer code examples before explanations.",
        "memory": "User prefers code examples before explanations.",
        "category": "preference",
    },
    {
        "content": "I can remember that you plan to migrate the billing service from Ruby to Go this quarter.",
        "memory": "User plans to migrate the billing service from Ruby to Go this quarter.",
        "category": "goal",
    },
)

RELEASE_MINIMUMS = {
    "accepted_precision": 0.98,
    "accepted_recall": 0.90,
    "pending_recall": 0.90,
    "rejected_recall": 0.98,
    "evidence_integrity": 1.0,
}


@dataclass(frozen=True, slots=True)
class ConfirmationCase:
    id: str
    language: str
    reference_type: str
    utterance: str
    expected_outcome: str
    target_ordinal: int | None


def load_confirmation_cases(
    path: str | Path,
    *,
    expected_split: str,
) -> tuple[list[ConfirmationCase], dict[str, int]]:
    source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if raw.get("split") != expected_split:
        raise ValueError(f"Phase 3A evaluator expected {expected_split} data only.")
    if expected_split == "holdout" and os.getenv(HOLDOUT_APPROVAL_ENV) != HOLDOUT_APPROVAL_TOKEN:
        raise PermissionError("Phase 3A holdout is locked without explicit approval token.")
    cases: list[ConfirmationCase] = []
    for reference_type, group in raw.get("reference_types", {}).items():
        expected = str(group.get("expected_outcome") or "")
        for index, entry in enumerate(group.get("utterances") or [], 1):
            if not isinstance(entry, list) or len(entry) not in {2, 3}:
                raise ValueError(f"Invalid calibration entry for {reference_type} #{index}")
            language, utterance = str(entry[0]), str(entry[1])
            target = int(entry[2]) if len(entry) == 3 else (1 if expected == "accepted" else None)
            cases.append(
                ConfirmationCase(
                    id=f"{reference_type}-{language}-{index:02d}",
                    language=language,
                    reference_type=reference_type,
                    utterance=utterance,
                    expected_outcome=expected,
                    target_ordinal=target,
                )
            )
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Phase 3A calibration case IDs must be unique.")
    minimums = {
        "language": int(raw.get("minimum_cases_per_language", 0)),
        "reference_type": int(raw.get("minimum_cases_per_reference_type", 0)),
    }
    _validate_slice_sizes(cases, minimums)
    return cases, minimums


def load_development_cases(
    path: str | Path = DEFAULT_DATASET,
) -> tuple[list[ConfirmationCase], dict[str, int]]:
    return load_confirmation_cases(path, expected_split="development")


def _validate_slice_sizes(cases: list[ConfirmationCase], minimums: dict[str, int]) -> None:
    languages = Counter(case.language for case in cases)
    references = Counter(case.reference_type for case in cases)
    if not languages or min(languages.values()) < minimums["language"]:
        raise ValueError(f"Language slices below minimum: {dict(languages)}")
    if not references or min(references.values()) < minimums["reference_type"]:
        raise ValueError(f"Reference slices below minimum: {dict(references)}")


def _case_input(case: ConfirmationCase) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, str]]]:
    proposal_count = 1 if case.reference_type.startswith("single_") or case.reference_type == "rejection" else 2
    selected = [PROPOSALS[index % len(PROPOSALS)] for index in range(proposal_count)]
    messages: list[dict[str, Any]] = []
    proposal_context: list[dict[str, Any]] = []
    expected: dict[int, dict[str, str]] = {}
    for offset, proposal in enumerate(selected, 1):
        turn_index = len(messages)
        turn_id = f"{case.id}-proposal-{offset}"
        content_hash = hashlib.sha256(proposal["content"].encode("utf-8")).hexdigest()
        messages.append(
            {
                "role": "assistant",
                "content": proposal["content"],
                "source_kind": "assistant_output",
                "is_memory_proposal": True,
                "turn_id": turn_id,
                "turn_content_sha256": content_hash,
            }
        )
        proposal_context.append(
            {
                "id": f"{case.id}-proposal-id-{offset}",
                "group_id": f"{case.id}-group",
                "ordinal": offset,
                "turn_index": turn_index,
                "turn_id": turn_id,
                "content_sha256": content_hash,
            }
        )
        expected[offset] = proposal
    user_content_hash = hashlib.sha256(case.utterance.encode("utf-8")).hexdigest()
    messages.append(
        {
            "role": "user",
            "content": case.utterance,
            "source_kind": "direct_user_input",
            "turn_id": f"{case.id}-user",
            "turn_content_sha256": user_content_hash,
        }
    )
    return messages, proposal_context, expected


async def run_confirmation_evaluation(
    dataset: str | Path,
    *,
    expected_split: str,
    provider_attempts: int = 3,
) -> dict[str, Any]:
    cases, slice_minimums = load_confirmation_cases(
        dataset,
        expected_split=expected_split,
    )
    recorder = RecordingLLMService(LLMService())
    extraction = ExtractionService(
        llm_service=recorder,
        cache_service=NoopUsageCache(),
        proposal_confirmation_enabled=True,
    )
    details: list[dict[str, Any]] = []
    for case in cases:
        messages, proposal_context, expected = _case_input(case)
        started = time.perf_counter()
        result = None
        error: dict[str, str] | None = None
        attempts = 0
        recorder.responses.clear()
        while attempts < provider_attempts:
            attempts += 1
            try:
                result = await extraction.extract(
                    messages=messages,
                    proxy_user_id=f"phase3a-{case.id}",
                    tenant_id="phase3a-development",
                    job_id=f"phase3a-{case.id}",
                    proposal_context=proposal_context,
                )
                break
            except (ProviderError, AllProvidersFailedError) as exc:
                error = {"kind": "provider_error", "type": type(exc).__name__, "message": str(exc)}
                if attempts < provider_attempts:
                    await asyncio.sleep(0.25 * attempts)
            except ExtractionError as exc:
                error = {"kind": "model_output_error", "type": type(exc).__name__, "message": str(exc)}
                break
        latency_ms = (time.perf_counter() - started) * 1000
        if result is None:
            details.append(
                {
                    "id": case.id,
                    "language": case.language,
                    "reference_type": case.reference_type,
                    "expected_outcome": case.expected_outcome,
                    "status": "error",
                    "error": error,
                    "attempts": attempts,
                    "latency_ms": round(latency_ms, 3),
                    "estimated_cost_usd": _estimate_cost(recorder.responses)[0],
                }
            )
            continue

        stored = list(result.memories_to_store)
        pending = list(result.pending_candidates)
        observed_outcome = "accepted" if stored else "pending" if pending else "rejected"
        target_correct = False
        evidence_integrity = True
        if observed_outcome == "accepted":
            target = expected.get(case.target_ordinal or -1)
            predictions = [
                {"content": item.content, "category": str(item.category)} for item in stored
            ]
            target_correct = bool(
                target
                and len(stored) == 1
                and match_memories(
                    (
                        ExpectedMemory(
                            proposition=target["memory"],
                            category=target["category"],
                        ),
                    ),
                    predictions,
                )
            )
            evidence = stored[0].validated_evidence if len(stored) == 1 else {}
            proposal = dict(evidence.get("proposal") or {})
            evidence_integrity = bool(
                len(stored) == 1
                and evidence.get("relation") == "user_confirmed_assistant_proposal"
                and proposal.get("ordinal") == case.target_ordinal
                and proposal.get("id") == f"{case.id}-proposal-id-{case.target_ordinal}"
            )
        elif observed_outcome == "pending":
            target_correct = all(
                item.candidate_reason
                in {"ambiguous_proposal_reference", "proposal_reference_mismatch"}
                for item in pending
            )
            evidence_integrity = not stored
        else:
            target_correct = case.expected_outcome == "rejected"
            evidence_integrity = True
        correct = observed_outcome == case.expected_outcome and target_correct and evidence_integrity
        estimated_cost, pricing_warnings = _estimate_cost(recorder.responses)
        details.append(
            {
                "id": case.id,
                "language": case.language,
                "reference_type": case.reference_type,
                "expected_outcome": case.expected_outcome,
                "observed_outcome": observed_outcome,
                "target_ordinal": case.target_ordinal,
                "target_correct": target_correct,
                "evidence_integrity": evidence_integrity,
                "correct": correct,
                "status": "completed",
                "error": None,
                "attempts": attempts,
                "latency_ms": round(latency_ms, 3),
                "estimated_cost_usd": estimated_cost,
                "pricing_warnings": pricing_warnings,
                "candidate_counts": {"stored": len(stored), "pending": len(pending)},
                "candidate_confidences": [
                    float(item.confidence) for item in [*stored, *pending]
                ],
                "rejection_counts": dict(
                    result.extraction_metadata.get("candidate_validation", {}).get(
                        "rejection_counts", {}
                    )
                ),
            }
        )

    summary = _summarize(details, slice_minimums)
    return {
        "schema_version": "1.0",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "config": {
            "mode": f"phase3a-live-provider-{expected_split}",
            "dataset": str(Path(dataset)),
            "holdout_loaded": expected_split == "holdout",
            "production_extraction_path": "api.services.extraction_service.ExtractionService",
            "proposal_confirmation_enabled": True,
            "provider_attempts": provider_attempts,
            "self_reported_confidence_grants_authority": False,
        },
        "summary": summary,
        "cases": details,
    }


async def run_development_calibration(
    dataset: str | Path = DEFAULT_DATASET,
    *,
    provider_attempts: int = 3,
) -> dict[str, Any]:
    return await run_confirmation_evaluation(
        dataset,
        expected_split="development",
        provider_attempts=provider_attempts,
    )


async def run_approved_holdout_evaluation(
    dataset: str | Path,
    *,
    provider_attempts: int = 3,
) -> dict[str, Any]:
    return await run_confirmation_evaluation(
        dataset,
        expected_split="holdout",
        provider_attempts=provider_attempts,
    )


def _summarize(details: list[dict[str, Any]], slice_minimums: dict[str, int]) -> dict[str, Any]:
    completed = [item for item in details if item["status"] == "completed"]
    expected_accept = [item for item in completed if item["expected_outcome"] == "accepted"]
    observed_accept = [item for item in completed if item.get("observed_outcome") == "accepted"]
    correct_accept = [item for item in observed_accept if item["correct"]]
    expected_pending = [item for item in completed if item["expected_outcome"] == "pending"]
    expected_reject = [item for item in completed if item["expected_outcome"] == "rejected"]

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    metrics = {
        "accepted_precision": ratio(len(correct_accept), len(observed_accept)),
        "accepted_recall": ratio(len(correct_accept), len(expected_accept)),
        "pending_recall": ratio(
            sum(item["correct"] for item in expected_pending), len(expected_pending)
        ),
        "rejected_recall": ratio(
            sum(item["correct"] for item in expected_reject), len(expected_reject)
        ),
        "evidence_integrity": ratio(
            sum(item["evidence_integrity"] for item in observed_accept), len(observed_accept)
        ),
    }
    latencies = sorted(float(item["latency_ms"]) for item in completed)
    p99_index = min(len(latencies) - 1, round((len(latencies) - 1) * 0.99)) if latencies else 0
    cost = sum(float(item.get("estimated_cost_usd", 0.0)) for item in details)
    slices: dict[str, dict[str, dict[str, float | int]]] = {}
    for field in ("language", "reference_type"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in completed:
            grouped[str(item[field])].append(item)
        slices[field] = {
            name: {
                "case_count": len(items),
                "accuracy": ratio(sum(item["correct"] for item in items), len(items)),
            }
            for name, items in sorted(grouped.items())
        }
    failures = [
        f"{name}={metrics[name]:.4f} below {minimum:.4f}"
        for name, minimum in RELEASE_MINIMUMS.items()
        if metrics[name] < minimum
    ]
    error_count = len(details) - len(completed)
    if error_count:
        failures.append(f"provider_or_model_errors={error_count}")
    for field, minimum in slice_minimums.items():
        for name, values in slices[field].items():
            if int(values["case_count"]) < minimum:
                failures.append(f"{field}:{name} has {values['case_count']} cases below {minimum}")
    return {
        "attempted_cases": len(details),
        "completed_cases": len(completed),
        "error_count": error_count,
        **metrics,
        "overall_accuracy": ratio(sum(item["correct"] for item in completed), len(completed)),
        "latency_ms": {
            "mean": sum(latencies) / len(latencies) if latencies else 0.0,
            "p99": latencies[p99_index] if latencies else 0.0,
        },
        "estimated_cost_usd": cost,
        "estimated_cost_at_turn_volume_usd": {
            "1000": cost / len(details) * 1_000 if details else 0.0,
            "10000": cost / len(details) * 10_000 if details else 0.0,
            "100000": cost / len(details) * 100_000 if details else 0.0,
        },
        "slices": slices,
        "minimums": RELEASE_MINIMUMS,
        "release_gate_failures": failures,
        "release_eligible": not failures,
        "confidence_note": (
            "Candidate confidence is recorded for diagnostics only. It is not calibrated as "
            "confirmation probability and never grants proposal authority."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 3A development calibration.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    record = asyncio.run(run_development_calibration(args.dataset))
    output = args.output or DEFAULT_OUTPUT_ROOT / f"{record['run_id']}-development.json"
    write_run_record(record, output)
    print(json.dumps({"output": str(output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
