from __future__ import annotations

import argparse
import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from api.services.extractor import ExtractionService


SPEC_PATH = Path("docs/extraction_spec.md")
PROMPT_PATH = Path("api/services/prompts/extraction_prompt.txt")
REPORT_PATH = Path(os.getenv("EVAL_REPORT_PATH", "docs/extraction_evaluation_report.md"))
MODEL_INPUT_COST_PER_1M = 0.10
MODEL_OUTPUT_COST_PER_1M = 0.40
PASS_THRESHOLD = 88.0
REQUEST_DELAY_SECONDS = float(os.getenv("EVAL_REQUEST_DELAY_SECONDS", "7"))
TRANSIENT_RETRY_DELAY_SECONDS = float(os.getenv("EVAL_TRANSIENT_RETRY_DELAY_SECONDS", "15"))
MAX_EXAMPLES = int(os.getenv("EVAL_MAX_EXAMPLES", "20"))


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_conversation(block: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for raw_line in block.strip().splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        speaker, content = line.split(":", 1)
        role = "assistant" if speaker.strip().lower() == "ai" else "user"
        messages.append({"role": role, "content": content.strip()})
    return messages


def parse_examples() -> list[dict[str, Any]]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    examples: list[dict[str, Any]] = []

    for match in re.finditer(
        r"### Example (\d+) .*?\n(.*?)(?=\n### Example \d+ |\n## 4\. What Should NEVER Be Stored)",
        text,
        re.S,
    ):
        example_number = int(match.group(1))
        section = match.group(2)
        conversation_match = re.search(r"\*\*Conversation.*?\*\*\s*```(.*?)```", section, re.S)
        if conversation_match is None:
            continue

        should_extract_match = re.search(
            r"\*\*SHOULD extract(?: / UPDATE)?:\*\*(.*?)(?:\n\*\*Should NOT extract:|\n\*\*Conflict resolution|\n\*\*Note on scoring:|\n\*\*Note:|\n---)",
            section,
            re.S,
        )
        if should_extract_match is None:
            continue

        expected_memories: list[dict[str, Any]] = []
        should_extract_block = should_extract_match.group(1).strip()

        if "NOTHING" not in should_extract_block.upper():
            for row in should_extract_block.splitlines():
                line = row.strip()
                if not line.startswith("|") or "--------" in line or "Memory |" in line:
                    continue
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) < 4:
                    continue
                expected_memories.append(
                    {
                        "content": cells[0].strip('"'),
                        "category": cells[1].lower(),
                        "importance_score": float(cells[2]),
                    }
                )

        examples.append(
            {
                "number": example_number,
                "messages": parse_conversation(conversation_match.group(1)),
                "expected_memories": expected_memories,
            }
        )

    return examples


def parse_example_selection(raw_value: str | None) -> set[int] | None:
    if raw_value is None:
        return None

    selected: set[int] = set()
    for item in raw_value.split(","):
        value = item.strip()
        if not value:
            continue
        selected.add(int(value))
    return selected


def normalize_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return " ".join(cleaned.split())


def similarity(a: str, b: str) -> float:
    normalized_a = normalize_text(a)
    normalized_b = normalize_text(b)
    if not normalized_a or not normalized_b:
        return 0.0

    token_overlap = 0.0
    tokens_a = set(normalized_a.split())
    tokens_b = set(normalized_b.split())
    if tokens_a and tokens_b:
        token_overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))

    sequence_ratio = SequenceMatcher(None, normalized_a, normalized_b).ratio()
    return max(token_overlap, sequence_ratio)


