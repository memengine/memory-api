from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api.db.models import SupportMemory
from api.schemas.support_schemas import SupportExtractionResult
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.claim_ledger_service import serialize_claim_value
from api.services.llm_service import LLMService
from api.services.support.pii_redaction import count_redactions
from api.services.support.pii_redaction import redact_support_pii
from api.services.support.prompt_builder import SupportPromptBuilder
from api.services.support.support_schema import active_fields_for
from api.services.support.support_type_detector import SupportTypeDetector


LOGGER = logging.getLogger(__name__)
DICT_FIELDS = {
    "customer_identity",
    "communication_preference",
    "language_profile",
    "resolution_preference",
    "support_context",
}
LIST_FIELDS = {"issue_history", "risk_signals"}
SCALAR_FIELDS = {"sentiment_pattern"}
SUPPORT_CLAIM_CATEGORIES = {
    "support_type": "fact",
    "support_type_source": "fact",
    "customer_identity": "fact",
    "communication_preference": "preference",
    "language_profile": "preference",
    "current_open_issue": "goal",
    "issue_history": "fact",
    "resolution_preference": "preference",
    "sentiment_pattern": "fact",
    "risk_signals": "fact",
    "support_context": "fact",
}
MAX_ISSUE_HISTORY = 50
MAX_RISK_SIGNALS = 25
DEFAULT_SUPPORT_TYPES = [
    "saas",
    "ecommerce",
    "banking_fintech",
    "travel",
    "telecom",
    "edtech_support",
    "general_info",
]


class SupportExtractionError(RuntimeError):
    pass


