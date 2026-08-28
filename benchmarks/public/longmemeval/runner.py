from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from benchmarks.public.longmemeval.adapter import (
    MemoryOSLongMemEvalAdapter,
    evidence_session_ids,
)
from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    load_dataset,
    select_smoke_cases,
    select_stratified_smoke_cases,
)
from benchmarks.public.longmemeval.qa import (
    OFFICIAL_MODEL,
    UPSTREAM_EVALUATOR_URL,
    OpenAIQAClient,
    prompt_sha256,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemoryOS LongMemEval adapter")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("validate", "smoke", "full"), default="validate"
    )
    parser.add_argument("--smoke-count", type=int, default=10)
    parser.add_argument(
        "--smoke-strategy",
        choices=("hash", "stratified"),
        default="hash",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("MEMORYOS_BENCHMARK_API_URL")
        or os.getenv("MEMORYOS_API_URL", ""),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("MEMORYOS_BENCHMARK_API_KEY")
        or os.getenv("BENCHMARK_API_KEY", ""),
    )
    parser.add_argument("--agent-id", default=os.getenv("MEMORYOS_BENCHMARK_AGENT_ID"))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--retrieval-limit", type=int, default=10)
    parser.add_argument("--context-max-tokens", type=int, default=2000)
    parser.add_argument("--answer-eval", action="store_true")
    parser.add_argument("--answer-model", default=OFFICIAL_MODEL)
    parser.add_argument("--judge-model", default=OFFICIAL_MODEL)
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument(
        "--approve-answer-eval-calls",
        action="store_true",
        help="Required for paid answer-generation and preview-judge calls.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--approve-provider-calls",
        action="store_true",
        help="Required for smoke/full because ingestion invokes production extraction.",
    )
    return parser


async def run_live(
    *,
    cases: list[LongMemEvalCase],
    dataset_sha256: str,
    args: argparse.Namespace,
    checkpoint_path: Path,
) -> dict[str, Any]:
    if not args.approve_provider_calls:
        raise SystemExit(
            "refusing live ingestion without --approve-provider-calls; validate mode is free"
        )
    if not args.base_url or not args.api_key:
        raise SystemExit(
            "MEMORYOS_BENCHMARK_API_URL and MEMORYOS_BENCHMARK_API_KEY are required"
        )
    run_id = args.run_id or f"lme-{secrets.token_hex(4)}"
    checkpoint = _load_checkpoint(
        checkpoint_path,
        run_id=run_id,
        dataset_sha256=dataset_sha256,
    )
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if args.answer_eval and not args.approve_answer_eval_calls:
        raise SystemExit(
            "refusing paid answer evaluation without --approve-answer-eval-calls"
        )
    if args.answer_eval and not args.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required for --answer-eval")
    qa_client = (
        OpenAIQAClient(api_key=args.openai_api_key) if args.answer_eval else None
    )
    async with MemoryOSLongMemEvalAdapter(
        base_url=args.base_url,
        api_key=args.api_key,
        run_id=run_id,
        agent_id=args.agent_id,
    ) as adapter:
        for case in cases:
            boundary = "memoryos_api_or_provider"
            try:
                case_checkpoint = checkpoint["cases"].setdefault(
                    case.question_id, {"sessions": {}}
                )

                ingestion = await adapter.ingest_case(
                    case,
                    completed_sessions=case_checkpoint["sessions"],
                    on_session_completed=partial(
                        _record_checkpoint_session,
                        checkpoint=checkpoint,
                        case_checkpoint=case_checkpoint,
                        checkpoint_path=checkpoint_path,
                    ),
                )
                retrieval = await adapter.retrieve(
                    case,
                    limit=args.retrieval_limit,
                    context_max_tokens=args.context_max_tokens,
                )
                retrieved_sessions = evidence_session_ids(case, retrieval.evidence)
                expected = set(case.answer_session_ids)
                hit_count = len(expected.intersection(retrieved_sessions))
                row: dict[str, Any] = {
                        "question_id": case.question_id,
                        "question_type": case.question_type,
                        "abstention": case.question_id.endswith("_abs"),
                        "ingestion": ingestion,
                        "retrieval": {
                            "latency_ms": retrieval.latency_ms,
                            "context_token_count": retrieval.context_token_count,
                            "result_count": len(retrieval.evidence),
                            "retrieved_session_ids": retrieved_sessions,
                            "evidence_session_recall": (
                                hit_count / len(expected) if expected else None
                            ),
                            "context": retrieval.system_prompt_addition,
                        },
                    }
                if qa_client is not None:
                    boundary = "answer_generation"
                    answer = await qa_client.answer(
                        case, retrieval.system_prompt_addition, model=args.answer_model
                    )
                    boundary = "preview_judge"
                    judge = await qa_client.judge(
                        case, answer.text, model=args.judge_model
                    )
                    row["answer_evaluation"] = {
                        "hypothesis": answer.text,
                        "preview_correct": judge.correct,
                        "answer": _call_payload(answer),
                        "judge": {
                            **_call_payload(judge.call),
                            "raw_label": judge.raw_label,
                        },
                    }
                rows.append(row)
            except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as exc:
                response_detail: Any = None
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        response_detail = exc.response.json()
                    except (ValueError, json.JSONDecodeError):
                        response_detail = exc.response.text[:2000]
                errors.append(
                    {
                        "question_id": case.question_id,
                        "boundary": boundary,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "http_status": (
                            exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None
                        ),
                        "response_detail": response_detail,
                    }
                )
    if qa_client is not None:
        await qa_client.client.aclose()
    answer_rows = [r for r in rows if "answer_evaluation" in r]
    per_type: dict[str, dict[str, int | float]] = {}
    for question_type in sorted({r["question_type"] for r in answer_rows}):
        typed = [r for r in answer_rows if r["question_type"] == question_type]
        correct = sum(r["answer_evaluation"]["preview_correct"] for r in typed)
        per_type[question_type] = {
            "cases": len(typed),
            "correct": correct,
            "preview_accuracy": correct / len(typed),
        }
    total_cost = sum(
        r["answer_evaluation"][stage]["estimated_cost_usd"]
        for r in answer_rows
        for stage in ("answer", "judge")
    )
    return {
        "schema_version": "longmemeval-memoryos-v2",
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "mode": args.mode,
        "dataset_sha256": dataset_sha256,
        "retrieval_limit": args.retrieval_limit,
        "context_max_tokens": args.context_max_tokens,
        "cases_requested": len(cases),
        "cases_completed": len(rows),
        "errors": errors,
        "cases": rows,
        "answer_evaluation": {
            "enabled": args.answer_eval,
            "answer_model": args.answer_model if args.answer_eval else None,
            "judge_model": args.judge_model if args.answer_eval else None,
            "answer_prompt_sha256": prompt_sha256("answer"),
            "judge_prompt_sha256": prompt_sha256("judge"),
            "preview_accuracy": (
                sum(r["answer_evaluation"]["preview_correct"] for r in answer_rows)
                / len(answer_rows)
                if answer_rows
                else None
            ),
            "accuracy_by_question_type": per_type,
            "estimated_cost_usd": round(total_cost, 8),
            "pricing_assumption_usd_per_million": {"input": 2.5, "output": 10.0},
            "upstream_evaluator": UPSTREAM_EVALUATOR_URL,
            "official_status": "pending_independent_upstream_evaluation",
        },
        "official_hypotheses": [
            {
                "question_id": r["question_id"],
                "hypothesis": r["answer_evaluation"]["hypothesis"],
            }
            for r in answer_rows
        ],
        "note": "Preview judge metrics are diagnostic, not an official LongMemEval score.",
    }