def match_expected_to_predicted(
    expected_memories: list[dict[str, Any]],
    predicted_memories: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    matched = 0
    unmatched_predicted = predicted_memories.copy()
    missing_expected: list[dict[str, Any]] = []
    matched_expected: list[dict[str, Any]] = []

    for expected in expected_memories:
        best_index: int | None = None
        best_score = 0.0

        for index, predicted in enumerate(unmatched_predicted):
            if predicted["category"] != expected["category"]:
                continue
            score = similarity(expected["content"], predicted["content"])
            if score > best_score:
                best_score = score
                best_index = index

        if best_index is not None and best_score >= 0.55:
            matched += 1
            matched_expected.append(expected)
            unmatched_predicted.pop(best_index)
        else:
            missing_expected.append(expected)

    return matched, matched_expected, missing_expected, unmatched_predicted


def calculate_cost(usage_events: list[dict[str, Any]]) -> float:
    prompt_tokens = sum(event.get("prompt_tokens") or 0 for event in usage_events)
    completion_tokens = sum(event.get("completion_tokens") or 0 for event in usage_events)
    return (
        (prompt_tokens / 1_000_000) * MODEL_INPUT_COST_PER_1M
        + (completion_tokens / 1_000_000) * MODEL_OUTPUT_COST_PER_1M
    )


class AlwaysInvalidGeminiClient:
    class Models:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return type(
                "Response",
                (),
                {
                    "text": "{invalid json",
                    "usage_metadata": type(
                        "Usage",
                        (),
                        {
                            "prompt_token_count": 10,
                            "candidates_token_count": 5,
                            "total_token_count": 15,
                        },
                    )(),
                },
            )()

    def __init__(self) -> None:
        self.models = self.Models()


def format_json_block(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def write_report(
    *,
    processed_examples: int,
    total_examples: int,
    overall_accuracy: float,
    category_totals: dict[str, int],
    category_matches: dict[str, int],
    seen_categories: set[str],
    importance_scores: list[float],
    costs: list[float],
    retry_calls: int,
    retry_passed: bool,
    example_results: list[dict[str, Any]],
    quota_exhausted: bool,
) -> None:
    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    average_cost = (sum(costs) / len(costs)) if costs else 0.0
    max_cost = max(costs) if costs else 0.0

    lines: list[str] = [
        "# Extraction Evaluation Report",
        "",
        f"- Processed examples: {processed_examples}/{total_examples}",
        f"- Overall accuracy: {overall_accuracy:.2f}%",
        f"- All 6 categories seen: {len(seen_categories) == 6}",
        f"- Importance scores are spread: {len(set(importance_scores)) > 1}",
        f"- Average cost per conversation: ${average_cost:.6f}",
        f"- Max cost per conversation: ${max_cost:.6f}",
        f"- Retry logic passed: {retry_passed} (calls={retry_calls})",
        f"- Quota exhausted during run: {quota_exhausted}",
        "",
        "## Checklist",
        "",
        f"- overall accuracy >= 88%: {overall_accuracy >= PASS_THRESHOLD}",
        f"- all 6 categories appear: {len(seen_categories) == 6}",
        f"- importance scores are spread: {len(set(importance_scores)) > 1}",
        f"- average token cost under $0.002: {average_cost < 0.002}",
        f"- retry logic 3 attempts then graceful failure: {retry_passed}",
        "",
        "## Accuracy Per Category",
        "",
    ]

    for category in sorted(category_totals):
        matched = category_matches.get(category, 0)
        total = category_totals[category]
        accuracy = matched / total * 100 if total else 0.0
        lines.append(f"- {category}: {accuracy:.2f}% ({matched}/{total})")

    lines.extend(
        [
            "",
            "## Prompt Used",
            "",
            "```text",
            prompt_text.rstrip(),
            "```",
            "",
            "## Example Outcomes",
            "",
        ]
    )

    for result in example_results:
        lines.extend(
            [
                f"### Example {result['number']}",
                "",
                f"- Cost: ${result['cost']:.6f}",
                f"- Matched expected memories: {result['matched']}/{result['expected_count']}",
                f"- Missing expected memories: {len(result['missing'])}",
                f"- Extra predicted memories: {len(result['extra'])}",
                "",
                "**Conversation**",
                "",
                "```json",
                format_json_block(result["messages"]),
                "```",
                "",
                "**Expected memories**",
                "",
                "```json",
                format_json_block(result["expected"]),
                "```",
                "",
                "**Predicted memories**",
                "",
                "```json",
                format_json_block(result["predicted"]),
                "```",
                "",
                "**Missing expected**",
                "",
                "```json",
                format_json_block(result["missing"]),
                "```",
                "",
                "**Extra predicted**",
                "",
                "```json",
                format_json_block(result["extra"]),
                "```",
                "",
                "**Usage events**",
                "",
                "```json",
                format_json_block(result["usage_events"]),
                "```",
                "",
            ]
        )

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate extraction accuracy against the spec examples.")
    parser.add_argument(
        "--examples",
        help="Comma-separated example numbers to run, e.g. 1,19,20",
    )
    args = parser.parse_args()

    load_env()

    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env or export it before running.")

    selected_examples = parse_example_selection(args.examples)
    all_examples = parse_examples()
    if selected_examples is not None:
        all_examples = [example for example in all_examples if example["number"] in selected_examples]
    examples = all_examples[:MAX_EXAMPLES]
    extractor = ExtractionService()

    total_expected = 0
    total_matched = 0
    category_totals: dict[str, int] = {}
    category_matches: dict[str, int] = {}
    seen_categories: set[str] = set()
    importance_scores: list[float] = []
    costs: list[float] = []
    failures: list[dict[str, Any]] = []
    example_results: list[dict[str, Any]] = []
    processed_examples = 0
    quota_exhausted = False

    print(f"Running extraction evaluation on {len(examples)} spec examples...\n")

    for index, example in enumerate(examples, start=1):
        extracted: list[Any] | None = None
        last_error: Exception | None = None

        for _attempt in range(3):
            try:
                extracted = extractor.extract(
                    example["messages"], user_id=f"eval-user-{example['number']}"
                )
                last_error = None
                break
            except Exception as error:
                last_error = error
                error_text = str(error)
                if "RESOURCE_EXHAUSTED" in error_text or "UNAVAILABLE" in error_text or "429" in error_text or "503" in error_text:
                    if "RESOURCE_EXHAUSTED" in error_text and "PerDay" in error_text:
                        print("\nStopping early because the Gemini daily quota was exhausted.")
                        quota_exhausted = True
                        break
                    time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                raise

        if extracted is None and last_error is not None:
            break

        predicted = [memory.model_dump(mode="json") for memory in extracted]
        processed_examples += 1
        matched, matched_expected, missing, extra = match_expected_to_predicted(
            example["expected_memories"], predicted
        )

        total_expected += len(example["expected_memories"])
        total_matched += matched
        for expected in example["expected_memories"]:
            category = expected["category"]
            category_totals[category] = category_totals.get(category, 0) + 1
        for expected in matched_expected:
            category = expected["category"]
            category_matches[category] = category_matches.get(category, 0) + 1

        seen_categories.update(memory["category"] for memory in predicted)
        importance_scores.extend(memory["importance_score"] for memory in predicted)

        cost = calculate_cost(extractor.last_usage_events)
        costs.append(cost)
        example_results.append(
            {
                "number": example["number"],
                "messages": example["messages"],
                "expected": example["expected_memories"],
                "expected_count": len(example["expected_memories"]),
                "predicted": predicted,
                "matched": matched,
                "missing": missing,
                "extra": extra,
                "cost": cost,
                "usage_events": extractor.last_usage_events,
            }
        )

        if missing or extra:
            failures.append(
                {
                    "example": example["number"],
                    "missing": missing,
                    "extra": extra,
                    "predicted": predicted,
                }
            )

        if index < len(examples):
            time.sleep(REQUEST_DELAY_SECONDS)

    overall_accuracy = (total_matched / total_expected * 100) if total_expected else 0.0
    average_cost = (sum(costs) / len(costs)) if costs else 0.0

    print(f"Processed examples: {processed_examples}/{len(examples)}")
    print(f"Overall accuracy: {overall_accuracy:.2f}%")
    print("\nAccuracy per category:")
    for category in sorted(category_totals):
        matched = category_matches.get(category, 0)
        total = category_totals[category]
        accuracy = matched / total * 100 if total else 0.0
        print(f"- {category}: {accuracy:.2f}% ({matched}/{total})")

    print(f"\nCategories seen in predictions: {sorted(seen_categories)}")
    print(f"Importance scores observed: {sorted(set(importance_scores))}")
    print(f"Average token cost per conversation: ${average_cost:.6f}")
    print(f"Max token cost per conversation: ${max(costs) if costs else 0.0:.6f}")

    retry_client = AlwaysInvalidGeminiClient()
    retry_service = ExtractionService(client=retry_client)
    retry_result = retry_service.extract(
        messages=[{"role": "user", "content": "I prefer concise responses."}],
        user_id="retry-test-user",
    )
    print(
        f"Retry logic check: calls={retry_client.models.calls}, graceful_failure={retry_result == []}"
    )
    retry_passed = retry_client.models.calls == 3 and retry_result == []

    print("\nChecklist:")
    print(f"- overall accuracy >= 88%: {overall_accuracy >= PASS_THRESHOLD}")
    print(f"- all 6 categories appear: {len(seen_categories) == 6}")
    print(f"- importance scores are spread: {len(set(importance_scores)) > 1}")
    print(f"- average token cost under $0.002: {average_cost < 0.002}")
    print(f"- retry logic 3 attempts then graceful failure: {retry_passed}")

    if failures:
        print("\nFailures to inspect:")
        for failure in failures[:10]:
            print(json.dumps(failure, indent=2))

    write_report(
        processed_examples=processed_examples,
        total_examples=len(examples),
        overall_accuracy=overall_accuracy,
        category_totals=category_totals,
        category_matches=category_matches,
        seen_categories=seen_categories,
        importance_scores=importance_scores,
        costs=costs,
        retry_calls=retry_client.models.calls,
        retry_passed=retry_passed,
        example_results=example_results,
        quota_exhausted=quota_exhausted,
    )
    print(f"\nWrote detailed report to {REPORT_PATH}")


if __name__ == "__main__":
    main()
