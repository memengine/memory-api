from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from api.services.llm_service import LLMService
from benchmarks.internal.phase3a_confirmation import DEFAULT_DATASET

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "phase3a_confirmation"
    / "holdout"
    / "holdout_v1.json"
)


def _normalized_utterances(payload: dict[str, Any]) -> set[str]:
    return {
        " ".join(str(entry[1]).lower().split())
        for group in payload.get("reference_types", {}).values()
        for entry in group.get("utterances", [])
        if isinstance(entry, list) and len(entry) >= 2
    }


def _validate(payload: dict[str, Any], development: dict[str, Any]) -> dict[str, int]:
    if payload.get("split") != "holdout":
        raise ValueError("Generated dataset is not marked as holdout.")
    groups = payload.get("reference_types") or {}
    expected_groups = {
        "single_vague": "accepted",
        "single_indirect": "accepted",
        "explicit_ordinal": "accepted",
        "ambiguous_multi": "pending",
        "rejection": "rejected",
    }
    if set(groups) != set(expected_groups):
        raise ValueError("Generated holdout has incorrect reference groups.")
    languages: dict[str, int] = {"en": 0, "hinglish": 0, "hi": 0}
    for name, outcome in expected_groups.items():
        group = groups[name]
        entries = group.get("utterances") or []
        if group.get("expected_outcome") != outcome or len(entries) != 10:
            raise ValueError(f"Generated holdout group {name} has invalid labels or count.")
        for entry in entries:
            expected_length = 3 if name == "explicit_ordinal" else 2
            if not isinstance(entry, list) or len(entry) != expected_length:
                raise ValueError(f"Generated holdout group {name} has invalid entry shape.")
            language = str(entry[0])
            if language not in languages or not str(entry[1]).strip():
                raise ValueError("Generated holdout has an invalid language or empty utterance.")
            languages[language] += 1
            if name == "explicit_ordinal" and int(entry[2]) not in {1, 2}:
                raise ValueError("Generated ordinal target must be 1 or 2.")
    if min(languages.values()) < 15:
        raise ValueError(f"Generated holdout language slices are too small: {languages}")
    holdout_utterances = _normalized_utterances(payload)
    if len(holdout_utterances) != 50:
        raise ValueError("Generated holdout utterances must be unique.")
    overlap = holdout_utterances & _normalized_utterances(development)
    if overlap:
        raise ValueError("Generated holdout overlaps development utterances.")
    return languages


async def prepare_holdout(output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite sealed holdout: {output}")
    development = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    definitions = {
        "single_vague": (
            "accepted",
            ("One active proposal. The user accepts it with a short dependent or deictic reply "
            "without independently restating the proposed memory."),
        ),
        "single_indirect": (
            "accepted",
            ("One active proposal. The user indirectly confirms that the proposed behavior fits "
            "them or should continue, without independently restating its factual content."),
        ),
        "explicit_ordinal": (
            "accepted",
            ("Two active proposals. The user clearly selects proposal 1 or 2 using an ordinal, "
            "number, former/latter, or equivalent natural reference."),
        ),
        "ambiguous_multi": (
            "pending",
            ("Two active proposals. The user sounds accepting but does not identify which proposal, "
            "so binding must remain ambiguous."),
        ),
        "rejection": (
            "rejected",
            ("One active proposal. The user refuses, corrects, questions, or limits agreement to "
            "the current session rather than consenting to durable memory."),
        ),
    }
    service = LLMService()
    groups: dict[str, Any] = {}
    provider = model = ""
    for name, (outcome, definition) in definitions.items():
        explicit = name == "explicit_ordinal"
        row_shape = "[language,text,target_ordinal]" if explicit else "[language,text]"
        prompt = f'''Create exactly 10 unseen utterances for this evaluation slice: {name}.
Definition: {definition}
Return JSON only as {{"utterances":[10 rows]}} where every row is {row_shape}.
Use exactly 3 rows labeled en, 3 rows labeled hinglish, and 4 rows labeled hi.
Hindi must use Devanagari. Hinglish must use Latin script. Use natural varied wording.
Do not use these common phrases: yes remember that; the second one; do not remember that.
Do not include secrets or explanations.'''
        response = await service.complete(
            system_prompt=(
                "You create blind evaluation data, not production decisions. "
                "Follow the requested JSON schema and exact counts."
            ),
            user_message=prompt,
            temperature=0.8,
            max_tokens=1800,
            response_format="json",
        )
        generated = json.loads(response.content)
        groups[name] = {
            "expected_outcome": outcome,
            "utterances": generated.get("utterances") or [],
        }
        provider = response.provider_used
        model = response.model_used
    payload = {
        "schema_version": "1.0",
        "split": "holdout",
        "minimum_cases_per_language": 15,
        "minimum_cases_per_reference_type": 10,
        "reference_types": groups,
    }
    language_counts = _validate(payload, development)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized, encoding="utf-8", newline="\n")
    return {
        "output": str(output),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "case_count": 50,
        "language_counts": language_counts,
        "generator_provider": provider,
        "generator_model": model,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a sealed Phase 3A holdout.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(prepare_holdout(args.output)), indent=2))


if __name__ == "__main__":
    main()
