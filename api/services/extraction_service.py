from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.db.cache import CacheService
from api.schemas.extraction_schemas import ExtractionResult, PendingExtractedMemory
from api.schemas.memory_schemas import ExtractedMemory
from api.services.evidence_policy import (
    has_explicit_proposal_denial,
    validate_conversational_evidence,
)
from api.services.llm_service import LLMService
from api.settings import get_settings

try:  # pragma: no cover - exercised implicitly when dependency is installed.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - local minimal test env fallback.
    tiktoken = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)
ALLOWED_CATEGORIES = {
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.65
DEFAULT_PENDING_CONFIDENCE_THRESHOLD = 0.45
MAX_CONVERSATION_TOKENS = 5000
MAX_EXISTING_MEMORIES = 20
MAX_COMPOSITION_HINT_TOKENS = 800
MAX_EXISTING_MEMORY_CONTEXT_TOKENS = 1200
MAX_PRIMARY_INPUT_TOKENS = 10_000
COMPOSITIONAL_MIN_MESSAGES = 4
COMPOSITIONAL_MIN_USER_MESSAGES = 2
COMPOSITIONAL_MIN_CHARS = 240
COMPOSITIONAL_MIN_SIGNAL_GROUPS = 2
COMPOSITIONAL_SIGNAL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "identity",
        (
            " i am ",
            " i'm ",
            " my role",
            " founder",
            " engineer",
            " student",
            " teacher",
            " manager",
        ),
    ),
    (
        "team",
        (
            " team",
            " company",
            " startup",
            " workspace",
            " client",
            " customer",
            " organisation",
            " organization",
        ),
    ),
    (
        "project",
        (
            " building",
            " working on",
            " project",
            " product",
            " app",
            " platform",
            " workflow",
            " integration",
        ),
    ),
    (
        "goal",
        (
            " goal",
            " trying to",
            " want to",
            " need to",
            " planning",
            " launch",
            " prepare",
            " improve",
        ),
    ),
    (
        "preference",
        (
            " prefer",
            " likes",
            " usually",
            " always",
            " avoid",
            " tone",
            " short",
            " detailed",
            " hindi",
            " english",
        ),
    ),
    (
        "timeline",
        (
            " today",
            " tomorrow",
            " next week",
            " by ",
            " deadline",
            " before",
            " after",
            " currently",
        ),
    ),
)

TEMPORARY_SESSION_MEMORY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bcurrent\s+(debugging|debug|troubleshooting|terminal|session|flow)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcontinue\s+with\s+the\s+(current|same)\s+.+\b(flow|debugging|debug|session)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdo\s+not\s+change\s+anything\b", re.IGNORECASE),
    re.compile(r"\bkeep\s+going\s+with\s+the\s+(current|same)\b", re.IGNORECASE),
    re.compile(r"\bnext\s+(terminal\s+)?command\b", re.IGNORECASE),
)

