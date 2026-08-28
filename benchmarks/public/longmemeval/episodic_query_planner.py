from __future__ import annotations

import re


def _clean(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip(" ,?.!"))
    return re.sub(r"^(?:and|or)\s+", "", cleaned, flags=re.IGNORECASE)


def plan_query(question: str) -> list[str]:
    """Create literal subqueries without inferring facts or benchmark answers."""
    original = _clean(question)
    variants = [original]

    comparison = re.match(
        r"^(?:who|which|what)\s+(.+?)\s+first\s*,\s*(.+?)\s+or\s+(.+)$",
        original,
        flags=re.IGNORECASE,
    )
    if comparison:
        predicate, left, right = map(_clean, comparison.groups())
        variants.extend(
            [
                left,
                right,
                f"{left} {predicate}",
                f"{right} {predicate}",
            ]
        )

    coordinated_actions = re.match(
        r"^how many\s+(.+?)\s+did i\s+(.+?)(?:\s+(?:in|during|over|within)\s+.+)?$",
        original,
        flags=re.IGNORECASE,
    )
    if coordinated_actions:
        focus, actions_text = map(_clean, coordinated_actions.groups())
        actions_text = re.sub(
            r"\s+(?:this|last|past)\s+(?:day|week|month|year)s?$",
            "",
            actions_text,
            flags=re.IGNORECASE,
        )
        actions = [
            _clean(action)
            for action in re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", actions_text)
            if _clean(action)
        ]
        if 2 <= len(actions) <= 8:
            variants.extend(f"{focus} {action}" for action in actions)

    if re.search(r"\b(?:total|combined|altogether)\b", original, re.IGNORECASE):
        conjunction = re.search(r"\bof\s+(.+?)\s+and\s+(.+)$", original, re.IGNORECASE)
        if conjunction:
            left, right = map(_clean, conjunction.groups())
            variants.extend([left, right])

    return list(dict.fromkeys(variant for variant in variants if variant))


__all__ = ["plan_query"]
