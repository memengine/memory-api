from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.public.longmemeval.contract import load_dataset
from benchmarks.public.longmemeval.episodic_chunk_selection import (
    _select_cases,
    build_chunks,
)
from benchmarks.public.longmemeval.episodic_shadow import _token_count

POLICIES = ("semantic_first", "lexical_first", "bounded_union")
FUSED_SESSION_LIMIT = 10
NEIGHBOR_RADII = (0, 1, 2)
CONTEXT_TOKEN_CAP = 6000
TOKEN_CAP_CURVE = (6000, 8000, 10000)


def select_chunk_hashes(
    fused_row: dict[str, Any],
    semantic: dict[str, dict[str, Any]],
    lexical: dict[str, dict[str, Any]],
    policy: str,
) -> list[str]:
    session_id = fused_row["session_id"]
    semantic_hash = semantic.get(session_id, {}).get("content_hash")
    lexical_hash = lexical.get(session_id, {}).get("content_hash")
    if policy == "semantic_first":
        ordered = [semantic_hash, lexical_hash]
    elif policy == "lexical_first":
        ordered = [lexical_hash, semantic_hash]
    elif policy == "bounded_union":
        ordered = [semantic_hash, lexical_hash]
    else:
        raise ValueError(f"unknown chunk-selection policy: {policy}")
    unique = list(dict.fromkeys(value for value in ordered if value))
    return unique[:2] if policy == "bounded_union" else unique[:1]


def evaluate_policy(
    cases: list[Any],
    hybrid: dict[str, Any],
    semantic_artifact: dict[str, Any],
    chunks_by_hash: dict[str, Any],
    policy: str,
) -> dict[str, Any]:
    rows = []
    for case in cases:
        question_id = case.question_id
        semantic = {
            row["session_id"]: row
            for row in semantic_artifact["scored_results"][question_id]
        }
        lexical = {
            row["session_id"]: row for row in hybrid["lexical_results"][question_id]
        }
        selected = []
        for fused_row in hybrid["fused_results"][question_id][:FUSED_SESSION_LIMIT]:
            for content_hash in select_chunk_hashes(
                fused_row, semantic, lexical, policy
            ):
                chunk = chunks_by_hash[content_hash]
                selected.append(
                    {
                        "session_id": chunk.session_id,
                        "content_hash": chunk.content_hash,
                        "turn_start": chunk.turn_start,
                        "turn_end": chunk.turn_end,
                        "roles": list(chunk.roles),
                    }
                )
        answer_turns = {
            (session_id, turn_index)
            for session_id, turns in zip(
                case.haystack_session_ids, case.haystack_sessions, strict=True
            )
            for turn_index, turn in enumerate(turns)
            if turn.has_answer
        }
        covered_turns = {
            (row["session_id"], turn_index)
            for row in selected
            for turn_index in range(row["turn_start"], row["turn_end"] + 1)
        }
        covered_answers = answer_turns & covered_turns
        context_tokens = sum(
            _token_count(chunks_by_hash[row["content_hash"]].text) for row in selected
        )
        rows.append(
            {
                "question_id": question_id,
                "question_type": case.question_type,
                "abstention": question_id.endswith("_abs"),
                "answer_turn_count": len(answer_turns),
                "covered_answer_turn_count": len(covered_answers),
                "answer_turn_coverage": len(covered_answers) / len(answer_turns)
                if answer_turns
                else None,
                "all_answer_turns_covered": answer_turns <= covered_turns,
                "selected_chunk_count": len(selected),
                "context_tokens_estimated": context_tokens,
                "selected_chunks": selected,
            }
        )
    labelled = [row for row in rows if row["answer_turn_coverage"] is not None]
    return {
        "policy": policy,
        "mean_answer_turn_coverage": sum(
            row["answer_turn_coverage"] for row in labelled
        )
        / len(labelled),
        "complete_answer_turn_coverage_rate": sum(
            row["all_answer_turns_covered"] for row in labelled
        )
        / len(labelled),
        "total_selected_chunks": sum(row["selected_chunk_count"] for row in rows),
        "total_context_tokens_estimated": sum(
            row["context_tokens_estimated"] for row in rows
        ),
        "mean_context_tokens_estimated": sum(
            row["context_tokens_estimated"] for row in rows
        )
        / len(rows),
        "cases": rows,
    }