CORRECTION_REPLACEMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:prefer|preference|pasand|chahiye)\b", re.IGNORECASE),
    re.compile(r"(?:पसंद|चाहिए|प्राथमिकता)"),
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
        proposal_confirmation_enabled: bool | None = None,
    ) -> None:
        self.client = client
        self.llm_service = llm_service or LLMService(
            provider_clients=None,
            require_provider=client is None,
            use_state_store=client is None,
        )
        self.cache_service = cache_service
        self._confidence_threshold = float(confidence_threshold)
        self._pending_confidence_threshold = min(
            float(pending_confidence_threshold), self._confidence_threshold
        )
        settings = (
            get_settings()
            if (
                importance_shadow_enabled is None
                or app_env is None
                or proposal_confirmation_enabled is None
            )
            else None
        )
        resolved_shadow_enabled = (
            settings.importance_shadow_enabled
            if importance_shadow_enabled is None and settings is not None
            else bool(importance_shadow_enabled)
        )
        resolved_app_env = (
            settings.app_env
            if app_env is None and settings is not None
            else str(app_env or "")
        )
        self._importance_shadow_enabled = bool(
            resolved_shadow_enabled
            and resolved_app_env.strip().lower() == "development"
        )
        self._importance_shadow_review_dir = (
            settings.importance_shadow_review_dir if settings is not None else ""
        )
        self._importance_shadow_service = importance_shadow_service
        self._proposal_confirmation_enabled = (
            settings.phase3a_confirmation_enabled
            if proposal_confirmation_enabled is None and settings is not None
            else bool(proposal_confirmation_enabled)
        )
        resolved_spec_path = (
            Path(spec_path) if spec_path is not None else self._default_spec_path()
        )
        parsed = self._load_spec(resolved_spec_path)
        self._category_definitions = parsed.category_definitions
        self._importance_rubric = parsed.importance_rubric
        self._never_store = parsed.never_store
        self._examples = parsed.examples

    @property
    def proposal_confirmation_enabled(self) -> bool:
        return self._proposal_confirmation_enabled

    async def extract(
        self,
        messages: list[dict[str, Any]],
        proxy_user_id: str | None = None,
        tenant_id: str | None = None,
        job_id: str | None = None,
        existing_memories: list[Any] | None = None,
        user_id: str | None = None,
        source_context: dict[str, Any] | None = None,
        proposal_context: list[dict[str, Any]] | None = None,
    ) -> ExtractionResult:
        """Extract memory candidates from a conversation.

        Persistence is intentionally handled by the Celery pipeline's conflict
        resolver so the existing outbox, audit, and versioning behavior stays
        in one place.
        """
        resolved_user_id = proxy_user_id or user_id or ""
        indexed_messages = [
            {**message, "_turn_index": index}
            for index, message in enumerate(messages)
        ]
        conversation, visible_turn_indexes = self._build_conversation_context(
            indexed_messages
        )
        base_user_message = self._prepend_source_context(conversation, source_context)
        user_message = self._append_existing_memory_context(
            base_user_message,
            existing_memories or [],
        )
        prompt_context_metrics = self._prompt_context_metrics(
            conversation=conversation,
            before_existing_context=base_user_message,
            after_existing_context=user_message,
            existing_memories=existing_memories or [],
        )

        composition_signals: dict[str, Any] = {}
        composition_prepass_attempted = self._should_run_compositional_pass(
            messages=indexed_messages,
            conversation=conversation,
            source_context=source_context,
        )
        composition_prepass_error: str | None = None
        composition_response: Any | None = None
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
                composition_signals = self._parse_composition_response(
                    composition_response.content
                )
                if composition_signals:
                    before_composition = user_message
                    user_message = self._append_composition_context(
                        user_message, composition_signals
                    )
                    prompt_context_metrics["composition_hint_tokens"] = max(
                        0,
                        self._count_tokens(user_message)
                        - self._count_tokens(before_composition),
                    )
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
        primary_system_prompt = self._build_system_prompt(
            source_context=source_context,
            has_composition_signals=bool(composition_signals),
        )
        primary_system_tokens = self._count_tokens(primary_system_prompt)
        user_budget = max(0, MAX_PRIMARY_INPUT_TOKENS - primary_system_tokens)
        user_message = self._truncate_to_token_budget(user_message, user_budget)
        # Optional context is appended after the transcript and can be safely
        # truncated. A transcript turn is citable only when its complete,
        # indexed rendering survived the final total-input budget.
        visible_turn_indexes = {
            int(message.get("_turn_index", index))
            for index, message in enumerate(indexed_messages)
            if self._messages_to_text([message]) in user_message
        }
        prompt_context_metrics["primary_user_message_tokens"] = self._count_tokens(
            user_message
        )
        prompt_context_metrics.setdefault("composition_hint_tokens", 0)
        prompt_context_metrics["composition_hint_budget_tokens"] = (
            MAX_COMPOSITION_HINT_TOKENS
        )
        prompt_context_metrics["primary_system_prompt_tokens"] = primary_system_tokens
        prompt_context_metrics["primary_input_budget_tokens"] = MAX_PRIMARY_INPUT_TOKENS
        prompt_context_metrics["primary_input_tokens"] = (
            primary_system_tokens
            + prompt_context_metrics["primary_user_message_tokens"]
        )
        prompt_context_metrics["visible_turn_count"] = len(visible_turn_indexes)
        primary_started = time.perf_counter()
        response = await self.llm_service.complete(
            system_prompt=primary_system_prompt,
            user_message=user_message,
            temperature=0.0 if proposal_context else 0.1,
            max_tokens=1500,
            response_format="json",
        )
        primary_wall_latency_ms = int((time.perf_counter() - primary_started) * 1000)
        tokens_used += int(response.total_tokens or 0)
        provider_used = response.provider_used or provider_used
        await self._record_provider_usage(response.provider_used)
        kept, pending, filtered_count, nothing_to_extract, rejection_counts = (
            self._parse_and_validate_response(
                response.content,
                messages=indexed_messages,
                visible_turn_indexes=visible_turn_indexes,
                source_context=source_context,
                evidence_context={
                    "provider": response.provider_used,
                    "model": response.model_used,
                    "extracted_at": datetime.now(UTC).isoformat(),
                    "extractor_version": "structured-evidence-v1",
                },
                proposal_context=proposal_context,
            )
        )
        correction_recovery_attempted = self._should_attempt_correction_recovery(
            messages=indexed_messages,
            source_context=source_context,
            proposal_context=proposal_context,
            kept=kept,
            pending=pending,
        )
        correction_recovery_response: Any | None = None
        correction_recovery_error: str | None = None
        correction_recovery_kept = 0
        correction_recovery_pending = 0
        correction_recovery_wall_latency_ms = 0
        if correction_recovery_attempted:
            try:
                recovery_started = time.perf_counter()
                correction_recovery_response = await self.llm_service.complete(
                    system_prompt=self._build_correction_recovery_prompt(),
                    user_message=self._correction_recovery_user_message(
                        indexed_messages
                    ),
                    temperature=0.0,
                    max_tokens=600,
                    response_format="json",
                )
                correction_recovery_wall_latency_ms = int(
                    (time.perf_counter() - recovery_started) * 1000
                )
                tokens_used += int(correction_recovery_response.total_tokens or 0)
                provider_used = (
                    correction_recovery_response.provider_used or provider_used
                )
                await self._record_provider_usage(
                    correction_recovery_response.provider_used
                )
                (
                    recovered_kept,
                    recovered_pending,
                    recovered_filtered,
                    _recovered_nothing,
                    recovered_rejections,
                ) = self._parse_and_validate_response(
                    self._bind_correction_recovery_evidence(
                        correction_recovery_response.content,
                        indexed_messages,
                    ),
                    messages=indexed_messages,
                    visible_turn_indexes=visible_turn_indexes,
                    source_context=source_context,
                    evidence_context={
                        "provider": correction_recovery_response.provider_used,
                        "model": correction_recovery_response.model_used,
                        "extracted_at": datetime.now(UTC).isoformat(),
                        "extractor_version": "structured-evidence-v1",
                        "pass": "correction_recovery",
                    },
                    # This pass can recover only the user's independent
                    # replacement claim; it cannot confirm any proposal.
                    proposal_context=None,
                )
                seen = {
                    (item.category, " ".join(item.content.casefold().split()))
                    for item in [*kept, *pending]
                }
                for item in recovered_kept:
                    key = (item.category, " ".join(item.content.casefold().split()))
                    if key not in seen:
                        kept.append(item)
                        seen.add(key)
                        correction_recovery_kept += 1
                for item in recovered_pending:
                    key = (item.category, " ".join(item.content.casefold().split()))
                    if key not in seen:
                        pending.append(item)
                        seen.add(key)
                        correction_recovery_pending += 1
                filtered_count += recovered_filtered
                for reason, count in recovered_rejections.items():
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + count
                nothing_to_extract = not kept and not pending
            except Exception as exc:  # pragma: no cover - defensive fail-open path.
                correction_recovery_error = exc.__class__.__name__
                LOGGER.warning(
                    "correction_recovery_failed",
                    extra={
                        "event": "correction_recovery_failed",
                        "tenant_id": tenant_id,
                        "proxy_user_id": resolved_user_id,
                        "job_id": job_id,
                        "error": str(exc),
                    },
                )
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
                "tokens_used": tokens_used,
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
                "candidate_validation": {
                    "model_returned_memories": len(kept)
                    + len(pending)
                    + filtered_count,
                    "accepted_for_storage": len(kept),
                    "accepted_as_pending": len(pending),
                    "rejected": filtered_count,
                    "rejection_counts": rejection_counts,
                    "model_marked_nothing_to_extract": nothing_to_extract,
                },
                "proposal_confirmation": {
                    "enabled": self._proposal_confirmation_enabled,
                    "active_proposal_count": len(proposal_context or []),
                    "accepted": sum(
                        item.validated_evidence.get("relation")
                        == "user_confirmed_assistant_proposal"
                        for item in kept
                    ),
                    "pending": sum(
                        item.candidate_reason
                        in {
                            "ambiguous_proposal_reference",
                            "proposal_reference_mismatch",
                        }
                        for item in pending
                    ),
                    "rejected_reasons": {
                        reason: count
                        for reason, count in rejection_counts.items()
                        if reason.startswith("proposal_")
                    },
                },
                "prompt_context": prompt_context_metrics,
                "primary_pass": {
                    "provider": response.provider_used,
                    "model": response.model_used,
                    "input_tokens": int(response.input_tokens or 0),
                    "output_tokens": int(response.output_tokens or 0),
                    "total_tokens": int(response.total_tokens or 0),
                    "latency_ms": int(response.latency_ms or primary_wall_latency_ms),
                    "wall_latency_ms": primary_wall_latency_ms,
                },
                "correction_recovery": {
                    "attempted": correction_recovery_attempted,
                    "completed": correction_recovery_response is not None,
                    "accepted_for_storage": correction_recovery_kept,
                    "accepted_as_pending": correction_recovery_pending,
                    "provider": getattr(
                        correction_recovery_response, "provider_used", None
                    ),
                    "model": getattr(
                        correction_recovery_response, "model_used", None
                    ),
                    "input_tokens": int(
                        getattr(correction_recovery_response, "input_tokens", 0) or 0
                    ),
                    "output_tokens": int(
                        getattr(correction_recovery_response, "output_tokens", 0) or 0
                    ),
                    "total_tokens": int(
                        getattr(correction_recovery_response, "total_tokens", 0) or 0
                    ),
                    "latency_ms": int(
                        getattr(correction_recovery_response, "latency_ms", 0) or 0
                    ),
                    "wall_latency_ms": correction_recovery_wall_latency_ms,
                    "error": correction_recovery_error,
                },
                "compositional_pass_metrics": {
                    "attempted": composition_prepass_attempted,
                    "completed": composition_response is not None,
                    "used": bool(composition_signals),
                    "provider": getattr(composition_response, "provider_used", None),
                    "model": getattr(composition_response, "model_used", None),
                    "input_tokens": int(
                        getattr(composition_response, "input_tokens", 0) or 0
                    ),
                    "output_tokens": int(
                        getattr(composition_response, "output_tokens", 0) or 0
                    ),
                    "total_tokens": int(
                        getattr(composition_response, "total_tokens", 0) or 0
                    ),
                    "latency_ms": int(
                        getattr(composition_response, "latency_ms", 0) or 0
                    ),
                    "error": composition_prepass_error,
                },
                "compositional_pass_attempted": composition_prepass_attempted,
                "compositional_pass_used": bool(composition_signals),
                "compositional_entities": len(
                    composition_signals.get("entities") or []
                ),
                "compositional_relationships": len(
                    composition_signals.get("relationships") or []
                ),
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
                from api.services.importance_shadow_service import (
                    ImportanceShadowService,
                )

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
                recorder = getattr(
                    self._importance_shadow_service, "record_failure", None
                )
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
            await cache_service.increment_provider_usage(
                str(provider).lower(), hour_bucket, ttl=7200
            )
        except (
            Exception
        ) as exc:  # pragma: no cover - metrics should never block extraction.
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
        proposal_context: list[dict[str, Any]] | None = None,
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
                proposal_context=proposal_context,
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
            "Confidence measures how strongly the user commits to the claim, not how fluent the model feels. "
            "Use 0.45-0.64 for tentative, conditional, someday, and no-current-plan claims "
            "so they remain pending. Use >= 0.80 only for direct, unambiguous commitments or current facts. "
            "Preserve uncertainty and no-current-plan qualifiers in the memory content. A conditional possibility "
            "the user is genuinely considering (for example, 'I might relocate if my partner gets the role') is "
            "a pending goal, not a current fact. A purely counterfactual claim whose premise is not currently true "
            "and which the user is not considering is not a present goal and must be discarded. "
            "Do not use pending confidence as a substitute for temporal validity. "
            "Do not default every plausible memory to 0.70 or 0.80.\n\n"
            "Use importance 1-3 for narrow project-only or occasionally useful context; 4-6 for "
            "regularly useful operating context; and 7-9 only for identity-level facts, committed priorities, "
            "or capabilities that should shape most responses. Do not default every memory to 5.\n\n"
            "The current persistence path cannot enforce an explicit end date. Do not extract facts, procedures, "
            "roles, or workarounds whose truth ends after a stated number of days, a stated date, or a named short "
            "event.\n\n"
            "Each memory must contain one independently correctable and independently reusable claim. Split "
            "distinct governance dimensions even when they occur in one sentence. For example, separate "
            "a residence from an employment fact, a role from a responsibility, and an organization-mandated "
            "tool from the user's personal tool preference. Keep reasons, limitations, time qualifiers, and evidence "
            "that merely explain the main claim inside that claim. Do not create separate memories for absence of a "
            "preference, redundant future reversals, counterfactual background, or every supported clause. Prefer "
            "fewer durable memories over exhaustive clause extraction. Durable autobiographical facts and durable "
            "preferences remain eligible even when they appear after an unrelated question or request.\n\n"
            "Return exactly this JSON shape:\n"
            "{\n"
            '  "memories": [\n'
            "    {\n"
            '      "content": "string",\n'
            '      "category": "preference|fact|goal|procedure|relationship|expertise",\n'
            '      "importance_score": float between 1.0 and 10.0,\n'
            '      "confidence": float between 0.0 and 1.0,\n'
            '      "evidence_turns": [zero-based indexes of transcript turns supporting the memory],\n'
            '      "evidence_relation": "direct_user_statement|user_confirmed_assistant_proposal",\n'
            '      "proposal_turn": "integer for a confirmed registered proposal, otherwise null",\n'
            '      "reasoning": "one sentence why this was extracted"\n'
            "    }\n"
            "  ],\n"
            '  "nothing_to_extract": false,\n'
            '  "extraction_notes": "optional string"\n'
            "}\n\n"
            "For normal conversations, evidence_turns is mandatory and must include at least one "
            "user turn that directly states or confirms the memory. A question from the user or an "
            "unsupported assistant statement is not evidence. For a confirmation of an assistant "
            "proposal, set evidence_relation to user_confirmed_assistant_proposal and proposal_turn "
            "to the cited assistant turn index. A dependent reply such as 'yes', 'keep it', "
            "'that captures it', or a paraphrased acceptance is not a direct user statement: cite "
            "both the registered proposal turn and the later user turn. proposal_turn identifies "
            "the assistant turn; evidence_turns must include the later user turn even if it also "
            "includes the proposal. Use direct_user_statement "
            "only when the user's own words independently state the extracted claim. Apply this test "
            "mechanically: remove every assistant turn; if the candidate claim is no longer entailed, "
            "it is not direct_user_statement. References such as 'that', 'it', 'this', 'the second one', "
            "'the framing', agreement, or acceptance depend on the proposal. For direct_user_statement, "
            "set proposal_turn to null. If the user rejects an assistant proposal but states a different "
            "durable fact or preference in the same turn, never store the rejected proposal. Extract only "
            "the independently stated correction as direct_user_statement, cite the user turn, and set "
            "proposal_turn to null. This rule applies regardless of the language used by the user.\n\n"
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
    def _build_correction_recovery_prompt() -> str:
        return (
            "You are recovering one direct replacement memory from the latest user turn. "
            "The user has rejected an assistant memory proposal. Never accept, restate, or "
            "store that rejected proposal. If the same user turn independently states a "
            "different durable fact or preference, extract only that replacement claim. "
            "Preserve the user's language and key wording so the claim can be verified "
            "against the cited user turn. Set evidence_relation to direct_user_statement, "
            "proposal_turn to null, and evidence_turns to the zero-based index of the user "
            "turn. If there is no independent durable replacement claim, return no memories. "
            "Use confidence >= 0.80 for an explicit replacement stated as the user's "
            "current fact or preference; use 0.45-0.64 only when the replacement itself "
            "is tentative or conditional. "
            "Return JSON only in this shape: "
            '{"memories":[{"content":"string","category":"preference|fact|goal|'
            'procedure|relationship|expertise","importance_score":1.0,'
            '"confidence":0.9,"evidence_turns":[0],"evidence_relation":'
            '"direct_user_statement","proposal_turn":null,"reasoning":"string"}],'
            '"nothing_to_extract":false,"extraction_notes":"optional string"}. '
            'For no replacement claim return {"memories":[],"nothing_to_extract":true,'
            '"extraction_notes":"no direct replacement claim"}.'
        )

    @classmethod
    def _should_attempt_correction_recovery(
        cls,
        *,
        messages: list[dict[str, Any]],
        source_context: dict[str, Any] | None,
        proposal_context: list[dict[str, Any]] | None,
        kept: list[ExtractedMemory],
        pending: list[PendingExtractedMemory],
    ) -> bool:
        if source_context or not proposal_context:
            return False
        if any(
            item.validated_evidence.get("relation") == "direct_user_statement"
            for item in [*kept, *pending]
        ):
            return False
        latest_user_text = next(
            (
                str(message.get("content") or "").strip()
                for message in reversed(messages)
                if str(message.get("role") or "").strip().lower() == "user"
                and str(message.get("source_kind") or "direct_user_input")
                .strip()
                .lower()
                in {"direct_user_input", "client_assertion"}
            ),
            "",
        )
        if not latest_user_text or latest_user_text.rstrip().endswith(("?", "？")):
            return False
        return has_explicit_proposal_denial(latest_user_text) and any(
            pattern.search(latest_user_text)
            for pattern in CORRECTION_REPLACEMENT_PATTERNS
        )

    @classmethod
    def _correction_recovery_user_message(
        cls,
        messages: list[dict[str, Any]],
    ) -> str:
        latest_user_message = next(
            (
                message
                for message in reversed(messages)
                if str(message.get("role") or "").strip().lower() == "user"
                and str(message.get("source_kind") or "direct_user_input")
                .strip()
                .lower()
                in {"direct_user_input", "client_assertion"}
            ),
            None,
        )
        return cls._messages_to_text([latest_user_message]) if latest_user_message else ""

    @staticmethod
    def _bind_correction_recovery_evidence(
        raw_content: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Bind recovery citations to the sole server-selected user turn."""

        latest_user_index = next(
            (
                int(message.get("_turn_index", index))
                for index, message in reversed(list(enumerate(messages)))
                if str(message.get("role") or "").strip().lower() == "user"
                and str(message.get("source_kind") or "direct_user_input")
                .strip()
                .lower()
                in {"direct_user_input", "client_assertion"}
            ),
            None,
        )
        if latest_user_index is None:
            return raw_content
        try:
            payload = json.loads(raw_content or "{}")
        except json.JSONDecodeError:
            return raw_content
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return raw_content
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory["evidence_turns"] = [latest_user_index]
            memory["evidence_relation"] = "direct_user_statement"
            memory["proposal_turn"] = None
        return json.dumps(payload, ensure_ascii=False)

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
            if str(message.get("role") or "").lower() == "user"
            and str(message.get("content") or "").strip()
        ]
        if len(user_turns) < COMPOSITIONAL_MIN_USER_MESSAGES:
            return False

        signal_groups = ExtractionService._composition_signal_groups(
            "\n".join(user_turns)
        )
        if len(signal_groups) < COMPOSITIONAL_MIN_SIGNAL_GROUPS:
            return False

        # At least two user turns should carry durable signals. This avoids an
        # extra LLM call for one long message that the normal extractor can handle.
        signaled_turns = sum(
            1
            for turn in user_turns
            if ExtractionService._composition_signal_groups(turn)
        )
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
            LOGGER.warning(
                "composition_pass_invalid_json",
                extra={"event": "composition_pass_invalid_json"},
            )
            return {}
        if not isinstance(data, dict):
            return {}

        entities = [
            item for item in data.get("entities") or [] if isinstance(item, dict)
        ][:12]
        relationships = [
            item for item in data.get("relationships") or [] if isinstance(item, dict)
        ][:12]
        if not entities and not relationships:
            return {}
        return {"entities": entities, "relationships": relationships}

    @staticmethod
    def _append_composition_context(user_message: str, signals: dict[str, Any]) -> str:
        hint_lines = [
            "Compositional extraction hints from pass 1 (use only if supported by transcript):",
        ]
        for entity in signals.get("entities") or []:
            name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("type") or "other").strip()
            evidence = str(entity.get("evidence") or "").strip()
            if name:
                hint_lines.append(
                    f"- entity: {name} ({entity_type}) evidence: {evidence[:160]}"
                )
        for relation in signals.get("relationships") or []:
            subject = str(relation.get("subject") or "").strip()
            predicate = str(relation.get("relation") or "").strip()
            obj = str(relation.get("object") or "").strip()
            confidence = relation.get("confidence", "")
            evidence = str(relation.get("evidence") or "").strip()
            if subject and predicate and obj:
                hint_lines.append(
                    f"- relationship: {subject} --{predicate}--> {obj} "
                    f"confidence: {confidence} evidence: {evidence[:160]}"
                )
        hints = ExtractionService._truncate_to_token_budget(
            "\n".join(hint_lines),
            MAX_COMPOSITION_HINT_TOKENS,
        )
        return f"{user_message}\n\n{hints}"

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

    def _build_conversation_context(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, set[int]]:
        retained: list[dict[str, Any]] = []
        for message in reversed(messages):
            candidate = [message, *retained]
            if (
                self._count_tokens(self._messages_to_text(candidate))
                > MAX_CONVERSATION_TOKENS
            ):
                if retained:
                    break
                continue
            retained = candidate
            if (
                self._count_tokens(self._messages_to_text(retained))
                >= MAX_CONVERSATION_TOKENS
            ):
                break

        text = self._messages_to_text(retained)
        visible = {
            int(message.get("_turn_index", index))
            for index, message in enumerate(retained)
        }
        if not text and messages:
            text = self._truncate_to_token_budget(
                self._messages_to_text([messages[-1]]),
                MAX_CONVERSATION_TOKENS,
            )
        return text, visible

    def _build_conversation_string(self, messages: list[dict[str, Any]]) -> str:
        return self._build_conversation_context(messages)[0]

    def _append_existing_memory_context(
        self, conversation: str, existing_memories: list[Any]
    ) -> str:
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
        used_tokens = 0
        for memory in ranked:
            category = getattr(
                getattr(memory, "category", ""),
                "value",
                getattr(memory, "category", "unknown"),
            )
            content = str(getattr(memory, "content", "")).strip()
            if content:
                line = f"- [{category}] {content}"
                line_tokens = self._count_tokens(line)
                if used_tokens + line_tokens > MAX_EXISTING_MEMORY_CONTEXT_TOKENS:
                    break
                lines.append(line)
                used_tokens += line_tokens
        return "\n".join(lines)

    def _prompt_context_metrics(
        self,
        *,
        conversation: str,
        before_existing_context: str,
        after_existing_context: str,
        existing_memories: list[Any],
    ) -> dict[str, int]:
        ranked = sorted(
            existing_memories,
            key=lambda memory: float(getattr(memory, "importance_score", 0.0) or 0.0),
            reverse=True,
        )[:MAX_EXISTING_MEMORIES]
        included = 0
        used_tokens = 0
        for memory in ranked:
            content = str(getattr(memory, "content", "") or "").strip()
            if not content:
                continue
            category = getattr(
                getattr(memory, "category", ""),
                "value",
                getattr(memory, "category", "unknown"),
            )
            line_tokens = self._count_tokens(f"- [{category}] {content}")
            if used_tokens + line_tokens > MAX_EXISTING_MEMORY_CONTEXT_TOKENS:
                break
            used_tokens += line_tokens
            included += 1
        before_tokens = self._count_tokens(before_existing_context)
        after_tokens = self._count_tokens(after_existing_context)
        return {
            "conversation_tokens": self._count_tokens(conversation),
            "before_existing_memory_context_tokens": before_tokens,
            "after_existing_memory_context_tokens": after_tokens,
            "existing_memory_context_budget_tokens": MAX_EXISTING_MEMORY_CONTEXT_TOKENS,
            "existing_memory_context_tokens": max(0, after_tokens - before_tokens),
            "existing_memories_available": len(existing_memories),
            "existing_memories_included": included,
        }

    def _parse_and_validate_response(
        self,
        raw_content: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        visible_turn_indexes: set[int] | None = None,
        source_context: dict[str, Any] | None = None,
        evidence_context: dict[str, Any] | None = None,
        proposal_context: list[dict[str, Any]] | None = None,
    ) -> tuple[
        list[ExtractedMemory], list[PendingExtractedMemory], int, bool, dict[str, int]
    ]:
        try:
            data = json.loads(raw_content or "{}")
        except json.JSONDecodeError as exc:
            LOGGER.error(
                "extraction_invalid_json",
                extra={
                    "event": "extraction_invalid_json",
                    "raw_response": raw_content[:1000],
                },
            )
            raise ExtractionError(
                "LLM returned invalid JSON for memory extraction"
            ) from exc

        raw_memories = data.get("memories") or []
        if data.get("nothing_to_extract"):
            rejection_counts = {"model_marked_nothing_to_extract": 1}
            if raw_memories:
                rejection_counts["memories_ignored_after_nothing_to_extract"] = len(
                    raw_memories
                )
            return [], [], len(raw_memories), True, rejection_counts
        if not isinstance(raw_memories, list):
            raise ExtractionError("LLM extraction response has non-list memories field")

        kept: list[ExtractedMemory] = []
        pending: list[PendingExtractedMemory] = []
        invalid_count = 0
        rejection_counts: dict[str, int] = {}
        for raw_memory in raw_memories:
            candidate, rejection_reason = self._coerce_memory(raw_memory)
            validated_evidence: dict[str, Any] = {}
            if candidate is None:
                invalid_count += 1
                reason = rejection_reason or "candidate_validation"
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue
            if not source_context:
                relation = (
                    raw_memory.get("evidence_relation")
                    if isinstance(raw_memory, dict)
                    else None
                )
                proposal_turn = (
                    raw_memory.get("proposal_turn")
                    if isinstance(raw_memory, dict)
                    else None
                )
                # When the model explicitly identifies an active proposal, apply
                # the stricter proposal relation even if it labels the dependent
                # user reply as a direct statement. Authority is still derived
                # only from the server-validated proposal and transcript.
                effective_relation = (
                    "user_confirmed_assistant_proposal"
                    if proposal_context
                    and isinstance(proposal_turn, int)
                    and not isinstance(proposal_turn, bool)
                    else relation
                )
                validated_evidence = self._validated_user_evidence(
                    candidate,
                    messages or [],
                    raw_memory.get("evidence_turns")
                    if isinstance(raw_memory, dict)
                    else None,
                    effective_relation,
                    proposal_turn,
                    evidence_context=evidence_context,
                    visible_turn_indexes=visible_turn_indexes,
                    proposal_confirmation_enabled=self._proposal_confirmation_enabled,
                    active_proposals=proposal_context,
                )
                if not validated_evidence:
                    if (
                        effective_relation == "user_confirmed_assistant_proposal"
                        and self._proposal_confirmation_enabled
                    ):
                        policy = validate_conversational_evidence(
                            messages=messages or [],
                            evidence_turns=raw_memory.get("evidence_turns"),
                            evidence_relation=effective_relation,
                            proposal_turn=proposal_turn,
                            visible_turn_indexes=visible_turn_indexes,
                            proposal_confirmation_enabled=True,
                            active_proposals=proposal_context,
                        )
                        if policy.reason == "explicit_confirmation_denied":
                            # The model can correctly extract the replacement
                            # claim while incorrectly retaining proposal_turn.
                            # Accept it only if the same candidate independently
                            # passes direct-user grounding against the cited turn.
                            validated_evidence = self._validated_user_evidence(
                                candidate,
                                messages or [],
                                raw_memory.get("evidence_turns"),
                                "direct_user_statement",
                                None,
                                evidence_context=evidence_context,
                                visible_turn_indexes=visible_turn_indexes,
                            )
                        if validated_evidence:
                            candidate.validated_evidence = validated_evidence
                        elif policy.review_required:
                            candidate.candidate_reason = policy.reason
                            pending.append(candidate)
                        elif not validated_evidence:
                            invalid_count += 1
                        if not validated_evidence:
                            rejection_counts[policy.reason] = (
                                rejection_counts.get(policy.reason, 0) + 1
                            )
                            continue
                    if validated_evidence:
                        candidate.validated_evidence = validated_evidence
                    else:
                        invalid_count += 1
                        rejection_counts["evidence_validation"] = (
                            rejection_counts.get("evidence_validation", 0) + 1
                        )
                        continue
                candidate.validated_evidence = validated_evidence
            if candidate.confidence >= self._confidence_threshold:
                kept.append(
                    ExtractedMemory(
                        content=candidate.content,
                        category=candidate.category,  # type: ignore[arg-type]
                        importance_score=candidate.importance_score,
                        confidence=candidate.confidence,
                        expiry="permanent",
                        reasoning=candidate.reasoning,
                        validated_evidence=validated_evidence,
                    )
                )
            else:
                pending.append(candidate)
        return kept, pending, invalid_count, False, rejection_counts

    @classmethod
    def _has_user_evidence(
        cls,
        candidate: PendingExtractedMemory,
        messages: list[dict[str, Any]],
        evidence_turns: Any,
        evidence_relation: Any = None,
        proposal_turn: Any = None,
    ) -> bool:
        return bool(
            cls._validated_user_evidence(
                candidate,
                messages,
                evidence_turns,
                evidence_relation,
                proposal_turn,
            )
        )

    @classmethod
    def _validated_user_evidence(
        cls,
        candidate: PendingExtractedMemory,
        messages: list[dict[str, Any]],
        evidence_turns: Any,
        evidence_relation: Any = None,
        proposal_turn: Any = None,
        *,
        evidence_context: dict[str, Any] | None = None,
        visible_turn_indexes: set[int] | None = None,
        proposal_confirmation_enabled: bool = False,
        active_proposals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Require conversational memories to be grounded in a user's own turn.

        The returned record is assembled only from the server-validated policy
        result and server-observed model metadata. It never trusts a model's
        assertion of authority or a caller-supplied provenance payload.
        """
        # Legacy extractors did not return evidence_turns. Preserve direct-user
        # extraction by validating every canonical turn, but never infer a
        # proposal binding through this compatibility path.
        policy_evidence_turns = evidence_turns
        if not isinstance(policy_evidence_turns, list) and evidence_relation is None:
            policy_evidence_turns = list(range(len(messages)))
        policy = validate_conversational_evidence(
            messages=messages,
            evidence_turns=policy_evidence_turns,
            evidence_relation=evidence_relation,
            proposal_turn=proposal_turn,
            visible_turn_indexes=visible_turn_indexes,
            proposal_confirmation_enabled=proposal_confirmation_enabled,
            active_proposals=active_proposals,
        )
        if not policy.accepted:
            return {}

        indexed_messages = [
            (index, message)
            for index, message in enumerate(messages)
            if visible_turn_indexes is None or index in visible_turn_indexes
        ]
        evidence_was_provided = isinstance(evidence_turns, list)
        cited_indexes = cls._valid_evidence_indexes(evidence_turns, len(messages))
        if evidence_was_provided and not cited_indexes:
            return {}
        if cited_indexes:
            indexed_messages = [
                item for item in indexed_messages if item[0] in cited_indexes
            ]

        eligible_user_indexes = set(policy.user_turn_indexes)
        user_turns = [
            (index, str(message.get("content") or "").strip())
            for index, message in indexed_messages
            if index in eligible_user_indexes
            and str(message.get("content") or "").strip()
        ]
        candidate_tokens = cls._significant_tokens(candidate.content)
        if policy.proposal_turn_index is not None:
            proposal_content = str(
                messages[policy.proposal_turn_index].get("content") or ""
            )
            supported = bool(
                candidate_tokens & cls._significant_tokens(proposal_content)
            )

        else:
            supported = any(
                not cls._is_question_only(content)
                and bool(candidate_tokens & cls._significant_tokens(content))
                for _index, content in user_turns
            )
        if not supported:
            return {}

        normalized_context = {
            key: value
            for key, value in dict(evidence_context or {}).items()
            if value is not None
        }
        return {
            "schema_version": 1,
            "citation_mode": "model_cited"
            if evidence_was_provided
            else "legacy_compatibility",
            "turn_indexes": sorted(cited_indexes)
            if evidence_was_provided
            else list(policy.user_turn_indexes),
            "user_turn_indexes": list(policy.user_turn_indexes),
            "relation": str(evidence_relation or "direct_user_statement"),
            "proposal_turn_index": policy.proposal_turn_index,
            "turn_references": [
                {
                    "turn_index": index,
                    "turn_id": str(
                        messages[index].get("turn_id") or f"legacy-index:{index}"
                    ),
                    "external_turn_id": messages[index].get("external_turn_id"),
                    "content_sha256": messages[index].get("turn_content_sha256"),
                    "role": str(messages[index].get("role") or "").lower(),
                    "source_kind": str(
                        messages[index].get("source_kind") or ""
                    ).lower(),
                }
                for index in sorted(cited_indexes)
                if evidence_was_provided
            ],
            "proposal_turn_id": (
                str(
                    messages[policy.proposal_turn_index].get("turn_id")
                    or f"legacy-index:{policy.proposal_turn_index}"
                )
                if policy.proposal_turn_index is not None
                else None
            ),
            "proposal": (
                {
                    "id": policy.proposal_id,
                    "group_id": policy.proposal_group_id,
                    "ordinal": policy.proposal_ordinal,
                }
                if policy.proposal_id is not None
                else None
            ),
            "authority": {
                "level": int(policy.authority),
                "label": policy.authority.name.lower(),
            },
            "validation": {
                "accepted": True,
                "reason": policy.reason,
            },
            "extraction": normalized_context,
        }

    @staticmethod
    def _valid_evidence_indexes(value: Any, message_count: int) -> set[int]:
        if not isinstance(value, list):
            return set()
        return {
            index
            for index in value
            if isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < message_count
        }

    @staticmethod
    def _is_question_only(content: str) -> bool:
        normalized = " ".join(content.lower().split())
        if not normalized:
            return False

        # A leading "when" can introduce a durable declarative preference
        # ("When you explain code, I prefer examples"), not only a question.
        # Do not discard it before the evidence gate has a chance to validate
        # the user's actual statement.
        if re.match(
            r"^when\s+(?:should|do|does|did|can|could|will|would|is|are|was|were|have|has)\b",
            normalized,
        ):
            return True
        question_starts = (
            "am ",
            "are ",
            "can ",
            "could ",
            "did ",
            "do ",
            "does ",
            "how ",
            "is ",
            "should ",
            "what ",
            "where ",
            "which ",
            "who ",
            "why ",
            "will ",
            "would ",
        )
        clauses = [
            clause.strip(" ,;:-")
            for clause in re.split(r"[.!?]+", normalized)
            if clause.strip(" ,;:-")
        ]
        if not clauses:
            return normalized.endswith("?")

        def clause_is_question(clause: str) -> bool:
            if re.match(
                r"^when\s+(?:should|do|does|did|can|could|will|would|is|are|was|were|have|has)\b",
                clause,
            ):
                return True
            return clause.startswith(question_starts)

        return all(clause_is_question(clause) for clause in clauses)

    @staticmethod
    def _significant_tokens(text: str) -> set[str]:
        stop_words = {
            "about",
            "after",
            "also",
            "and",
            "are",
            "because",
            "before",
            "from",
            "has",
            "have",
            "into",
            "its",
            "more",
            "that",
            "the",
            "their",
            "them",
            "this",
            "user",
            "with",
            "would",
        }
        tokens: list[str] = []
        current: list[str] = []
        for character in unicodedata.normalize("NFKC", text.casefold()):
            category = unicodedata.category(character)
            if category[0] in {"L", "M", "N"}:
                current.append(character)
            elif current:
                tokens.append("".join(current))
                current = []
        if current:
            tokens.append("".join(current))
        return {
            token
            for token in tokens
            if len(token) >= 3 and token not in stop_words
        }

    def _coerce_memory(
        self, raw_memory: Any
    ) -> tuple[PendingExtractedMemory | None, str | None]:
        if not isinstance(raw_memory, dict):
            return None, "invalid_shape"

        content = str(raw_memory.get("content") or "").strip()
        category = str(raw_memory.get("category") or "").strip().lower()
        reasoning = (
            str(raw_memory.get("reasoning") or "").strip()
            or "Extracted from conversation"
        )
        try:
            importance_score = float(raw_memory.get("importance_score"))
            confidence = float(raw_memory.get("confidence"))
        except (TypeError, ValueError):
            return None, "invalid_numeric_scores"

        if confidence < self._pending_confidence_threshold:
            return None, "below_pending_confidence"
        if importance_score < 1.0:
            return None, "invalid_importance"
        if category not in ALLOWED_CATEGORIES:
            return None, "invalid_category"
        if self._looks_like_temporary_session_memory(
            content=content, category=category, reasoning=reasoning
        ):
            return None, "temporary_session_directive"
        if len(content) < 10:
            return None, "content_too_short"
        if len(content) > 500:
            content = content[:500].rstrip()

        try:
            return (
                PendingExtractedMemory(
                    content=content,
                    category=category,
                    importance_score=max(1.0, min(10.0, importance_score)),
                    confidence=max(0.0, min(1.0, confidence)),
                    reasoning=reasoning,
                ),
                None,
            )
        except (TypeError, ValueError):
            return None, "schema_validation"

    @staticmethod
    def _looks_like_temporary_session_memory(
        *, content: str, category: str, reasoning: str
    ) -> bool:
        if category not in {"preference", "procedure", "goal", "fact"}:
            return False
        combined = f"{content}\n{reasoning}"
        return any(
            pattern.search(combined) for pattern in TEMPORARY_SESSION_MEMORY_PATTERNS
        )

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
            importance_rubric=cls._extract_section(
                raw_text, "## 2. Importance Scoring Rubric", "## 3."
            ),
            never_store=cls._extract_never_store(raw_text),
            examples=cls._extract_section(
                raw_text, "## 3. Example Conversations", "## 4."
            ),
        )
        cls._cached_spec = parsed
        cls._cached_spec_path = spec_path
        return parsed

    @staticmethod
    def _default_spec_path() -> Path:
        source_tree_path = (
            Path(__file__).resolve().parents[2] / "docs" / "extraction_spec.md"
        )
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
        return {
            category: definitions[category]
            for category in (
                "expertise",
                "preference",
                "goal",
                "fact",
                "procedure",
                "relationship",
            )
        }

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
        section = ExtractionService._extract_section(
            raw_text, "## 4. What Should NEVER Be Stored", "## 5."
        )
        rules = re.findall(
            r"\*\*Rule\s+\d+\s+[^*]+\*\*\s*\n(.*?)(?=\n---|\n\*\*Rule|\Z)",
            section,
            flags=re.DOTALL,
        )
        cleaned = [" ".join(rule.split())[:260] for rule in rules if rule.strip()]
        return cleaned[:20] or [
            "Never store greetings, filler, secrets, health data, one-time context, or AI-authored statements."
        ]

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for fallback_index, message in enumerate(messages):
            role = str(message.get("role") or "user").strip().lower()
            content = str(message.get("content") or "").strip()
            if content:
                turn_index = message.get("_turn_index", fallback_index)
                source_kind = str(message.get("source_kind") or "").strip().lower()
                source_label = f"[source {source_kind}]" if source_kind else ""
                proposal_label = (
                    "[registered memory proposal]"
                    if bool(
                        message.get("is_memory_proposal")
                        or message.get("_registered_memory_proposal")
                    )
                    else ""
                )
                lines.append(
                    f"[turn {turn_index}][{role}]{source_label}{proposal_label}: {content}"
                )
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

    @staticmethod
    def _truncate_to_token_budget(text: str, budget: int) -> str:
        """Enforce an input ceiling even when one retained turn is very large."""

        if budget <= 0:
            return ""
        try:
            if tiktoken is None:
                raise RuntimeError("tiktoken unavailable")
            encoding = tiktoken.get_encoding("cl100k_base")
            tokens = encoding.encode(text)
            return text if len(tokens) <= budget else encoding.decode(tokens[:budget])
        except Exception:
            return text[: budget * 4]


__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "ExtractionService",
    "ParsedExtractionSpec",
]
