from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RUBRIC = {
    "1": "Temporary or almost worthless beyond the current session.",
    "3": "Occasionally useful context, relevant to fewer than about 20% of conversations.",
    "5": "Consistently useful primary context, relevant to roughly half of conversations.",
    "7": "Identity-level context or immediate priority shaping most relevant responses.",
    "9": "Foundational identity that should almost always be retrieved.",
}
PRIORITY_CATEGORIES = ("preference", "relationship", "fact", "goal")


def load_candidates(capture_dir: Path) -> list[dict[str, Any]]:
    candidates = []
    for path in sorted(capture_dir.glob("*.json")):
        capture = json.loads(path.read_text(encoding="utf-8"))
        for memory in capture.get("memories", []):
            candidates.append({
                "capture": path.name,
                "memory_index": memory["memory_index"],
                "category": memory["category"],
                "disposition": memory["disposition"],
                "memory": memory["content"],
                "evidence": capture.get("messages", []),
                "model_score": float(memory["model_score"]),
                "deterministic_score": float(memory["deterministic_score"]),
                "absolute_delta": abs(float(memory["delta"])),
            })
    return candidates


def select_sample(candidates: list[dict[str, Any]], size: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    pool = list(candidates)
    rng.shuffle(pool)
    selected: list[dict[str, Any]] = []

    def take(predicate, count: int) -> None:
        for item in list(pool):
            if len([row for row in selected if predicate(row)]) >= count:
                break
            if predicate(item):
                selected.append(item)
                pool.remove(item)

    per_category = max(2, size // 8)
    for category in PRIORITY_CATEGORIES:
        take(lambda item, category=category: item["category"] == category, per_category)
    take(lambda item: item["disposition"] == "pending", max(3, size // 6))
    take(lambda item: item["absolute_delta"] >= 2, max(4, size // 4))
    take(lambda item: item["absolute_delta"] <= 1, max(4, size // 4))
    for item in pool:
        if len(selected) >= size:
            break
        selected.append(item)
    return selected[:size]


def prepare(capture_dir: Path, packet_path: Path, key_path: Path, size: int, seed: int) -> None:
    candidates = load_candidates(capture_dir)
    if len(candidates) < size:
        raise RuntimeError(f"Need {size} captured memories; found {len(candidates)}")
    selected = select_sample(candidates, size, seed)
    rng = random.Random(seed + 1)
    packet_items = []
    key_items = []
    for index, item in enumerate(selected, 1):
        review_id = f"natural-shadow-{index:03d}"
        swapped = bool(rng.getrandbits(1))
        score_a = item["deterministic_score"] if swapped else item["model_score"]
        score_b = item["model_score"] if swapped else item["deterministic_score"]
        packet_items.append({
            "review_id": review_id,
            "category": item["category"],
            "disposition": item["disposition"],
            "memory": item["memory"],
            "evidence": item["evidence"],
            "reviews": [
                {"reviewer_id": "reviewer_1", "importance_range": [None, None], "ambiguous": None, "notes": ""},
                {"reviewer_id": "reviewer_2", "importance_range": [None, None], "ambiguous": None, "notes": ""},
            ],
        })
        key_items.append({
            "review_id": review_id,
            "capture": item["capture"],
            "memory_index": item["memory_index"],
            "absolute_delta": item["absolute_delta"],
            "score_a": score_a,
            "score_b": score_b,
            "score_a_source": "deterministic" if swapped else "model",
            "score_b_source": "model" if swapped else "deterministic",
        })
    packet = {"schema_version": "1.0", "status": "awaiting_human_labels", "instructions": "Each reviewer independently records a range before opening the sealed key. Set ambiguous=true only when the rubric cannot support a defensible range.", "rubric": RUBRIC, "items": packet_items}
    key = {"schema_version": "1.0", "sealed": True, "packet": packet_path.name, "items": key_items}
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")


def score(packet_path: Path, key_path: Path, output: Path) -> None:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    key = json.loads(key_path.read_text(encoding="utf-8"))
    provenance = {item["review_id"]: item for item in key["items"]}
    rows = []
    ambiguity = 0
    for item in packet["items"]:
        reviews = item["reviews"]
        if any(review["ambiguous"] is None or any(value is None for value in review["importance_range"]) for review in reviews):
            raise RuntimeError(f"Human labels incomplete for {item['review_id']}")
        ranges = [review["importance_range"] for review in reviews]
        low, high = max(value[0] for value in ranges), min(value[1] for value in ranges)
        disputed = any(review["ambiguous"] for review in reviews) or low > high
        ambiguity += disputed
        source = provenance[item["review_id"]]
        scores = {
            source["score_a_source"]: float(source["score_a"]),
            source["score_b_source"]: float(source["score_b"]),
        }
        rows.append({"review_id": item["review_id"], "category": item["category"], "disposition": item["disposition"], "large_disagreement": source["absolute_delta"] >= 2, "ambiguous": disputed, "expected_range": None if disputed else [low, high], "scores": scores})

    eligible = [row for row in rows if not row["ambiguous"]]
    def outcome(row: dict[str, Any], scorer: str) -> str:
        low, high = row["expected_range"]
        value = row["scores"][scorer]
        return "under" if value < low else "over" if value > high else "within"
    def metrics(items: list[dict[str, Any]], scorer: str) -> dict[str, Any]:
        counts = Counter(outcome(row, scorer) for row in items)
        return {"count": len(items), "accuracy": counts["within"] / len(items) if items else None, "under": counts["under"], "over": counts["over"]}
    result = {
        "schema_version": "1.0",
        "reviewed_count": len(rows),
        "eligible_count": len(eligible),
        "inter_review_ambiguity_count": ambiguity,
        "model": metrics(eligible, "model"),
        "deterministic": metrics(eligible, "deterministic"),
        "by_category": {category: {"model": metrics([row for row in eligible if row["category"] == category], "model"), "deterministic": metrics([row for row in eligible if row["category"] == category], "deterministic")} for category in sorted({row["category"] for row in eligible})},
        "by_disposition": {disposition: {"model": metrics([row for row in eligible if row["disposition"] == disposition], "model"), "deterministic": metrics([row for row in eligible if row["disposition"] == disposition], "deterministic")} for disposition in sorted({row["disposition"] for row in eligible})},
        "large_disagreements": {"model": metrics([row for row in eligible if row["large_disagreement"]], "model"), "deterministic": metrics([row for row in eligible if row["large_disagreement"]], "deterministic")},
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or score a blinded natural-traffic importance review.")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("capture_dir", type=Path)
    prepare_parser.add_argument("packet", type=Path)
    prepare_parser.add_argument("sealed_key", type=Path)
    prepare_parser.add_argument("--size", type=int, default=40)
    prepare_parser.add_argument("--seed", type=int, default=20260809)
    score_parser = sub.add_parser("score")
    score_parser.add_argument("packet", type=Path)
    score_parser.add_argument("sealed_key", type=Path)
    score_parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.capture_dir, args.packet, args.sealed_key, args.size, args.seed)
    else:
        score(args.packet, args.sealed_key, args.output)


if __name__ == "__main__":
    main()