def expand_selected_chunks(
    selected: list[dict[str, Any]],
    chunks_by_session: dict[tuple[str, str], list[Any]],
    question_id: str,
    radius: int,
    token_cap: int = CONTEXT_TOKEN_CAP,
    allocation: str = "sequential",
) -> tuple[list[Any], bool]:
    if radius not in NEIGHBOR_RADII:
        raise ValueError(f"unsupported neighbor radius: {radius}")
    seeds = []
    for row in selected:
        session_chunks = chunks_by_session[(question_id, row["session_id"])]
        index_by_hash = {
            chunk.content_hash: position
            for position, chunk in enumerate(session_chunks)
        }
        center = index_by_hash[row["content_hash"]]
        seeds.append((session_chunks, center))

    if allocation == "sequential":
        visits = [
            (session_chunks, position)
            for session_chunks, center in seeds
            for position in [
                center,
                *[
                    neighbor
                    for distance in range(1, radius + 1)
                    for neighbor in (center - distance, center + distance)
                ],
            ]
        ]
    elif allocation == "round_robin":
        visits = [
            (session_chunks, center)
            for session_chunks, center in seeds
        ]
        for distance in range(1, radius + 1):
            for direction in (-1, 1):
                visits.extend(
                    (session_chunks, center + direction * distance)
                    for session_chunks, center in seeds
                )
    else:
        raise ValueError(f"unsupported allocation policy: {allocation}")

    ordered_candidates = []
    seen = set()
    for session_chunks, position in visits:
            if position < 0 or position >= len(session_chunks):
                continue
            chunk = session_chunks[position]
            if chunk.content_hash not in seen:
                seen.add(chunk.content_hash)
                ordered_candidates.append(chunk)

    accepted = []
    tokens = 0
    truncated = False
    for chunk in ordered_candidates:
        chunk_tokens = _token_count(chunk.text)
        if tokens + chunk_tokens > token_cap:
            truncated = True
            continue
        accepted.append(chunk)
        tokens += chunk_tokens
    return accepted, truncated


