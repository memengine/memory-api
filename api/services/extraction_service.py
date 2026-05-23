from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from api.db.cache import CacheService
from api.schemas.extraction_schemas import ExtractionResult
from api.schemas.memory_schemas import ExtractedMemory
from api.services.llm_service import LLMProvider
from api.services.llm_service import LLMService

try:  # pragma: no cover - exercised implicitly when dependency is installed.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - local minimal test env fallback.
    tiktoken = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
ALLOWED_CATEGORIES = {"preference", "fact", "goal", "procedure", "relationship", "expertise"}
DEFAULT_CONFIDENCE_THRESHOLD = 0.65
MAX_CONVERSATION_TOKENS = 5000
MAX_EXISTING_MEMORIES = 20


class ExtractionError(RuntimeError):
    """Raised when the extraction model returns unusable output."""


@dataclass(frozen=True, slots=True)
class ParsedExtractionSpec:
    raw_text: str
    category_definitions: dict[str, str]
    importance_rubric: str
    never_store: list[str]
    examples: str


class ExtractionService:
    """Spec-driven memory extraction using the multi-provider LLM service."""

    _cached_spec: ParsedExtractionSpec | None = None
    _cached_spec_path: Path | None = None

    def __init__(
        self,
        *,
        client: Any | None = None,
        llm_service: LLMService | None = None,
        cache_service: CacheService | None = None,
        spec_path: Path | str | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.client = client
        self.llm_service = llm_service or LLMService(
            provider_clients={LLMProvider.GEMINI: client} if client is not None else None,
            require_provider=client is None,
            use_state_store=client is None,
        )
        self.cache_service = cache_service
        self._confidence_threshold = float(confidence_threshold)
        resolved_spec_path = Path(spec_path) if spec_path is not None else self._default_spec_path()
        parsed = self._load_spec(resolved_spec_path)
        self._category_definitions = parsed.category_definitions
        self._importance_rubric = parsed.importance_rubric
        self._never_store = parsed.never_store
        self._examples = parsed.examples

    async def extract(
        self,
        messages: list[dict[str, Any]],
        proxy_user_id: str | None = None,
        tenant_id: str | None = None,
        job_id: str | None = None,
        existing_memories: list[Any] | None = None,
        user_id: str | None = None,
    ) -> ExtractionResult:
        """Extract memory candidates from a conversation.

        Persistence is intentionally handled by the Celery pipeline's conflict
        resolver so the existing outbox, audit, and versioning behavior stays
        in one place.
        """
        resolved_user_id = proxy_user_id or user_id or ""
        conversation = self._build_conversation_string(messages)
        user_message = self._append_existing_memory_context(
            conversation,
            existing_memories or [],
        )

        response = await self.llm_service.complete(
            system_prompt=self._build_system_prompt(),
            user_message=user_message,
            temperature=0.1,
            max_tokens=1500,
            response_format="json",
        )
        await self._record_provider_usage(response.provider_used)
        kept, filtered_count, nothing_to_extract = self._parse_and_validate_response(response.content)
        LOGGER.info(
            "extraction_completed",
            extra={
                "event": "extraction_completed",
                "tenant_id": tenant_id,
                "proxy_user_id": resolved_user_id,
                "job_id": job_id,
                "provider_used": response.provider_used,
                "memories_extracted": len(kept),
                "memories_filtered": filtered_count,
                "tokens_used": response.total_tokens,
            },
        )
        return ExtractionResult(
            memories_extracted=len(kept),
            memories_filtered=filtered_count,
            conflicts_resolved=0,
            nothing_to_extract=nothing_to_extract,
            tokens_used=int(response.total_tokens or 0),
            provider_used=response.provider_used,
            job_id=str(job_id or ""),
            memories_to_store=kept,
        )

    async def _record_provider_usage(self, provider: str | None) -> None:
        if not provider:
            return
        hour_bucket = datetime.now(UTC).strftime("%Y%m%d%H")
        try:
            cache_service = self.cache_service or CacheService()
            await cache_service.increment_provider_usage(str(provider).lower(), hour_bucket, ttl=7200)
        except Exception as exc:  # pragma: no cover - metrics should never block extraction.
            LOGGER.warning(
                "provider_usage_counter_failed",
                extra={
                    "event": "provider_usage_counter_failed",
                    "provider": provider,
                    "error": str(exc),
                },
            )

    def extract_sync(
        self,
        *,
        messages: list[dict[str, Any]],
        proxy_user_id: str | None = None,
        tenant_id: str | None = None,
        job_id: str | None = None,
        existing_memories: list[Any] | None = None,
        user_id: str | None = None,
    ) -> ExtractionResult:
        return asyncio.run(
            self.extract(
                messages=messages,
                proxy_user_id=proxy_user_id,
                tenant_id=tenant_id,
                job_id=job_id,
                existing_memories=existing_memories,
                user_id=user_id,
            )
        )

    def _build_system_prompt(self) -> str:
        categories = "\n".join(
            f"- {category}: {definition.strip()}"
            for category, definition in self._category_definitions.items()
        )
        never_store = "\n".join(f"- {item}" for item in self._never_store)
        prompt = (
            "You are a memory extraction specialist. Extract reusable facts about a user "
            "from their conversation. Return JSON only. No markdown. No explanation.\n\n"
            "Memory categories:\n"
            f"{categories}\n\n"
            "Importance scoring rubric:\n"
            f"{self._importance_rubric.strip()}\n\n"
            "Never store:\n"
            f"{never_store}\n\n"
            f"Only extract memories with confidence >= {self._confidence_threshold:.2f}. "
            "Discard anything below this threshold.\n\n"
            "Return exactly this JSON shape:\n"
            '{\n'
            '  "memories": [\n'
            "    {\n"
            '      "content": "string",\n'
            '      "category": "preference|fact|goal|procedure|relationship|expertise",\n'
            '      "importance_score": float between 1.0 and 10.0,\n'
            '      "confidence": float between 0.0 and 1.0,\n'
            '      "reasoning": "one sentence why this was extracted"\n'
            "    }\n"
            "  ],\n"
            '  "nothing_to_extract": false,\n'
            '  "extraction_notes": "optional string"\n'
            "}\n\n"
            "If nothing should be extracted, return:\n"
            '{"memories":[],"nothing_to_extract":true,"extraction_notes":"reason"}'
        )
        return prompt

    def _build_conversation_string(self, messages: list[dict[str, Any]]) -> str:
        retained: list[dict[str, Any]] = []
        for message in reversed(messages):
            retained.insert(0, message)
            if len(retained) >= 6 and self._count_tokens(self._messages_to_text(retained)) > MAX_CONVERSATION_TOKENS:
                retained.pop(0)
                break

        if not retained:
            retained = messages[-6:]

        text = self._messages_to_text(retained)
        while retained and len(retained) > 6 and self._count_tokens(text) > MAX_CONVERSATION_TOKENS:
            retained.pop(0)
            text = self._messages_to_text(retained)
        return text

    def _append_existing_memory_context(self, conversation: str, existing_memories: list[Any]) -> str:
        if not existing_memories:
            return conversation
        ranked = sorted(
            existing_memories,
            key=lambda memory: float(getattr(memory, "importance_score", 0.0) or 0.0),
            reverse=True,
        )[:MAX_EXISTING_MEMORIES]
        lines = [
            conversation,
            "",
            "Existing memories for this user (for context - do not re-extract these):",
        ]
        for memory in ranked:
            category = getattr(getattr(memory, "category", ""), "value", getattr(memory, "category", "unknown"))
            content = str(getattr(memory, "content", "")).strip()
            if content:
                lines.append(f"- [{category}] {content}")
        return "\n".join(lines)

    def _parse_and_validate_response(self, raw_content: str) -> tuple[list[ExtractedMemory], int, bool]:
        try:
            data = json.loads(raw_content or "{}")
        except json.JSONDecodeError as exc:
            LOGGER.error(
                "extraction_invalid_json",
                extra={"event": "extraction_invalid_json", "raw_response": raw_content[:1000]},
            )
            raise ExtractionError("LLM returned invalid JSON for memory extraction") from exc

        raw_memories = data.get("memories") or []
        if data.get("nothing_to_extract"):
            return [], len(raw_memories), True
        if not isinstance(raw_memories, list):
            raise ExtractionError("LLM extraction response has non-list memories field")

        kept: list[ExtractedMemory] = []
        for raw_memory in raw_memories:
            candidate = self._coerce_memory(raw_memory)
            if candidate is not None:
                kept.append(candidate)
        return kept, len(raw_memories) - len(kept), False

    def _coerce_memory(self, raw_memory: Any) -> ExtractedMemory | None:
        if not isinstance(raw_memory, dict):
            return None

        content = str(raw_memory.get("content") or "").strip()
        category = str(raw_memory.get("category") or "").strip().lower()
        reasoning = str(raw_memory.get("reasoning") or "").strip() or "Extracted from conversation"
        try:
            importance_score = float(raw_memory.get("importance_score"))
            confidence = float(raw_memory.get("confidence"))
        except (TypeError, ValueError):
            return None

        if confidence < self._confidence_threshold:
            return None
        if importance_score < 2.0:
            return None
        if category not in ALLOWED_CATEGORIES:
            return None
        if len(content) < 10:
            return None
        if len(content) > 500:
            content = content[:500].rstrip()

        try:
            return ExtractedMemory(
                content=content,
                category=category,  # type: ignore[arg-type]
                importance_score=max(1.0, min(10.0, importance_score)),
                confidence=max(0.0, min(1.0, confidence)),
                expiry="permanent",
                reasoning=reasoning,
            )
        except ValidationError:
            return None

    @classmethod
    def _load_spec(cls, spec_path: Path) -> ParsedExtractionSpec:
        if cls._cached_spec is not None and cls._cached_spec_path == spec_path:
            return cls._cached_spec
        if not spec_path.exists():
            raise RuntimeError(
                "extraction_spec.md not found at /docs/extraction_spec.md. "
                "Create this file before starting the worker."
            )
        raw_text = spec_path.read_text(encoding="utf-8")
        parsed = ParsedExtractionSpec(
            raw_text=raw_text,
            category_definitions=cls._extract_category_definitions(raw_text),
            importance_rubric=cls._extract_section(raw_text, "## 2. Importance Scoring Rubric", "## 3."),
            never_store=cls._extract_never_store(raw_text),
            examples=cls._extract_section(raw_text, "## 3. Example Conversations", "## 4."),
        )
        cls._cached_spec = parsed
        cls._cached_spec_path = spec_path
        return parsed

    @staticmethod
    def _default_spec_path() -> Path:
        source_tree_path = Path(__file__).resolve().parents[2] / "docs" / "extraction_spec.md"
        app_workdir_path = Path.cwd() / "docs" / "extraction_spec.md"
        if source_tree_path.exists():
            return source_tree_path
        return app_workdir_path

    @staticmethod
    def _extract_category_definitions(raw_text: str) -> dict[str, str]:
        definitions: dict[str, str] = {}
        for category in ALLOWED_CATEGORIES:
            pattern = re.compile(
                rf"### {category.upper()}\s+\*\*Definition:\*\*(.*?)(?=\n---|\n### |\n## )",
                re.DOTALL,
            )
            match = pattern.search(raw_text)
            if match:
                definitions[category] = " ".join(match.group(1).split())[:700]
        for category in ALLOWED_CATEGORIES - definitions.keys():
            definitions[category] = f"Reusable user memory in the {category} category."
        return {category: definitions[category] for category in ("expertise", "preference", "goal", "fact", "procedure", "relationship")}

    @staticmethod
    def _extract_section(raw_text: str, start_marker: str, end_marker: str) -> str:
        start = raw_text.find(start_marker)
        if start == -1:
            return ""
        end = raw_text.find(end_marker, start + len(start_marker))
        section = raw_text[start:end if end != -1 else len(raw_text)]
        return section.strip()

    @staticmethod
    def _extract_never_store(raw_text: str) -> list[str]:
        section = ExtractionService._extract_section(raw_text, "## 4. What Should NEVER Be Stored", "## 5.")
        rules = re.findall(r"\*\*Rule\s+\d+\s+[^*]+\*\*\s*\n(.*?)(?=\n---|\n\*\*Rule|\Z)", section, flags=re.DOTALL)
        cleaned = [" ".join(rule.split())[:260] for rule in rules if rule.strip()]
        return cleaned[:20] or [
            "Never store greetings, filler, secrets, health data, one-time context, or AI-authored statements."
        ]

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user").strip().lower()
            content = str(message.get("content") or "").strip()
            if content:
                lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    @staticmethod
    def _count_tokens(text: str) -> int:
        try:
            if tiktoken is None:
                raise RuntimeError("tiktoken unavailable")
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception:
            return max(1, len(text) // 4)


__all__ = ["ExtractionError", "ExtractionService", "ExtractionResult", "ParsedExtractionSpec"]