def _call_payload(call: Any) -> dict[str, Any]:
    return {
        "model": call.model,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "latency_ms": call.latency_ms,
        "estimated_cost_usd": call.estimated_cost_usd,
    }


def _load_checkpoint(path: Path, *, run_id: str, dataset_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "longmemeval-checkpoint-v1",
            "run_id": run_id,
            "dataset_sha256": dataset_sha256,
            "cases": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("run_id") != run_id:
        raise ValueError("checkpoint run_id does not match the requested run")
    if payload.get("dataset_sha256") != dataset_sha256:
        raise ValueError("checkpoint dataset checksum does not match")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def _record_checkpoint_session(
    session_id: str,
    outcome: dict[str, Any],
    *,
    checkpoint: dict[str, Any],
    case_checkpoint: dict[str, Any],
    checkpoint_path: Path,
) -> None:
    case_checkpoint["sessions"][session_id] = outcome
    _write_json_atomic(checkpoint_path, checkpoint)


def validation_summary(
    cases: list[LongMemEvalCase], dataset_sha256: str
) -> dict[str, Any]:
    return {
        "valid": True,
        "dataset_sha256": dataset_sha256,
        "case_count": len(cases),
        "question_types": dict(sorted(Counter(c.question_type for c in cases).items())),
        "abstention_count": sum(c.question_id.endswith("_abs") for c in cases),
        "session_count": sum(len(c.haystack_sessions) for c in cases),
        "blank_turns_dropped_at_api_boundary": sum(
            not turn.content.strip()
            for case in cases
            for session in case.haystack_sessions
            for turn in session
        ),
    }


def main() -> None:
    load_dotenv(override=False)
    args = build_parser().parse_args()
    cases, dataset_sha256 = load_dataset(args.dataset)
    summary = validation_summary(cases, dataset_sha256)
    if args.mode == "validate":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    if args.mode == "smoke" and args.smoke_strategy == "stratified":
        selected = select_stratified_smoke_cases(cases)
    elif args.mode == "smoke":
        selected = select_smoke_cases(
            cases, count=min(args.smoke_count, len(cases))
        )
    else:
        selected = cases
    output = (
        args.output
        or Path("benchmarks/public/results")
        / f"{args.run_id or 'longmemeval-run'}.json"
    )
    checkpoint_path = output.with_suffix(".checkpoint.json")
    artifact = asyncio.run(
        run_live(
            cases=selected,
            dataset_sha256=dataset_sha256,
            args=args,
            checkpoint_path=checkpoint_path,
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    if artifact["official_hypotheses"]:
        hypotheses_path = output.with_suffix(".hypotheses.jsonl")
        hypotheses_path.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n"
                for row in artifact["official_hypotheses"]
            ),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                **summary,
                "artifact": str(output),
                "completed": artifact["cases_completed"],
                "errors": len(artifact["errors"]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
