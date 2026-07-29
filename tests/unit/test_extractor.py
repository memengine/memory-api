from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import types

from api.schemas.memory_schemas import ExtractionResponseSchema
from api.services.extractor import ExtractionService
from api.settings import get_settings


SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "extraction_spec.md"


def _make_completion(content: str, prompt_tokens: int = 100, completion_tokens: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        text=content,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=completion_tokens,
            total_token_count=prompt_tokens + completion_tokens,
        ),
    )


def _parse_conversation(block: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for raw_line in block.strip().splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        speaker, content = line.split(":", 1)
        role = "assistant" if speaker.strip().lower() in {"ai", "assistant"} else "user"
        messages.append({"role": role, "content": content.strip()})
    return messages


_CONVERSATION_HEADING_RE = r"(?:Conversation|Input|Transcript|Dialog)[^*\n]*"
_SHOULD_EXTRACT_HEADING_RE = r"^\*\*SHOULD extract(?: / UPDATE)?[^\n]*\*\*"


def _strip_markdown_fence(block: str) -> str:
    stripped = block.strip()
    fence_match = re.match(r"^```[^\n]*\r?\n?(.*?)\r?\n?```$", stripped, re.S)
    return fence_match.group(1) if fence_match is not None else block


def _contains_conversation_lines(block: str) -> bool:
    allowed_speakers = {"user", "ai", "assistant"}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        speaker, _ = line.split(":", 1)
        if speaker.strip().lower() in allowed_speakers:
            return True
    return False


def _find_conversation_block(section: str, example_number: str | None = None) -> str:
    fenced_match = re.search(
        rf"^\*\*{_CONVERSATION_HEADING_RE}\*\*\s*\r?\n```[^\n]*\r?\n(.*?)\r?\n```",
        section,
        re.S | re.M | re.I,
    )
    if fenced_match is not None:
        return fenced_match.group(1)

    inline_fenced_match = re.search(
        rf"\*\*{_CONVERSATION_HEADING_RE}\*\*\s*```[^\n]*\r?\n?(.*?)```",
        section,
        re.S | re.I,
    )
    if inline_fenced_match is not None:
        return inline_fenced_match.group(1)

    plain_match = re.search(
        rf"^\*\*{_CONVERSATION_HEADING_RE}\*\*\s*(.*?)(?={_SHOULD_EXTRACT_HEADING_RE})",
        section,
        re.S | re.M | re.I,
    )
    if plain_match is not None:
        return _strip_markdown_fence(plain_match.group(1))

    fallback_match = re.search(
        rf"\A(.*?)(?={_SHOULD_EXTRACT_HEADING_RE})",
        section,
        re.S | re.M | re.I,
    )
    if fallback_match is not None:
        fallback = re.sub(
            rf"^\*\*{_CONVERSATION_HEADING_RE}\*\*\s*",
            "",
            fallback_match.group(1),
            flags=re.M | re.I,
        )
        fallback = _strip_markdown_fence(fallback)
        if _contains_conversation_lines(fallback):
            return fallback

    preview = " ".join(section.strip().split())[:160]
    raise AssertionError(
        f"Example {example_number or '?'} is missing a Conversation block. Preview: {preview}"
    )


def _find_should_extract_block(section: str) -> str:
    match = re.search(
        r"^\*\*SHOULD extract(?: / UPDATE)?[^\n]*\*\*\s*(.*?)(?=^\*\*Should NOT extract:|^\*\*Conflict resolution|^\*\*Note on scoring:|^\*\*Note:|^---|\Z)",
        section,
        re.S | re.M,
    )
    if match is None:
        raise AssertionError("Example is missing a SHOULD extract block")
    return match.group(1).strip()


def _parse_examples() -> list[dict[str, object]]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    examples: list[dict[str, object]] = []
    matches = re.finditer(
        r"^### Example\s+(\d+)\b[^\n]*\n(.*?)(?=^### Example\s+\d+\b|^##\s+4\.\s+What Should NEVER Be Stored|\Z)",
        text,
        re.S | re.M,
    )

    for match in matches:
        example_number = match.group(1)
        section = match.group(2)
        conversation = _parse_conversation(_find_conversation_block(section, example_number))

        should_extract_block = _find_should_extract_block(section)

        expected_memories: list[dict[str, object]] = []
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
                        "confidence": 0.9,
                        "expiry": "permanent",
                        "reasoning": cells[3],
                    }
                )

        examples.append(
            {
                "number": int(example_number),
                "messages": conversation,
                "expected_memories": expected_memories,
            }
        )

    return examples


SPEC_EXAMPLES = _parse_examples()


def test_extraction_service_uses_extraction_model_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTION_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()
    try:
        service = ExtractionService(client=MagicMock())
        assert service.model == "gemini-2.0-flash"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("example", SPEC_EXAMPLES, ids=lambda item: f"example_{item['number']}")
