from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from benchmarks.internal.cases import ExpectedMemory

TOKEN_RE = re.compile(r"[a-z0-9]+")
RELAXED_SAME_CATEGORY_THRESHOLD = 0.50
NON_DISTINCTIVE_TOKENS = {
    "a", "an", "and", "but", "for", "from", "generally", "in", "is", "it",
    "may", "might", "of", "only", "or", "the", "this", "to", "user", "while",
}
NEGATION_TOKENS = {"cannot", "didnt", "doesnt", "dont", "never", "no", "not", "without"}


@dataclass(frozen=True)
class Match:
    expected_index: int
    actual_index: int
    similarity: float
    method: str = "strict"


def match_memories(
    expected: tuple[ExpectedMemory, ...],
    actual: list[dict[str, Any]],
    *,
    threshold: float = 0.62,
) -> list[Match]:
    """Return a deterministic greedy one-to-one semantic-lexical matching."""
    candidates: list[Match] = []
    for expected_index, item in enumerate(expected):
        forms = (item.proposition, *item.acceptable_paraphrases)
        for actual_index, prediction in enumerate(actual):
            content = str(prediction.get("content") or "")
            similarity = max((_similarity(form, content) for form in forms), default=0.0)
            if similarity >= threshold:
                candidates.append(Match(expected_index, actual_index, similarity, "strict"))
            elif (
                similarity >= RELAXED_SAME_CATEGORY_THRESHOLD
                and _category_is_acceptable(item, prediction)
                and _relaxed_anchor_match(item.proposition, content)
            ):
                candidates.append(Match(expected_index, actual_index, similarity, "relaxed_same_category"))
    matches: list[Match] = []
    used_expected: set[int] = set()
    used_actual: set[int] = set()
    for candidate in sorted(candidates, key=lambda item: (-item.similarity, item.expected_index, item.actual_index)):
        if candidate.expected_index in used_expected or candidate.actual_index in used_actual:
            continue
        matches.append(candidate)
        used_expected.add(candidate.expected_index)
        used_actual.add(candidate.actual_index)
    return sorted(matches, key=lambda item: item.expected_index)


def _similarity(left: str, right: str) -> float:
    left_normalized = " ".join(TOKEN_RE.findall(left.lower()))
    right_normalized = " ".join(TOKEN_RE.findall(right.lower()))
    if not left_normalized or not right_normalized:
        return 0.0
    left_tokens, right_tokens = set(left_normalized.split()), set(right_normalized.split())
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    containment = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return (0.45 * sequence) + (0.35 * jaccard) + (0.20 * containment)


def _category_is_acceptable(expected: ExpectedMemory, actual: dict[str, Any]) -> bool:
    actual_category = str(actual.get("category") or "").lower()
    return actual_category in {expected.category, *expected.acceptable_categories}


def _relaxed_anchor_match(left: str, right: str) -> bool:
    left_tokens = set(TOKEN_RE.findall(left.lower()))
    right_tokens = set(TOKEN_RE.findall(right.lower()))
    if bool(left_tokens & NEGATION_TOKENS) != bool(right_tokens & NEGATION_TOKENS):
        return False
    left_anchors = left_tokens - NON_DISTINCTIVE_TOKENS
    right_anchors = right_tokens - NON_DISTINCTIVE_TOKENS
    shared = left_anchors & right_anchors
    containment = len(shared) / max(1, min(len(left_anchors), len(right_anchors)))
    return len(shared) >= 2 and containment >= 0.40
