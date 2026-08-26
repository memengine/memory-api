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
from api.schemas.extraction_schemas import PendingExtractedMemory
from api.schemas.memory_schemas import ExtractedMemory
from api.services.llm_service import LLMService
from api.settings import get_settings

try:  # pragma: no cover - exercised implicitly when dependency is installed.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - local minimal test env fallback.
    tiktoken = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
ALLOWED_CATEGORIES = {"preference", "fact", "goal", "procedure", "relationship", "expertise"}
DEFAULT_CONFIDENCE_THRESHOLD = 0.65
DEFAULT_PENDING_CONFIDENCE_THRESHOLD = 0.45
MAX_CONVERSATION_TOKENS = 5000
MAX_EXISTING_MEMORIES = 20
COMPOSITIONAL_MIN_MESSAGES = 4
COMPOSITIONAL_MIN_USER_MESSAGES = 2
COMPOSITIONAL_MIN_CHARS = 240
COMPOSITIONAL_MIN_SIGNAL_GROUPS = 2
COMPOSITIONAL_SIGNAL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity", (" i am ", " i'm ", " my role", " founder", " engineer", " student", " teacher", " manager")),
    ("team", (" team", " company", " startup", " workspace", " client", " customer", " organisation", " organization")),
    ("project", (" building", " working on", " project", " product", " app", " platform", " workflow", " integration")),
    ("goal", (" goal", " trying to", " want to", " need to", " planning", " launch", " prepare", " improve")),
    ("preference", (" prefer", " likes", " usually", " always", " avoid", " tone", " short", " detailed", " hindi", " english")),
    ("timeline", (" today", " tomorrow", " next week", " by ", " deadline", " before", " after", " currently")),
)

