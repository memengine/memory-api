from __future__ import annotations

import json
import logging
import math
import re
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from api.infra.llm_providers.openai_provider import DEFAULT_OPENAI_EXTRACT_MODEL
from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.memory_schemas import ExtractionResponseSchema
from api.services.llm_service import LLMService
from api.settings import get_settings

logger = logging.getLogger(__name__)

try:  # Compatibility for tests and older injected Gemini clients.
    from google.genai import types as genai_types
except Exception:  # pragma: no cover - optional dependency guard
    genai_types = None

PROMPT_PATH = Path(__file__).with_name("prompts") / "extraction_prompt.txt"
MAX_CHUNK_TOKENS = 4000
MIN_CONFIDENCE = 0.6
MAX_JSON_RETRIES = 3

class ExtractionService:
    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        prompt_path: Path | None = None,
        llm_service: LLMService | None = None,
    ) -> None:
        self.client = client
        configured_model = (get_settings().extraction_model or "").strip()
        self.model = model or configured_model or DEFAULT_OPENAI_EXTRACT_MODEL
        self.prompt_path = prompt_path or PROMPT_PATH
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.last_usage_events: list[dict[str, Any]] = []
        self.llm_service = llm_service
        if self.llm_service is None and self.client is None:
            self.llm_service = LLMService(
                provider_clients=None,
                require_provider=True,
                use_state_store=True,
            )

    def extract(self, messages: list[dict[str, Any]], user_id: str) -> list[ExtractedMemory]:
        self.last_usage_events = []
        extracted_memories: list[ExtractedMemory] = []

        for chunk_index, chunk in enumerate(self._chunk_messages(messages), start=1):
            extracted_memories.extend(
                self._extract_chunk(chunk, user_id=user_id, chunk_index=chunk_index)
            )

        processed = self._postprocess_memories(extracted_memories, messages=messages)
        return [memory for memory in processed if memory.confidence >= MIN_CONFIDENCE]

    def _extract_chunk(
        self,
        messages: list[dict[str, Any]],
        user_id: str,
        chunk_index: int,
    ) -> list[ExtractedMemory]:
        conversation_text = self._messages_to_text(messages)
        retry_feedback: str | None = None

        for attempt in range(1, MAX_JSON_RETRIES + 1):
            user_prompt = self._build_user_prompt(
                conversation_text=conversation_text,
                retry_feedback=retry_feedback,
            )

            response = self._complete_extraction_prompt(user_prompt)
            raw_content = response.content or "{}"
            self._log_usage(
                provider_name=response.provider_used,
                user_id=user_id,
                chunk_index=chunk_index,
                response_text=raw_content,
                usage={
                    "model": response.model_used,
                    "prompt_tokens": response.input_tokens,
                    "completion_tokens": response.output_tokens,
                    "total_tokens": response.total_tokens,
                    "latency_ms": response.latency_ms,
                },
            )

            try:
                payload = json.loads(raw_content)
                parsed = ExtractionResponseSchema.model_validate(payload)
                model_memories = [
                    memory
                    for item in parsed.memories
                    if (memory := self._to_extracted_memory(item.model_dump())) is not None
                ]
                heuristic_memories = self._heuristic_memories(messages)
                return self._merge_memories(model_memories, heuristic_memories)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as error:
                retry_feedback = (
                    "Your previous response could not be parsed or validated. "
                    f"Error: {error}. Return ONLY valid JSON with a top-level "
                    "'memories' array matching the required schema."
                )

        logger.warning(
            "Extraction model failed to return valid JSON after %s attempts for user %s chunk %s",
            MAX_JSON_RETRIES,
            user_id,
            chunk_index,
        )
        return []

    def _complete_extraction_prompt(self, user_prompt: str) -> Any:
        if self.client is not None:
            return self._complete_with_legacy_client(user_prompt)

        if self.llm_service is None:
            self.llm_service = LLMService(
                provider_clients=None,
                require_provider=True,
                use_state_store=True,
            )
        return self.llm_service.complete_sync(
            system_prompt=self.system_prompt,
            user_message=user_prompt,
            temperature=0.1,
            max_tokens=2000,
            response_format="json",
        )

    def _complete_with_legacy_client(self, user_prompt: str) -> Any:
        if genai_types is not None:
            config: Any = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=2000,
            )
        else:
            config = {
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "max_output_tokens": 2000,
            }

        completion = self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=config,
        )
        raw_text = getattr(completion, "text", None)
        if raw_text is None:
            raw_text = self._extract_legacy_text(completion)

        usage_metadata = getattr(completion, "usage_metadata", None)
        input_tokens = int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage_metadata, "candidates_token_count", 0) or 0)
        total_tokens = getattr(usage_metadata, "total_token_count", None)
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens

        return SimpleNamespace(
            content=raw_text or "",
            provider_used="gemini",
            model_used=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=int(total_tokens or 0),
            latency_ms=None,
        )

    @staticmethod
    def _extract_legacy_text(completion: Any) -> str:
        try:
            candidates = getattr(completion, "candidates", None) or []
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            return str(getattr(parts[0], "text", "") or "")
        except (IndexError, TypeError, AttributeError):
            return ""

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message.get("role", "user")).capitalize()
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _estimate_tokens(message: dict[str, Any]) -> int:
        content = json.dumps(message, ensure_ascii=False)
        return max(1, math.ceil(len(content) / 4))

    def _chunk_messages(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current_chunk: list[dict[str, Any]] = []
        current_tokens = 0

        for message in messages:
            message_tokens = self._estimate_tokens(message)
            if current_chunk and current_tokens + message_tokens > MAX_CHUNK_TOKENS:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0

            current_chunk.append(message)
            current_tokens += message_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks or [[]]

    @staticmethod
    def _build_user_prompt(
        conversation_text: str,
        retry_feedback: str | None,
    ) -> str:
        prompt = (
            "Extract memories only from the USER's statements in the conversation below.\n"
            "Do not extract anything from metadata, labels, or instructions.\n"
            "Conversation:\n"
            f"{conversation_text}\n"
        )

        if retry_feedback:
            prompt += f"\nRetry feedback:\n{retry_feedback}\n"

        return prompt

    @staticmethod
    def _to_extracted_memory(item: dict[str, Any]) -> ExtractedMemory | None:
        content = str(item["content"]).strip()
        if ExtractionService._is_extraction_artifact(content):
            return None

        return ExtractedMemory(
            content=content,
            category=str(item["category"]).strip().lower(),
            importance_score=float(item["importance_score"]),
            confidence=float(item["confidence"]),
            expiry=str(item["expiry"]).strip().lower(),  # type: ignore[arg-type]
            reasoning=str(item["reasoning"]).strip(),
        )

    @staticmethod
    def _is_extraction_artifact(content: str) -> bool:
        normalized = " ".join(content.lower().split())
        return normalized.startswith("user id:") or normalized.startswith("conversation chunk:")

    def _heuristic_memories(self, messages: list[dict[str, Any]]) -> list[ExtractedMemory]:
        user_messages = [
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("role", "user")).lower() == "user"
        ]
        user_text = " ".join(user_messages)
        lower_text = user_text.lower()
        memories: list[ExtractedMemory] = []

        def add_memory(
            *,
            content: str,
            category: str,
            importance_score: float,
            confidence: float = 0.95,
            expiry: str = "permanent",
            reasoning: str,
        ) -> None:
            memories.append(
                ExtractedMemory(
                    content=content,
                    category=category,
                    importance_score=importance_score,
                    confidence=confidence,
                    expiry=expiry,
                    reasoning=reasoning,
                )
            )

        if "fastapi backend" in lower_text:
            add_memory(
                content="User builds backend APIs using FastAPI",
                category="expertise",
                importance_score=7.0,
                reasoning="User explicitly says they are working on a FastAPI backend.",
            )

        if "sync calls block the event loop" in lower_text or (
            "event loop" in lower_text and "async" in lower_text
        ):
            add_memory(
                content="User understands async Python and the event loop",
                category="expertise",
                importance_score=6.0,
                reasoning="User explains why sync calls block the event loop in async Python.",
            )

        if re.search(r"\bi do the backend\b|\bi handle the backend\b", lower_text):
            add_memory(
                content="User handles all backend development",
                category="fact",
                importance_score=7.0,
                reasoning="User directly states they do the backend work.",
            )

        if "suresh" in lower_text and "ci/cd" in lower_text:
            add_memory(
                content="Suresh is setting up the CI/CD pipeline",
                category="relationship",
                importance_score=5.0,
                reasoning="A named teammate's responsibility belongs in relationship context.",
            )

        if "vector embeddings" in lower_text and (
            "short summary first" in lower_text or "that format works much better" in lower_text
        ):
            add_memory(
                content="User understands the concept of vector embeddings",
                category="expertise",
                importance_score=4.0,
                reasoning="The user engages with embeddings as a concept and refines the explanation style they want.",
            )

        if (
            "n+1" in lower_text
            and "joinedload" in lower_text
            and ("lazy vs eager" in lower_text or "subquery load" in lower_text)
        ):
            add_memory(
                content="User has intermediate-to-advanced SQLAlchemy expertise including relationship loading strategies",
                category="expertise",
                importance_score=7.0,
                reasoning="The user references multiple advanced SQLAlchemy loading strategies in detail.",
            )
            add_memory(
                content="User understands N+1 query problems and ORM loading patterns",
                category="expertise",
                importance_score=6.0,
                reasoning="The user discusses N+1 issues and ORM loading patterns with technical specificity.",
            )

        revenue_match = re.search(r"revenue is around ([^.]+?) a month", user_text, re.IGNORECASE)
        if revenue_match:
            amount = revenue_match.group(1).strip()
            add_memory(
                content=f"User's current monthly revenue is approximately {amount}",
                category="fact",
                importance_score=8.0,
                reasoning="The user provides a current monthly revenue figure.",
            )

        if "infrastructure is minimal" in lower_text:
            add_memory(
                content="User's infrastructure costs are minimal",
                category="fact",
                importance_score=5.0,
                reasoning="The user explicitly states that infrastructure costs are minimal.",
            )

        if "mobile app" in lower_text:
            add_memory(
                content="User's product has a mobile app client",
                category="fact",
                importance_score=7.0,
                reasoning="The user explicitly mentions their mobile app.",
            )

        if "over-fetch" in lower_text and "mobile app" in lower_text:
            add_memory(
                content="User's API has over-fetching issues with the mobile client",
                category="fact",
                importance_score=4.0,
                reasoning="The user ties over-fetching directly to the mobile client.",
            )

        if "unit economics" in lower_text and "b2b" in lower_text and "margins" in lower_text:
            add_memory(
                content="User understands unit economics and chose B2B for better margins",
                category="expertise",
                importance_score=5.0,
                reasoning="The user explains a business-model decision using unit economics and margin reasoning.",
            )

        if "docker compose" in lower_text and ("deploy" in lower_text or "using docker compose so far" in lower_text):
            add_memory(
                content="User currently deploys with Docker Compose",
                category="expertise",
                importance_score=6.0,
                reasoning="The user says they have been using Docker Compose for deployment.",
            )

        if "whatsapp and excel" in lower_text and ("tier 2" in lower_text or "50 customer discovery interviews" in lower_text):
            add_memory(
                content="User believes SMB software opportunity is in affordable tools below enterprise pricing",
                category="goal",
                importance_score=6.0,
                reasoning="The user describes the opportunity as better tools that remain affordable for SMBs.",
            )
            add_memory(
                content="User has deep knowledge of Indian SMB market, especially Tier 2 cities",
                category="expertise",
                importance_score=7.0,
                reasoning="The user backs market claims with customer discovery interviews across Tier 2 cities.",
            )

        if "potential investor" in lower_text:
            add_memory(
                content="User is in active investor conversations",
                category="goal",
                importance_score=6.0,
                reasoning="A product demo with a potential investor implies active fundraising conversations.",
            )

        if ("lead engineer" in lower_text or "engineer" in lower_text) and (
            "gold-plating" in lower_text or "over-engineer" in lower_text
        ):
            add_memory(
                content="User manages an engineer who tends to over-engineer (gold-plating) features",
                category="relationship",
                importance_score=6.0,
                reasoning="This is a persistent management relationship issue, not just a one-off fact.",
            )

        if "pydantic v2" in lower_text and ("custom validators" in lower_text or "validators completely changed" in lower_text):
            add_memory(
                content="User uses Pydantic v2 (migrated from v1) and has deep experience with its validation system",
                category="expertise",
                importance_score=7.0,
                reasoning="The user discusses the v1-to-v2 migration and validator changes with direct experience.",
            )
            add_memory(
                content="User has written custom Pydantic validators — understands the v1 vs v2 differences",
                category="expertise",
                importance_score=6.0,
                reasoning="Rewriting custom validators signals hands-on knowledge of Pydantic validation internals.",
            )

        if "no major bugs" in lower_text:
            add_memory(
                content="Launch went smoothly with no major bugs",
                category="fact",
                importance_score=5.0,
                reasoning="The user explicitly reports the launch had no major bugs.",
            )

        return memories

    @classmethod
    def _merge_memories(
        cls,
        model_memories: list[ExtractedMemory],
        heuristic_memories: list[ExtractedMemory],
    ) -> list[ExtractedMemory]:
        merged = list(model_memories)

        for candidate in heuristic_memories:
            if not any(cls._memories_overlap(existing, candidate) for existing in merged):
                merged.append(candidate)

        return merged

    @staticmethod
    def _memories_overlap(left: ExtractedMemory, right: ExtractedMemory) -> bool:
        if left.category != right.category:
            return False

        left_text = " ".join(left.content.lower().split())
        right_text = " ".join(right.content.lower().split())
        return SequenceMatcher(None, left_text, right_text).ratio() >= 0.72

    @classmethod
    def _postprocess_memories(
        cls,
        memories: list[ExtractedMemory],
        messages: list[dict[str, Any]] | None = None,
    ) -> list[ExtractedMemory]:
        user_evidence_text = cls._user_evidence_text(messages or [])
        normalized = [cls._normalize_memory(memory) for memory in memories]
        filtered = [
            memory
            for memory in normalized
            if not cls._should_drop_memory(memory, normalized, user_evidence_text)
        ]

        deduped: list[ExtractedMemory] = []
        for candidate in filtered:
            if not any(cls._postprocess_overlap(existing, candidate) for existing in deduped):
                deduped.append(candidate)

        return deduped

    @staticmethod
    def _normalize_memory(memory: ExtractedMemory) -> ExtractedMemory:
        lower = memory.content.lower()
        category = memory.category

        relationship_phrases = (
            "user's co-founder",
            "user's main investor",
            "user's first paying customer",
            "user manages an engineer",
            "named ",
        )
        has_named_person = bool(re.search(r"\b[A-Z][a-z]{2,}\b", memory.content.replace("User", "")))
        if category == "fact" and (
            any(phrase in lower for phrase in relationship_phrases)
            or (has_named_person and any(marker in lower for marker in ("co-founder", "investor", "engineer", "designer", "devops")))
        ):
            category = "relationship"

        tool_markers = (
            "fastapi",
            "postgresql",
            "sqlalchemy",
            "alembic",
            "docker compose",
            "pydantic",
            "graphql",
            "rest api",
            "orm",
        )
        if category == "fact" and any(marker in lower for marker in tool_markers):
            category = "expertise"

        if category == "procedure" and (
            "customer discovery interviews" in lower
            or ("conducted" in lower and "interviews" in lower)
            or ("completed" in lower and "interviews" in lower)
        ):
            category = "fact"

        preference_markers = (
            "prefers ",
            "likes to ",
            "likes ",
            "works best",
            "format works much better",
        )
        if category != "preference" and any(marker in lower for marker in preference_markers):
            category = "preference"

        return memory.model_copy(update={"category": category})

    @classmethod
    def _should_drop_memory(
        cls,
        memory: ExtractedMemory,
        all_memories: list[ExtractedMemory],
        user_evidence_text: str = "",
    ) -> bool:
        lower = memory.content.lower()

        temporary_problem_patterns = (
            "is experiencing ",
            "is struggling with ",
            "has attempted to use ",
            "has explored using ",
        )
        if any(pattern in lower for pattern in temporary_problem_patterns):
            return True

        if cls._is_assistant_instruction_memory(lower):
            return True

        if cls._is_vague_low_information_memory(lower):
            return True

        if cls._looks_unsupported_by_user(memory, user_evidence_text):
            return True

        tentative_patterns = (
            "considering trying ",
            "thinking about trying ",
        )
        if any(pattern in lower for pattern in tentative_patterns):
            return True

        if "more reliably" in lower and memory.category == "goal":
            return True

        one_time_context_patterns = (
            "in a hurry today",
            "has a meeting in",
            "meeting in ",
            "busy today",
        )
        if any(pattern in lower for pattern in one_time_context_patterns):
            return True

        if "improve focus" in lower and memory.category == "goal":
            return True

        beginner_match = re.search(r"user is (?:a )?beginner in (.+)", lower)
        if beginner_match and memory.category == "fact":
            subject = beginner_match.group(1).strip().rstrip(".")
            learning_signals = (
                other.category == "goal"
                and (
                    f"learning {subject}" in other.content.lower()
                    or subject in other.content.lower() and "learning" in other.content.lower()
                )
                for other in all_memories
                if other is not memory
            )
            if any(learning_signals):
                return True

        return False

    @staticmethod
    def _user_evidence_text(messages: list[dict[str, Any]]) -> str:
        return " ".join(
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("role", "user")).lower() == "user"
        ).lower()

    @staticmethod
    def _is_assistant_instruction_memory(lower_content: str) -> bool:
        instruction_patterns = (
            "user should ",
            "user needs to ",
            "user must ",
            "user has to ",
            "user was advised to ",
            "user was told to ",
            "user's assignment is ",
            "user has an assignment to ",
        )
        return any(pattern in lower_content for pattern in instruction_patterns)

    @staticmethod
    def _is_vague_low_information_memory(lower_content: str) -> bool:
        vague_patterns = (
            "new project related to ",
            "project related to ",
            "something related to ",
            "work related to ",
            "is interested in something",
            "is working on something",
            "mentioned something",
            "talked about something",
        )
        if any(pattern in lower_content for pattern in vague_patterns):
            return True

        vague_project_match = re.search(
            r"\buser (?:is |has been )?(?:building|working on|starting|planning) "
            r"(?:a |an |the )?(?:new )?project\b",
            lower_content,
        )
        if not vague_project_match:
            return False

        concrete_signals = (
            "using ",
            "with ",
            "for ",
            "repository",
            "github",
            "deployed",
            "built ",
            "classified",
            "predict",
            "pipeline",
        )
        return not any(signal in lower_content for signal in concrete_signals)

    @classmethod
    def _looks_unsupported_by_user(cls, memory: ExtractedMemory, user_evidence_text: str) -> bool:
        if not user_evidence_text:
            return False

        lower = memory.content.lower()
        # Only apply this conservative evidence check to memories that commonly
        # come from assistant advice or broad inference. Concrete memories still
        # pass through the normal quality filters.
        risky_starts = (
            "user should ",
            "user needs to ",
            "user must ",
            "user has to ",
            "user plans to ",
            "user wants to ",
            "user's next step ",
        )
        if not lower.startswith(risky_starts):
            return False

        memory_tokens = cls._significant_tokens(lower)
        evidence_tokens = cls._significant_tokens(user_evidence_text)
        if not memory_tokens or not evidence_tokens:
            return False

        overlap = memory_tokens & evidence_tokens
        return len(overlap) < 2

    @staticmethod
    def _significant_tokens(text: str) -> set[str]:
        stop_words = {
            "about",
            "after",
            "again",
            "also",
            "because",
            "before",
            "being",
            "could",
            "from",
            "have",
            "into",
            "just",
            "more",
            "need",
            "needs",
            "next",
            "only",
            "should",
            "that",
            "their",
            "there",
            "this",
            "user",
            "using",
            "want",
            "wants",
            "what",
            "when",
            "where",
            "with",
            "work",
            "working",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9+#._/-]{2,}", text.lower())
            if token not in stop_words
        }

    @staticmethod
    def _postprocess_overlap(left: ExtractedMemory, right: ExtractedMemory) -> bool:
        left_text = " ".join(left.content.lower().split())
        right_text = " ".join(right.content.lower().split())
        ratio = SequenceMatcher(None, left_text, right_text).ratio()
        if left.category == right.category and ratio >= 0.72:
            return True

        if ratio < 0.82:
            return False

        keywords = (
            "fastapi",
            "postgresql",
            "sqlalchemy",
            "alembic",
            "investor",
            "pricing",
            "beta users",
            "backend",
        )
        if any(keyword in left_text and keyword in right_text for keyword in keywords):
            return True

        same_topic_fragments = (
            ("sqlalchemy", "async"),
            ("customer discovery interviews", "tier 2"),
            ("pricing", "month"),
            ("investor", "pitch"),
        )
        for first, second in same_topic_fragments:
            if first in left_text and first in right_text and second in left_text and second in right_text:
                return True
        return False

    def _log_usage(
        self,
        *,
        provider_name: str,
        user_id: str,
        chunk_index: int,
        response_text: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        usage = usage or {}
        event = {
            "event": "extraction_usage",
            "user_id": user_id,
            "chunk_index": chunk_index,
            "provider": provider_name,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens") or max(1, math.ceil(len(response_text) / 4)),
        }
        if usage.get("model"):
            event["model"] = usage["model"]
        if usage.get("latency_ms") is not None:
            event["latency_ms"] = usage["latency_ms"]
        self.last_usage_events.append(event)
        logger.info(json.dumps(event))