def evaluate_expansion(
    base_policy: dict[str, Any],
    cases: list[Any],
    chunks_by_session: dict[tuple[str, str], list[Any]],
    radius: int,
    allocation: str = "sequential",
    token_cap: int = CONTEXT_TOKEN_CAP,
) -> dict[str, Any]:
    base_by_id = {row["question_id"]: row for row in base_policy["cases"]}
    rows = []
    for case in cases:
        base = base_by_id[case.question_id]
        expanded, truncated = expand_selected_chunks(
            base["selected_chunks"],
            chunks_by_session,
            case.question_id,
            radius,
            allocation=allocation,
            token_cap=token_cap,
        )
        answer_turns = {
            (session_id, turn_index)
            for session_id, turns in zip(
                case.haystack_session_ids, case.haystack_sessions, strict=True
            )
            for turn_index, turn in enumerate(turns)
            if turn.has_answer
        }
        covered_turns = {
            (chunk.session_id, turn_index)
            for chunk in expanded
            for turn_index in range(chunk.turn_start, chunk.turn_end + 1)
        }
        covered_answers = answer_turns & covered_turns
        context_tokens = sum(_token_count(chunk.text) for chunk in expanded)
        rows.append(
            {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "abstention": case.question_id.endswith("_abs"),
                "answer_turn_count": len(answer_turns),
                "covered_answer_turn_count": len(covered_answers),
                "answer_turn_coverage": len(covered_answers) / len(answer_turns)
                if answer_turns
                else None,
                "all_answer_turns_covered": answer_turns <= covered_turns,
                "selected_chunk_count": len(expanded),
                "context_tokens_estimated": context_tokens,
                "token_cap_truncated": truncated,
                "selected_chunks": [
                    {
                        "session_id": chunk.session_id,
                        "content_hash": chunk.content_hash,
                        "turn_start": chunk.turn_start,
                        "turn_end": chunk.turn_end,
                        "roles": list(chunk.roles),
                    }
                    for chunk in expanded
                ],
            }
        )
    labelled = [row for row in rows if row["answer_turn_coverage"] is not None]
    return {
        "base_policy": base_policy["policy"],
        "neighbor_radius": radius,
        "allocation": allocation,
        "context_token_cap": token_cap,
        "mean_answer_turn_coverage": sum(
            row["answer_turn_coverage"] for row in labelled
        )
        / len(labelled),
        "complete_answer_turn_coverage_rate": sum(
            row["all_answer_turns_covered"] for row in labelled
        )
        / len(labelled),
        "total_selected_chunks": sum(row["selected_chunk_count"] for row in rows),
        "total_context_tokens_estimated": sum(
            row["context_tokens_estimated"] for row in rows
        ),
        "mean_context_tokens_estimated": sum(
            row["context_tokens_estimated"] for row in rows
        )
        / len(rows),
        "truncated_case_count": sum(row["token_cap_truncated"] for row in rows),
        "cases": rows,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    all_cases, dataset_sha256 = load_dataset(args.dataset)
    cases, manifest = _select_cases(all_cases, dataset_sha256, args.manifest)
    hybrid = json.loads(args.hybrid_artifact.read_text(encoding="utf-8"))
    semantic_path = Path(hybrid["semantic_artifact"])
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    for artifact, name in ((hybrid, "hybrid"), (semantic, "semantic")):
        if artifact.get("dataset_sha256") != dataset_sha256:
            raise SystemExit(f"{name} artifact dataset hash mismatch")
        if artifact.get("sample_seed") != manifest.get("seed"):
            raise SystemExit(f"{name} artifact sample mismatch")
    chunks = build_chunks(cases, role_aware=True)
    chunks_by_hash = {chunk.content_hash: chunk for chunk in chunks}
    chunks_by_session: dict[tuple[str, str], list[Any]] = {}
    for chunk in chunks:
        chunks_by_session.setdefault((chunk.question_id, chunk.session_id), []).append(
            chunk
        )
    for session_chunks in chunks_by_session.values():
        session_chunks.sort(key=lambda chunk: chunk.chunk_index)
    policies = [
        evaluate_policy(cases, hybrid, semantic, chunks_by_hash, policy)
        for policy in POLICIES
    ]
    bounded_union = next(
        policy for policy in policies if policy["policy"] == "bounded_union"
    )
    expansions = [
        evaluate_expansion(
            bounded_union,
            cases,
            chunks_by_session,
            radius,
            allocation=allocation,
        )
        for allocation in ("sequential", "round_robin")
        for radius in NEIGHBOR_RADII
    ]
    token_cap_curve = [
        evaluate_expansion(
            bounded_union,
            cases,
            chunks_by_session,
            radius=2,
            allocation="sequential",
            token_cap=token_cap,
        )
        for token_cap in TOKEN_CAP_CURVE
    ]
    return {
        "schema_version": "longmemeval-context-chunk-selection-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "classification": "public-development-offline-diagnostic",
        "dataset_sha256": dataset_sha256,
        "sample_seed": manifest["seed"],
        "hybrid_artifact": str(args.hybrid_artifact),
        "semantic_artifact": str(semantic_path),
        "holdout_accessed": False,
        "production_writes": False,
        "provider_calls": 0,
        "selection_uses_gold_labels": False,
        "gold_labels_used_only_for_post_selection_measurement": True,
        "policies": policies,
        "neighbor_expansions": expansions,
        "token_cap_curve": token_cap_curve,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--hybrid-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "policies": [
                    {
                        "policy": row["policy"],
                        "mean_answer_turn_coverage": row[
                            "mean_answer_turn_coverage"
                        ],
                        "complete_answer_turn_coverage_rate": row[
                            "complete_answer_turn_coverage_rate"
                        ],
                        "mean_context_tokens_estimated": row[
                            "mean_context_tokens_estimated"
                        ],
                    }
                    for row in artifact["policies"]
                ],
                "neighbor_expansions": [
                    {
                        "neighbor_radius": row["neighbor_radius"],
                        "allocation": row["allocation"],
                        "mean_answer_turn_coverage": row[
                            "mean_answer_turn_coverage"
                        ],
                        "complete_answer_turn_coverage_rate": row[
                            "complete_answer_turn_coverage_rate"
                        ],
                        "mean_context_tokens_estimated": row[
                            "mean_context_tokens_estimated"
                        ],
                        "truncated_case_count": row["truncated_case_count"],
                    }
                    for row in artifact["neighbor_expansions"]
                ],
                "token_cap_curve": [
                    {
                        "context_token_cap": row["context_token_cap"],
                        "mean_answer_turn_coverage": row[
                            "mean_answer_turn_coverage"
                        ],
                        "complete_answer_turn_coverage_rate": row[
                            "complete_answer_turn_coverage_rate"
                        ],
                        "mean_context_tokens_estimated": row[
                            "mean_context_tokens_estimated"
                        ],
                        "truncated_case_count": row["truncated_case_count"],
                    }
                    for row in artifact["token_cap_curve"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