class SupportExtractor:
    def __init__(
        self,
        *,
        session: Session,
        llm_service: LLMService | None = None,
        prompt_builder: SupportPromptBuilder | None = None,
        client: Any | None = None,
    ) -> None:
        self.session = session
        self.llm_service = llm_service or LLMService(
            provider_clients=None,
            require_provider=client is None,
            use_state_store=client is None,
        )
        self.prompt_builder = prompt_builder or SupportPromptBuilder()

    def extract_and_merge_sync(
        self,
        *,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
        tenant_configured_type: str | None = None,
        support_type_mode: str = "single",
        allowed_support_types: list[str] | None = None,
    ) -> SupportExtractionResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.extract_and_merge(
                    messages=messages,
                    proxy_user_id=proxy_user_id,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    tenant_configured_type=tenant_configured_type,
                    support_type_mode=support_type_mode,
                    allowed_support_types=allowed_support_types,
                )
            )
        raise RuntimeError(
            "extract_and_merge_sync cannot be called from a running event loop."
        )

    async def extract_and_merge(
        self,
        *,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
        tenant_configured_type: str | None = None,
        support_type_mode: str = "single",
        allowed_support_types: list[str] | None = None,
    ) -> SupportExtractionResult:
        tenant_uuid = uuid.UUID(str(tenant_id))
        proxy_uuid = uuid.UUID(str(proxy_user_id))
        existing = self.session.execute(
            select(SupportMemory).where(
                SupportMemory.proxy_user_id == proxy_uuid,
                SupportMemory.tenant_id == tenant_uuid,
            )
        ).scalar_one_or_none()

        routing = _normalize_routing(
            support_type_mode=support_type_mode,
            tenant_configured_type=tenant_configured_type,
            allowed_support_types=allowed_support_types,
            existing_support_type=_existing_support_type(existing),
        )
        detection = SupportTypeDetector().detect_result(
            messages,
            tenant_configured_type=routing["fixed_support_type"],
            allowed_support_types=routing["allowed_support_types"],
        )
        conversation = _build_conversation(messages)
        prompt = self.prompt_builder.build_prompt(
            conversation=conversation,
            support_type=detection.support_type,
            support_type_mode=str(routing["support_type_mode"]),
            allowed_support_types=list(routing["allowed_support_types"]),
            existing_memory_compressed=self.prompt_builder.compress_existing_memory(
                existing
            ),
            active_fields=active_fields_for(detection.support_type),
        )
        tokens_used = 0
        provider_used = "none"
        try:
            response = await self.llm_service.complete(
                system_prompt=prompt,
                user_message="Extract durable customer support memory from the conversation above.",
                temperature=0.0,
                max_tokens=1200,
                response_format="json",
            )
            tokens_used = int(response.total_tokens or 0)
            provider_used = response.provider_used
            data = _parse_response(response.content)
        except Exception as exc:
            LOGGER.warning(
                "support_extraction_llm_failed",
                extra={
                    "event": "support_extraction_llm_failed",
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
            return SupportExtractionResult(
                fields_updated=[],
                nothing_to_extract=True,
                tokens_used=tokens_used,
                provider_used=provider_used,
                support_type=detection.support_type,
                support_type_source=detection.source,
                support_type_confidence=detection.confidence,
            )

        detected_type, detected_source, detected_confidence = (
            _support_type_from_response(
                data=data,
                fallback_type=detection.support_type,
                fallback_source=detection.source,
                fallback_confidence=detection.confidence,
                support_type_mode=str(routing["support_type_mode"]),
                allowed_support_types=list(routing["allowed_support_types"]),
            )
        )
        extracted = data.get("extracted") or {}
        if extracted and not isinstance(extracted, dict):
            raise SupportExtractionError(
                "Support extraction response field 'extracted' must be an object."
            )

        redactions = count_redactions(extracted, support_type=detected_type)
        extracted = redact_support_pii(extracted, support_type=detected_type)
        if redactions:
            LOGGER.warning(
                "support_pii_redacted",
                extra={
                    "event": "support_pii_redacted",
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "redactions_count": redactions,
                },
            )

        if data.get("nothing_to_extract") and not extracted:
            return SupportExtractionResult(
                fields_updated=[],
                nothing_to_extract=True,
                tokens_used=tokens_used,
                provider_used=provider_used,
                support_type=detected_type,
                support_type_source=detected_source,
                support_type_confidence=detected_confidence,
                redactions_count=redactions,
            )

        memory = existing or SupportMemory(
            id=uuid.uuid4(), proxy_user_id=proxy_uuid, tenant_id=tenant_uuid
        )
        previous_field_values = {
            field: copy.deepcopy(getattr(memory, field, None))
            for field in SUPPORT_CLAIM_CATEGORIES
        }
        fields_updated: set[str] = set()
        memory.support_type = detected_type
        memory.support_type_source = detected_source
        fields_updated.update({"support_type", "support_type_source"})
        self._merge_extracted(memory, extracted, fields_updated)

        memory.last_extraction_at = datetime.now(UTC)
        memory.updated_at = datetime.now(UTC)
        existing_job_ids = list(memory.extraction_source_job_ids or [])
        try:
            parsed_job_id = uuid.UUID(str(job_id))
            if parsed_job_id not in existing_job_ids:
                existing_job_ids.append(parsed_job_id)
        except (TypeError, ValueError):
            pass
        memory.extraction_source_job_ids = existing_job_ids
        claims = self._record_field_claims(
            memory,
            fields_updated=fields_updated,
            job_id=job_id,
        )
        self._restore_non_winning_fields(
            memory,
            claims=claims,
            previous_field_values=previous_field_values,
        )
        self._upsert_memory(memory)

        return SupportExtractionResult(
            fields_updated=sorted(fields_updated),
            nothing_to_extract=False,
            tokens_used=tokens_used,
            provider_used=provider_used,
            support_type=detected_type,
            support_type_source=detected_source,
            support_type_confidence=detected_confidence,
            redactions_count=redactions,
        )

    def _record_field_claims(
        self,
        memory: SupportMemory,
        *,
        fields_updated: set[str],
        job_id: str,
    ) -> None:
        try:
            ClaimLedgerService(self.session).record_domain_fields(
                domain_record=memory,
                domain="support",
                fields_updated=fields_updated,
                field_categories=SUPPORT_CLAIM_CATEGORIES,
                job_id=job_id,
            )
        except Exception:
            LOGGER.exception(
                "support_claim_ledger_write_failed",
                extra={
                    "event": "support_claim_ledger_write_failed",
                    "tenant_id": str(memory.tenant_id),
                    "proxy_user_id": str(memory.proxy_user_id),
                    "job_id": job_id,
                },
            )
            return []

    @staticmethod
    def _restore_non_winning_fields(
        memory: SupportMemory,
        *,
        claims: list[Any],
        previous_field_values: dict[str, Any],
    ) -> None:
        for claim in claims:
            field = str(claim.predicate_key).removeprefix("support.")
            current_value = serialize_claim_value(getattr(memory, field, None))
            if claim.active_value != current_value and field in previous_field_values:
                setattr(memory, field, previous_field_values[field])

    def _merge_extracted(
        self,
        memory: SupportMemory,
        extracted: dict[str, Any],
        fields_updated: set[str],
    ) -> None:
        for field in DICT_FIELDS:
            value = extracted.get(field)
            if isinstance(value, dict) and value:
                current = dict(getattr(memory, field) or {})
                setattr(memory, field, _deep_merge(current, value))
                fields_updated.add(field)

        current_open_issue = extracted.get("current_open_issue")
        if isinstance(current_open_issue, dict) and current_open_issue:
            memory.current_open_issue = current_open_issue
            fields_updated.add("current_open_issue")

        for field in LIST_FIELDS:
            value = extracted.get(field)
            if isinstance(value, list) and value:
                limit = (
                    MAX_ISSUE_HISTORY if field == "issue_history" else MAX_RISK_SIGNALS
                )
                setattr(
                    memory,
                    field,
                    _append_unique(getattr(memory, field) or [], value, limit=limit),
                )
                fields_updated.add(field)

        for field in SCALAR_FIELDS:
            value = extracted.get(field)
            if isinstance(value, str) and value.strip():
                setattr(memory, field, value.strip())
                fields_updated.add(field)

    def _upsert_memory(self, memory: SupportMemory) -> None:
        values = {
            "id": memory.id,
            "proxy_user_id": memory.proxy_user_id,
            "tenant_id": memory.tenant_id,
            "support_type": memory.support_type,
            "support_type_source": memory.support_type_source or "detected",
            "customer_identity": memory.customer_identity or {},
            "communication_preference": memory.communication_preference or {},
            "language_profile": memory.language_profile or {},
            "current_open_issue": memory.current_open_issue,
            "issue_history": memory.issue_history or [],
            "resolution_preference": memory.resolution_preference or {},
            "sentiment_pattern": memory.sentiment_pattern,
            "risk_signals": memory.risk_signals or [],
            "support_context": memory.support_context or {},
            "schema_version": memory.schema_version or 1,
            "last_extraction_at": memory.last_extraction_at,
            "extraction_source_job_ids": memory.extraction_source_job_ids or [],
            "updated_at": datetime.now(UTC),
        }
        insert_stmt = pg_insert(SupportMemory).values(**values)
        update_values = {
            key: value
            for key, value in values.items()
            if key not in {"id", "proxy_user_id", "tenant_id"}
        }
        stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_support_memories_proxy_tenant",
            set_=update_values,
        )
        self.session.execute(stmt)
        self.session.flush()


def _existing_support_type(memory: SupportMemory | None) -> str | None:
    return memory.support_type if memory is not None else None


def _normalize_routing(
    *,
    support_type_mode: str,
    tenant_configured_type: str | None,
    allowed_support_types: list[str] | None,
    existing_support_type: str | None,
) -> dict[str, Any]:
    mode = (
        support_type_mode
        if support_type_mode in {"single", "multi", "auto"}
        else "single"
    )
    allowed = [
        item for item in (allowed_support_types or []) if item in DEFAULT_SUPPORT_TYPES
    ]
    if mode == "single":
        fixed = (
            tenant_configured_type
            if tenant_configured_type in DEFAULT_SUPPORT_TYPES
            else existing_support_type
        )
        fixed = fixed if fixed in DEFAULT_SUPPORT_TYPES else "general_info"
        return {
            "support_type_mode": "single",
            "fixed_support_type": fixed,
            "allowed_support_types": [fixed],
        }
    if mode == "multi":
        if not allowed and tenant_configured_type in DEFAULT_SUPPORT_TYPES:
            allowed = [tenant_configured_type]
        allowed = [
            item for item in allowed if item in DEFAULT_SUPPORT_TYPES
        ] or DEFAULT_SUPPORT_TYPES
        return {
            "support_type_mode": "multi",
            "fixed_support_type": None,
            "allowed_support_types": allowed,
        }
    return {
        "support_type_mode": "auto",
        "fixed_support_type": None,
        "allowed_support_types": DEFAULT_SUPPORT_TYPES,
    }


def _support_type_from_response(
    *,
    data: dict[str, Any],
    fallback_type: str,
    fallback_source: str,
    fallback_confidence: float,
    support_type_mode: str,
    allowed_support_types: list[str],
) -> tuple[str, str, float]:
    if support_type_mode == "single":
        return fallback_type, "tenant_configured", 1.0

    candidate = str(data.get("support_type") or "").strip()
    confidence = _coerce_confidence(
        data.get("support_type_confidence"), fallback_confidence
    )
    if candidate in allowed_support_types and confidence >= 0.75:
        return candidate, "allowed_detected", confidence

    if "general_info" in allowed_support_types and confidence < 0.75:
        return "general_info", "allowed_detected", confidence
    return (
        fallback_type,
        fallback_source
        if fallback_source != "tenant_configured"
        else "allowed_detected",
        fallback_confidence,
    )


def _coerce_confidence(value: Any, fallback: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return fallback
    if confidence > 1:
        confidence = confidence / 100.0
    return max(0.0, min(1.0, confidence))


def _build_conversation(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages[-24:]:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or message.get("parts") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _parse_response(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SupportExtractionError(
            "Support extraction response was not valid JSON."
        ) from exc
    if not isinstance(data, dict):
        raise SupportExtractionError(
            "Support extraction response must be a JSON object."
        )
    return data


def _deep_merge(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _append_unique(current: list[Any], incoming: list[Any], *, limit: int) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for item in [*current, *incoming]:
        if item in (None, "", [], {}):
            continue
        key = (
            json.dumps(item, sort_keys=True, default=str)
            if isinstance(item, (dict, list))
            else str(item)
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output[-limit:]


__all__ = [
    "SupportExtractionError",
    "SupportExtractor",
    "_normalize_routing",
    "_support_type_from_response",
]