def test_extract_matches_spec_examples(example) -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps({"memories": example["expected_memories"]})
    )
    service = ExtractionService(client=client)

    extracted = service.extract(example["messages"], user_id="user-123")

    assert [memory.content for memory in extracted] == [
        item["content"] for item in example["expected_memories"]
    ]
    assert [memory.category for memory in extracted] == [
        item["category"] for item in example["expected_memories"]
    ]
    assert [memory.importance_score for memory in extracted] == [
        item["importance_score"] for item in example["expected_memories"]
    ]


def test_spec_parser_finds_all_20_examples() -> None:
    assert len(SPEC_EXAMPLES) == 20


def test_extract_retries_on_json_parse_failure_with_feedback() -> None:
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _make_completion("{invalid json"),
        _make_completion(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "User prefers concise answers",
                            "category": "preference",
                            "importance_score": 7,
                            "confidence": 0.92,
                            "expiry": "permanent",
                            "reasoning": "Directly stated preference",
                        }
                    ]
                }
            )
        ),
    ]
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I prefer concise answers."}],
        user_id="user-123",
    )

    assert len(extracted) == 1
    assert client.models.generate_content.call_count == 2
    retry_prompt = client.models.generate_content.call_args_list[1].kwargs["contents"]
    assert "could not be parsed" in retry_prompt


def test_extract_filters_out_low_confidence_memories() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers prose",
                        "category": "preference",
                        "importance_score": 8,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Direct preference",
                    },
                    {
                        "content": "User might know Kubernetes",
                        "category": "expertise",
                        "importance_score": 3,
                        "confidence": 0.4,
                        "expiry": "permanent",
                        "reasoning": "Weak signal",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I prefer prose responses."}],
        user_id="user-123",
    )

    assert len(extracted) == 1
    assert extracted[0].content == "User prefers prose"


def test_extract_filters_metadata_artifacts_from_model_output() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User ID: eval-user-4",
                        "category": "fact",
                        "importance_score": 7,
                        "confidence": 0.99,
                        "expiry": "permanent",
                        "reasoning": "Metadata leak",
                    },
                    {
                        "content": "User prefers prose responses",
                        "category": "preference",
                        "importance_score": 8,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Directly stated",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I prefer prose responses."}],
        user_id="user-123",
    )

    assert len(extracted) == 1
    assert extracted[0].content == "User prefers prose responses"


def test_extract_adds_heuristic_memory_for_investor_conversations() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(json.dumps({"memories": []}))
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[
            {
                "role": "user",
                "content": "I have a product demo with a potential investor next Tuesday. Need to prepare a 10-minute pitch.",
            }
        ],
        user_id="user-123",
    )

    assert any(
        memory.content == "User is in active investor conversations"
        and memory.category == "goal"
        for memory in extracted
    )


def test_extract_adds_heuristic_memory_for_advanced_sqlalchemy_signals() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(json.dumps({"memories": []}))
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[
            {
                "role": "user",
                "content": (
                    "I'm getting N+1 query issues with my SQLAlchemy relationships. "
                    "I thought using joinedload would fix it. "
                    "I know about lazy vs eager loading, but I'm struggling with "
                    "the subquery load strategy for many-to-many relationships."
                ),
            }
        ],
        user_id="user-123",
    )

    contents = {memory.content for memory in extracted}
    assert "User has intermediate-to-advanced SQLAlchemy expertise including relationship loading strategies" in contents
    assert "User understands N+1 query problems and ORM loading patterns" in contents


def test_extract_chunks_large_conversations_and_logs_usage(caplog) -> None:
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _make_completion(json.dumps({"memories": []}), prompt_tokens=150, completion_tokens=30),
        _make_completion(json.dumps({"memories": []}), prompt_tokens=140, completion_tokens=20),
    ]
    service = ExtractionService(client=client)
    long_message = "x" * 9000

    with caplog.at_level("INFO"):
        extracted = service.extract(
            messages=[
                {"role": "user", "content": long_message},
                {"role": "assistant", "content": long_message},
            ],
            user_id="user-123",
        )

    assert extracted == []
    assert client.models.generate_content.call_count >= 2
    assert "extraction_usage" in caplog.text


def test_gemini_generate_content_config_is_used() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(json.dumps({"memories": []}))
    service = ExtractionService(client=client)

    service.extract(
        messages=[{"role": "user", "content": "We use FastAPI and PostgreSQL."}],
        user_id="user-123",
    )

    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == service.model
    assert isinstance(kwargs["config"], types.GenerateContentConfig)
    assert kwargs["config"].response_mime_type == "application/json"


def test_extract_gracefully_fails_after_three_invalid_json_attempts() -> None:
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _make_completion("{invalid json"),
        _make_completion("{invalid json"),
        _make_completion("{invalid json"),
    ]
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I prefer concise answers."}],
        user_id="user-123",
    )

    assert extracted == []
    assert client.models.generate_content.call_count == 3


