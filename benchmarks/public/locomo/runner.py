from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.public.locomo.adapter import MemoryOSLoCoMoAdapter, candidate_dialog_ids
from benchmarks.public.locomo.contract import LoCoMoSample, load_dataset


def load_pilot(
    dataset: Path, manifest_path: Path
) -> tuple[LoCoMoSample, int, dict[str, Any], str]:
    samples, digest = load_dataset(dataset)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("dataset_sha256") != digest:
        raise ValueError("pilot manifest does not match the supplied dataset")
    if manifest.get("classification") != "public-benchmark-pilot-not-official-score":
        raise ValueError("unexpected LoCoMo pilot classification")
    questions = manifest.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("pilot manifest contains no questions")
    selected = questions[0]
    sample = next(
        (item for item in samples if item.sample_id == selected.get("sample_id")),
        None,
    )
    if sample is None:
        raise ValueError("pilot sample is missing from the dataset")
    qa_index = int(selected["qa_index"])
    if sample.question_id(qa_index) != selected.get("question_id"):
        raise ValueError("pilot question identity does not match the dataset")
    if sample.qa[qa_index].category != selected.get("category"):
        raise ValueError("pilot question category does not match the dataset")
    return sample, qa_index, manifest, digest


def preflight(sample: LoCoMoSample, qa_index: int, digest: str) -> dict[str, Any]:
    sessions = sample.conversation.sessions()
    return {
        "mode": "preflight",
        "dataset_sha256": digest,
        "sample_id": sample.sample_id,
        "question_id": sample.question_id(qa_index),
        "question_category": sample.qa[qa_index].category,
        "session_count": len(sessions),
        "turn_count": sum(len(turns) for _number, _timestamp, turns in sessions),
        "maximum_extraction_jobs": len(sessions),
        "answer_model_calls": 0,
        "judge_calls": 0,
        "live_provider_calls_required_for_ingestion": True,
        "production_behavior_changed": False,
        "holdout_used": False,
    }


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    sample, qa_index, _manifest, digest = load_pilot(args.dataset, args.manifest)
    if not args.approve_provider_calls:
        raise SystemExit(
            "live smoke requires --approve-provider-calls after reviewing preflight"
        )
    api_key = os.getenv("MEMORYOS_BENCHMARK_API_KEY") or os.getenv("BENCHMARK_API_KEY")
    if not api_key:
        raise SystemExit("MEMORYOS_BENCHMARK_API_KEY or BENCHMARK_API_KEY is required")
    base_url = os.getenv("MEMORYOS_BENCHMARK_API_URL", "http://localhost:8000")
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    async with MemoryOSLoCoMoAdapter(
        base_url=base_url,
        api_key=api_key,
        run_id=run_id,
        agent_id=args.agent_id,
    ) as adapter:
        ingestion = await adapter.ingest_sample(sample)
        retrieval = await adapter.retrieve(
            sample,
            sample.qa[qa_index],
            limit=args.limit,
            context_max_tokens=args.context_max_tokens,
        )
    retrieved_dialog_ids = candidate_dialog_ids(retrieval.memories)
    expected_dialog_ids = list(sample.qa[qa_index].evidence_dialog_ids())
    return {
        "schema_version": "memoryos-locomo-smoke-v1",
        "classification": "public-benchmark-smoke-not-official-score",
        "run_id": run_id,
        "dataset_sha256": digest,
        "sample_id": sample.sample_id,
        "question_id": sample.question_id(qa_index),
        "question_category": sample.qa[qa_index].category,
        "ingestion": ingestion,
        "retrieval": {
            "latency_ms": retrieval.latency_ms,
            "context_token_count": retrieval.context_token_count,
            "memory_count": len(retrieval.memories),
            "memories": [
                {
                    "memory_id": memory.memory_id,
                    "content": memory.content,
                    "relevance_score": memory.relevance_score,
                    "source_event_id": memory.source_event_id,
                    "provenance": memory.provenance,
                }
                for memory in retrieval.memories
            ],
            "candidate_dialog_ids": retrieved_dialog_ids,
            "expected_dialog_ids": expected_dialog_ids,
            "candidate_evidence_recall": (
                len(set(retrieved_dialog_ids) & set(expected_dialog_ids))
                / len(set(expected_dialog_ids))
                if expected_dialog_ids
                else None
            ),
            "attribution_precision_scored": False,
        },
        "answer_model_calls": 0,
        "judge_calls": 0,
        "holdout_used": False,
        "production_behavior_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MemoryOS LoCoMo smoke runner")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mode", choices=("preflight", "smoke"), default="preflight")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--context-max-tokens", type=int, default=2000)
    parser.add_argument("--approve-provider-calls", action="store_true")
    args = parser.parse_args()
    sample, qa_index, _manifest, digest = load_pilot(args.dataset, args.manifest)
    result = (
        preflight(sample, qa_index, digest)
        if args.mode == "preflight"
        else asyncio.run(run_smoke(args))
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