TEMPORARY_SESSION_MEMORY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcurrent\s+(debugging|debug|troubleshooting|terminal|session|flow)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+with\s+the\s+(current|same)\s+.+\b(flow|debugging|debug|session)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+change\s+anything\b", re.IGNORECASE),
    re.compile(r"\bkeep\s+going\s+with\s+the\s+(current|same)\b", re.IGNORECASE),
    re.compile(r"\bnext\s+(terminal\s+)?command\b", re.IGNORECASE),
)

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
        pending_confidence_threshold: float = DEFAULT_PENDING_CONFIDENCE_THRESHOLD,
        importance_shadow_enabled: bool | None = None,
        app_env: str | None = None,
        importance_shadow_service: Any | None = None,
    ) -> None:
        self.client = client
        self.llm_service = llm_service or LLMService(
            provider_clients=None,
            require_provider=client is None,
            use_state_store=client is None,
        )
        self.cache_service = cache_service
        self._confidence_threshold = float(confidence_threshold)
        self._pending_confidence_threshold = min(float(pending_confidence_threshold), self._confidence_threshold)
        settings = get_settings() if importance_shadow_enabled is None or app_env is None else None
        resolved_shadow_enabled = (
            settings.importance_shadow_enabled
            if importance_shadow_enabled is None and settings is not None
            else bool(importance_shadow_enabled)
        )
        resolved_app_env = settings.app_env if app_env is None and settings is not None else str(app_env or "")
        self._importance_shadow_enabled = bool(
            resolved_shadow_enabled and resolved_app_env.strip().lower() == "development"
        )
        self._importance_shadow_review_dir = (
            settings.importance_shadow_review_dir if settings is not None else ""
        )
        self._importance_shadow_service = importance_shadow_service
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
        source_context: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        """Extract memory candidates from a conversation.

        Persistence is intentionally handled by the Celery pipeline's conflict
        resolver so the existing outbox, audit, and versioning behavior stays
        in one place.
        """
        resolved_user_id = proxy_user_id or user_id or ""
        conversation = self._build_conversation_string(messages)
        user_message = self._append_existing_memory_context(
            self._prepend_source_context(conversation, source_context),
            existing_memories or [],
        )

        composition_signals: dict[str, Any] = {}
        composition_prepass_attempted = self._should_run_compositional_pass(
            messages=messages,
            conversation=conversation,
            source_context=source_context,
        )
        composition_prepass_error: str | None = None
        tokens_used = 0
        provider_used: str | None = None
        if composition_prepass_attempted:
            try:
                composition_response = await self.llm_service.complete(
                    system_prompt=self._build_composition_system_prompt(),
                    user_message=conversation,
                    temperature=0.0,
                    max_tokens=700,
                    response_format="json",
                )
                tokens_used += int(composition_response.total_tokens or 0)
                provider_used = composition_response.provider_used or provider_used
                await self._record_provider_usage(composition_response.provider_used)
                composition_signals = self._parse_composition_response(composition_response.content)
                if composition_signals:
                    user_message = self._append_composition_context(user_message, composition_signals)
            except Exception as exc:  # pragma: no cover - defensive fail-open path.
                composition_prepass_error = exc.__class__.__name__
                LOGGER.warning(
                    "composition_pass_failed",
                    extra={
                        "event": "composition_pass_failed",
                        "tenant_id": tenant_id,
                        "proxy_user_id": resolved_user_id,
                        "job_id": job_id,
                        "error": str(exc),
                    },
                )
        response = await self.llm_service.complete(
            system_prompt=self._build_system_prompt(
                source_context=source_context,
                has_composition_signals=bool(composition_signals),
            ),
            user_message=user_message,
            temperature=0.1,
            max_tokens=1500,
            response_format="json",
        )
        tokens_used += int(response.total_tokens or 0)
        provider_used = response.provider_used or provider_used
        await self._record_provider_usage(response.provider_used)
        kept, pending, filtered_count, nothing_to_extract = self._parse_and_validate_response(response.content)
        self._observe_importance_shadow(
            kept=kept,
            pending=pending,
            messages=messages,
            tenant_id=tenant_id,
            proxy_user_id=resolved_user_id,
            job_id=job_id,
        )
        LOGGER.info(
            "extraction_completed",
            extra={
                "event": "extraction_completed",
                "tenant_id": tenant_id,
                "proxy_user_id": resolved_user_id,
                "job_id": job_id,
                "provider_used": provider_used,
                "compositional_pass": bool(composition_signals),
                "memories_extracted": len(kept),
                "memories_filtered": filtered_count,
                "pending_candidates": len(pending),
                "tokens_used": response.total_tokens,
            },
        )
        return ExtractionResult(
            memories_extracted=len(kept),
            memories_filtered=filtered_count,
            pending_candidates_count=len(pending),
            conflicts_resolved=0,
            nothing_to_extract=nothing_to_extract,
            tokens_used=tokens_used,
            provider_used=provider_used or "unknown",
            job_id=str(job_id or ""),
            memories_to_store=kept,
            pending_candidates=pending,
            extraction_metadata={
                "compositional_pass_attempted": composition_prepass_attempted,
                "compositional_pass_used": bool(composition_signals),
                "compositional_entities": len(composition_signals.get("entities") or []),
                "compositional_relationships": len(composition_signals.get("relationships") or []),
                "compositional_error": composition_prepass_error,
            },
        )

    def _observe_importance_shadow(
        self,
        *,
        kept: list[ExtractedMemory],
        pending: list[PendingExtractedMemory],
        messages: list[dict[str, Any]],
        tenant_id: str | None,
        proxy_user_id: str,
        job_id: str | None,
    ) -> None:
        if not self._importance_shadow_enabled:
            return
        try:
            if self._importance_shadow_service is None:
                from api.services.importance_shadow_service import ImportanceShadowService

                self._importance_shadow_service = ImportanceShadowService(
                    review_dir=self._importance_shadow_review_dir,
                )
            self._importance_shadow_service.observe(
                stored=kept,
                pending=pending,
                messages=messages,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                job_id=job_id,
            )
        except Exception as exc:  # pragma: no cover - fail-open observer boundary.
            try:
                recorder = getattr(self._importance_shadow_service, "record_failure", None)
                if recorder is not None:
                    recorder(
                        error=exc,
                        tenant_id=tenant_id,
                        proxy_user_id=proxy_user_id,
                        job_id=job_id,
                    )
            except Exception:
                pass
            LOGGER.warning(
                "importance_shadow_failed: %s: %s",
                exc.__class__.__name__,
                exc,
                extra={
                    "event": "importance_shadow_failed",
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "job_id": job_id,
                    "error": str(exc),
                },
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
        source_context: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        return asyncio.run(
            self.extract(
                messages=messages,
                proxy_user_id=proxy_user_id,
                tenant_id=tenant_id,
                job_id=job_id,
                existing_memories=existing_memories,
                user_id=user_id,
                source_context=source_context,
            )
        )

    def _build_system_prompt(
        self,
        *,
        source_context: dict[str, Any] | None = None,
        has_composition_signals: bool = False,
    ) -> str:
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
            f"Extract strong memories with confidence >= {self._confidence_threshold:.2f}. "
            f"Also return borderline candidates with confidence >= {self._pending_confidence_threshold:.2f}; "
            "MemoryOS will hold those as pending candidates instead of storing them permanently. "
            f"Discard anything below {self._pending_confidence_threshold:.2f}.\n\n"
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
        if source_context:
            prompt += (
                "\n\nAUTHENTICATED SERVICE EVENT MODE\n"
                "This payload was deliberately submitted by a registered backend service. "
                "Declarative statements from the service are authoritative observations, "
                "even when represented with the assistant role. Extract durable customer "
                "facts asserted by the service, but never extract questions, instructions, "
                "speculation, credentials, or unsupported implications. Canonicalize the "
                "result as a fact about the user/customer."
            )
        if has_composition_signals:
            prompt += (
                "\n\nCOMPOSITIONAL EXTRACTION MODE\n"
                "The user message includes compact entity and relationship hints from an earlier pass. "
                "Use those hints only when they are directly supported by the transcript. "
                "They are not memories by themselves. Convert supported cross-message relationships "
                "into clean, atomic memories and discard unsupported hints."
            )
        return prompt

    @staticmethod
    def _should_run_compositional_pass(
        *,
        messages: list[dict[str, Any]],
        conversation: str,
        source_context: dict[str, Any] | None,
    ) -> bool:
        if source_context:
            return False
        if len(messages) < COMPOSITIONAL_MIN_MESSAGES:
            return False
        if len(conversation) < COMPOSITIONAL_MIN_CHARS:
            return False

        user_turns = [
            str(message.get("content") or "")
            for message in messages
            if str(message.get("role") or "").lower() == "user" and str(message.get("content") or "").strip()
        ]
        if len(user_turns) < COMPOSITIONAL_MIN_USER_MESSAGES:
            return False

        signal_groups = ExtractionService._composition_signal_groups("\n".join(user_turns))
        if len(signal_groups) < COMPOSITIONAL_MIN_SIGNAL_GROUPS:
            return False

        # At least two user turns should carry durable signals. This avoids an
        # extra LLM call for one long message that the normal extractor can handle.
        signaled_turns = sum(1 for turn in user_turns if ExtractionService._composition_signal_groups(turn))
        return signaled_turns >= COMPOSITIONAL_MIN_USER_MESSAGES

    @staticmethod
    def _build_composition_system_prompt() -> str:
        return (
            "You are pass 1 in a two-pass MemoryOS extraction pipeline. "
            "Find durable entities and relationships that require connecting details across multiple user turns. "
            "Do not create memories, do not infer beyond the transcript, and ignore one-off operational chatter. "
            "Return JSON only.\n\n"
            "Return exactly this JSON shape:\n"
            '{"entities":[{"name":"string","type":"person|project|company|tool|role|goal|preference|other","evidence":"short quote or turn summary"}],'
            '"relationships":[{"subject":"string","relation":"string","object":"string","evidence":"short quote or turn summary","confidence":0.0}],'
            '"notes":"optional string"}'
        )

    @staticmethod
    def _composition_signal_groups(text: str) -> set[str]:
        padded = f" {text.lower()} "
        groups: set[str] = set()
        for group_name, markers in COMPOSITIONAL_SIGNAL_GROUPS:
            if any(marker in padded for marker in markers):
                groups.add(group_name)
        return groups

    @staticmethod
    def _parse_composition_response(raw_content: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_content or "{}")
        except json.JSONDecodeError:
            LOGGER.warning("composition_pass_invalid_json", extra={"event": "composition_pass_invalid_json"})
            return {}
        if not isinstance(data, dict):
            return {}

        entities = [item for item in data.get("entities") or [] if isinstance(item, dict)][:12]
        relationships = [item for item in data.get("relationships") or [] if isinstance(item, dict)][:12]
        if not entities and not relationships:
            return {}
        return {"entities": entities, "relationships": relationships}

    @staticmethod
    def _append_composition_context(user_message: str, signals: dict[str, Any]) -> str:
        lines = [
            user_message,
            "",
            "Compositional extraction hints from pass 1 (use only if supported by transcript):",
        ]
        for entity in signals.get("entities") or []:
            name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("type") or "other").strip()
            evidence = str(entity.get("evidence") or "").strip()
            if name:
                lines.append(f"- entity: {name} ({entity_type}) evidence: {evidence[:160]}")
        for relation in signals.get("relationships") or []:
            subject = str(relation.get("subject") or "").strip()
            predicate = str(relation.get("relation") or "").strip()
            obj = str(relation.get("object") or "").strip()
            confidence = relation.get("confidence", "")
            evidence = str(relation.get("evidence") or "").strip()
            if subject and predicate and obj:
                lines.append(
                    f"- relationship: {subject} --{predicate}--> {obj} "
                    f"confidence: {confidence} evidence: {evidence[:160]}"
                )
        return "\n".join(lines)
    @staticmethod
    def _prepend_source_context(
        conversation: str,
        source_context: dict[str, Any] | None,
    ) -> str:
        if not source_context:
            return conversation
        service = str(source_context.get("service") or "registered-service")
        observed_at = str(source_context.get("observed_at") or "")
        return (
            "Authenticated backend observation\n"
            f"Service: {service}\n"
            f"Observed at: {observed_at}\n"
            "Treat declarative service statements as observed customer facts.\n\n"
            f"{conversation}"
        )

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

    def _parse_and_validate_response(
        self,
        raw_content: str,
    ) -> tuple[list[ExtractedMemory], list[PendingExtractedMemory], int, bool]:
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
            return [], [], len(raw_memories), True
        if not isinstance(raw_memories, list):
            raise ExtractionError("LLM extraction response has non-list memories field")

        kept: list[ExtractedMemory] = []
        pending: list[PendingExtractedMemory] = []
        invalid_count = 0
        for raw_memory in raw_memories:
            candidate = self._coerce_memory(raw_memory)
            if candidate is None:
                invalid_count += 1
                continue
            if candidate.confidence >= self._confidence_threshold:
                kept.append(
                    ExtractedMemory(
                        content=candidate.content,
                        category=candidate.category,  # type: ignore[arg-type]
                        importance_score=candidate.importance_score,
                        confidence=candidate.confidence,
                        expiry="permanent",
                        reasoning=candidate.reasoning,
                    )
                )
            else:
                pending.append(candidate)
        return kept, pending, invalid_count, False

    def _coerce_memory(self, raw_memory: Any) -> PendingExtractedMemory | None:
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

        if confidence < self._pending_confidence_threshold:
            return None
        if importance_score < 2.0:
            return None
        if category not in ALLOWED_CATEGORIES:
            return None
        if self._looks_like_temporary_session_memory(content=content, category=category, reasoning=reasoning):
            return None
        if len(content) < 10:
            return None
        if len(content) > 500:
            content = content[:500].rstrip()

        try:
            return PendingExtractedMemory(
                content=content,
                category=category,
                importance_score=max(1.0, min(10.0, importance_score)),
                confidence=max(0.0, min(1.0, confidence)),
                reasoning=reasoning,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _looks_like_temporary_session_memory(*, content: str, category: str, reasoning: str) -> bool:
        if category not in {"preference", "procedure", "goal", "fact"}:
            return False
        combined = f"{content}\n{reasoning}"
        return any(pattern.search(combined) for pattern in TEMPORARY_SESSION_MEMORY_PATTERNS)
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




