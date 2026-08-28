from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from benchmarks.public.longmemeval.contract import load_dataset
from benchmarks.public.longmemeval.episodic_chunk_selection import (
    _select_cases,
    build_chunks,
)
from benchmarks.public.longmemeval.qa import (
    OFFICIAL_MODEL,
    UPSTREAM_EVALUATOR_URL,
    OpenAIQAClient,
    prompt_sha256,
)

EXPECTED_CASES = 30
EXPECTED_PROVIDER_CALLS = 60


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def _call_payload(call: Any) -> dict[str, Any]:
    return {
        "model": call.model,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "latency_ms": call.latency_ms,
        "estimated_cost_usd": call.estimated_cost_usd,
    }


def build_contexts(
    cases: list[Any],
    evidence_artifact: dict[str, Any],
    semantic_artifact: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    semantic_artifact = semantic_artifact or evidence_artifact
    chunks = build_chunks(cases, role_aware=True)
    by_hash = {chunk.content_hash: chunk for chunk in chunks}
    contexts: dict[str, dict[str, Any]] = {}
    for case in cases:
        question_id = case.question_id
        semantic = {
            row["session_id"]: row
            for row in semantic_artifact["scored_results"][question_id]
        }
        lexical = {
            row["session_id"]: row
            for row in evidence_artifact["lexical_results"][question_id]
        }
        fused = evidence_artifact["fused_results"][question_id][:10]
        blocks = []
        provenance = []
        for rank, row in enumerate(fused, 1):
            session_id = row["session_id"]
            source = semantic.get(session_id) or lexical.get(session_id)
            if source is None:
                raise RuntimeError(f"missing provenance for {question_id}/{session_id}")
            chunk = by_hash[source["content_hash"]]
            blocks.append(
                f"[Evidence {rank}; session={session_id}; date={chunk.occurred_at}; "
                f"roles={','.join(chunk.roles)}]\n{chunk.text}"
            )
            provenance.append(
                {
                    "rank": rank,
                    "session_id": session_id,
                    "content_hash": chunk.content_hash,
                    "occurred_at": chunk.occurred_at,
                    "turn_start": chunk.turn_start,
                    "turn_end": chunk.turn_end,
                    "roles": list(chunk.roles),
                    "semantic_rank": row.get("semantic_rank"),
                    "lexical_rank": row.get("lexical_rank"),
                }
            )
        contexts[question_id] = {
            "text": "\n\n".join(blocks),
            "provenance": provenance,
        }
    return contexts


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_answer_judge_calls:
        raise SystemExit("refusing paid calls without --approve-answer-judge-calls")
    if not args.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required")
    all_cases, dataset_sha256 = load_dataset(args.dataset)
    cases, manifest = _select_cases(all_cases, dataset_sha256, args.manifest)
    if len(cases) != EXPECTED_CASES:
        raise SystemExit("saved-evidence evaluation requires exactly 30 frozen cases")
    evidence = json.loads(args.evidence_artifact.read_text(encoding="utf-8"))
    if evidence.get("dataset_sha256") != dataset_sha256:
        raise SystemExit("evidence artifact dataset hash mismatch")
    if evidence.get("sample_seed") != manifest.get("seed"):
        raise SystemExit("evidence artifact sample mismatch")
    semantic_path = Path(evidence.get("semantic_artifact") or args.evidence_artifact)
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    if semantic.get("dataset_sha256") != dataset_sha256:
        raise SystemExit("semantic provenance artifact dataset hash mismatch")
    if semantic.get("sample_seed") != manifest.get("seed"):
        raise SystemExit("semantic provenance artifact sample mismatch")
    contexts = build_contexts(cases, evidence, semantic)

    checkpoint: dict[str, Any] = {
        "schema_version": "longmemeval-saved-evidence-answer-checkpoint-v1",
        "dataset_sha256": dataset_sha256,
        "sample_seed": manifest["seed"],
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "cases": {},
    }
    if args.checkpoint.exists():
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        expected_identity = (
            dataset_sha256,
            manifest["seed"],
            args.answer_model,
            args.judge_model,
        )
        actual_identity = (
            checkpoint.get("dataset_sha256"),
            checkpoint.get("sample_seed"),
            checkpoint.get("answer_model"),
            checkpoint.get("judge_model"),
        )
        if actual_identity != expected_identity:
            raise SystemExit("checkpoint identity does not match this frozen run")

    async with OpenAIQAClient(api_key=args.openai_api_key) as client:
        for case in cases:
            if case.question_id in checkpoint["cases"]:
                continue
            answer = await client.answer(
                case, contexts[case.question_id]["text"], model=args.answer_model
            )
            judge = await client.judge(case, answer.text, model=args.judge_model)
            checkpoint["cases"][case.question_id] = {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "abstention": case.question_id.endswith("_abs"),
                "evidence": contexts[case.question_id]["provenance"],
                "hypothesis": answer.text,
                "preview_correct": judge.correct,
                "answer": _call_payload(answer),
                "judge": {**_call_payload(judge.call), "raw_label": judge.raw_label},
            }
            _write_atomic(args.checkpoint, checkpoint)

    rows = [checkpoint["cases"][case.question_id] for case in cases]
    per_type = {}
    for question_type in sorted({row["question_type"] for row in rows}):
        typed = [row for row in rows if row["question_type"] == question_type]
        correct = sum(row["preview_correct"] for row in typed)
        per_type[question_type] = {
            "cases": len(typed),
            "correct": correct,
            "preview_accuracy": correct / len(typed),
        }
    total_cost = sum(
        row[stage]["estimated_cost_usd"]
        for row in rows
        for stage in ("answer", "judge")
    )
    artifact = {
        "schema_version": "longmemeval-saved-evidence-answer-eval-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "classification": "public-development-preview",
        "dataset_sha256": dataset_sha256,
        "sample_seed": manifest["seed"],
        "evidence_artifact": str(args.evidence_artifact),
        "semantic_provenance_artifact": str(semantic_path),
        "holdout_accessed": False,
        "production_writes": False,
        "embedding_calls": 0,
        "answer_calls": len(rows),
        "judge_calls": len(rows),
        "maximum_approved_provider_calls": EXPECTED_PROVIDER_CALLS,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "answer_prompt_sha256": prompt_sha256("answer"),
        "judge_prompt_sha256": prompt_sha256("judge"),
        "preview_accuracy": sum(row["preview_correct"] for row in rows) / len(rows),
        "accuracy_by_question_type": per_type,
        "estimated_cost_usd": round(total_cost, 8),
        "pricing_assumption_usd_per_million": {"input": 2.5, "output": 10.0},
        "upstream_evaluator": UPSTREAM_EVALUATOR_URL,
        "official_status": "pending_independent_upstream_evaluation",
        "cases": rows,
        "official_hypotheses": [
            {"question_id": row["question_id"], "hypothesis": row["hypothesis"]}
            for row in rows
        ],
        "note": "Preview judge results are diagnostic, not an official LongMemEval score.",
    }
    _write_atomic(args.output, artifact)
    return artifact


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-artifact", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--answer-model", default=OFFICIAL_MODEL)
    parser.add_argument("--judge-model", default=OFFICIAL_MODEL)
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--approve-answer-judge-calls", action="store_true")
    args = parser.parse_args()
    artifact = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "output": str(args.output),
                "preview_accuracy": artifact["preview_accuracy"],
                "estimated_cost_usd": artifact["estimated_cost_usd"],
                "answer_calls": artifact["answer_calls"],
                "judge_calls": artifact["judge_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