def test_postprocess_recategorizes_named_people_as_relationships() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User's co-founder Priya handles design and frontend.",
                        "category": "fact",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Named teammate role.",
                    }
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "My co-founder Priya handles all the design and frontend."}],
        user_id="user-123",
    )

    assert len(extracted) == 1
    assert extracted[0].category == "relationship"


def test_postprocess_drops_temporary_troubleshooting_memories() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User is experiencing N+1 query issues with SQLAlchemy relationships.",
                        "category": "fact",
                        "importance_score": 4,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Current problem.",
                    },
                    {
                        "content": "User understands N+1 query problems and ORM loading patterns",
                        "category": "expertise",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Durable expertise signal.",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I know about lazy vs eager loading and N+1 problems."}],
        user_id="user-123",
    )

    assert [memory.content for memory in extracted] == [
        "User understands N+1 query problems and ORM loading patterns"
    ]


def test_postprocess_drops_beginner_fact_when_learning_goal_exists() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User is learning Kubernetes.",
                        "category": "goal",
                        "importance_score": 4,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Learning goal.",
                    },
                    {
                        "content": "User is a beginner in Kubernetes.",
                        "category": "fact",
                        "importance_score": 5,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Beginner status.",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I just started learning Kubernetes. Still very much a beginner."}],
        user_id="user-123",
    )

    assert [memory.content for memory in extracted] == ["User is learning Kubernetes."]


def test_postprocess_drops_tentative_self_improvement_memory() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User is considering trying time-blocking to improve focus.",
                        "category": "goal",
                        "importance_score": 4,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Tentative exploration.",
                    },
                    {
                        "content": "User works best in 90-minute focused blocks.",
                        "category": "preference",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Durable work preference.",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "Maybe I should try time-blocking. I work best in 90-minute focused blocks."}],
        user_id="user-123",
    )

    assert [memory.content for memory in extracted] == ["User works best in 90-minute focused blocks."]


def test_postprocess_drops_one_time_situational_context() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User is in a hurry today.",
                        "category": "fact",
                        "importance_score": 1,
                        "confidence": 0.95,
                        "expiry": "temporary",
                        "reasoning": "Current urgency.",
                    }
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I'm in a hurry today, I have a meeting in 5 minutes."}],
        user_id="user-123",
    )

    assert extracted == []


def test_postprocess_recategorizes_completed_customer_discovery_as_fact() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User has conducted 50 customer discovery interviews with shop owners in Tier 2 Indian cities to understand their operational pain points.",
                        "category": "procedure",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Research work.",
                    }
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "We've done 50 customer discovery interviews with shop owners across Tier 2 cities."}],
        user_id="user-123",
    )

    assert len(extracted) == 1
    assert extracted[0].category == "fact"


def test_postprocess_drops_generic_improve_focus_goal() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _make_completion(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User is trying to improve focus.",
                        "category": "goal",
                        "importance_score": 4,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Generic improvement goal.",
                    },
                    {
                        "content": "User works best in 90-minute focused blocks.",
                        "category": "preference",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "expiry": "permanent",
                        "reasoning": "Durable preference.",
                    },
                ]
            }
        )
    )
    service = ExtractionService(client=client)

    extracted = service.extract(
        messages=[{"role": "user", "content": "I've been struggling to focus lately. I work best in 90-minute focused blocks."}],
        user_id="user-123",
    )

    assert [memory.content for memory in extracted] == ["User works best in 90-minute focused blocks."]


def test_prompt_file_contains_required_sections() -> None:
    prompt_text = Path("api/services/prompts/extraction_prompt.txt").read_text(encoding="utf-8")

    assert "MEMORY CATEGORIES" in prompt_text
    assert "SCORING RUBRIC" in prompt_text
    assert "NEGATIVE EXAMPLES" in prompt_text
    assert "JSON OUTPUT SCHEMA" in prompt_text
    assert "importance_score range is 1.0 to 10.0" in prompt_text
    assert "score that preference 8 or 9" in prompt_text
    assert 'UPDATES PREVIOUS: [description]' in prompt_text
    assert 'reduce confidence by 0.1' in prompt_text
    assert "never store metadata, user IDs, chunk numbers" in prompt_text
    assert 'store "User is in active investor conversations"' in prompt_text
    assert "advanced troubleshooting" in prompt_text


def test_build_user_prompt_excludes_user_metadata() -> None:
    prompt = ExtractionService._build_user_prompt(
        conversation_text="User: I use FastAPI.\nAssistant: Nice.",
        retry_feedback=None,
    )

    assert "User ID:" not in prompt
    assert "Conversation chunk:" not in prompt
    assert "Do not extract anything from metadata" in prompt


def test_extraction_response_schema_enforces_score_range() -> None:
    with pytest.raises(Exception):
        ExtractionResponseSchema.model_validate(
            {
                "memories": [
                    {
                        "content": "User prefers prose",
                        "category": "preference",
                        "importance_score": 0.5,
                        "confidence": 0.9,
                        "expiry": "permanent",
                        "reasoning": "Strong preference",
                    }
                ]
            }
        )
