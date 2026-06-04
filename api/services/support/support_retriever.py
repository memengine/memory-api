from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import SupportMemory
from api.schemas.support_schemas import SupportRetrieveResult

try:  # pragma: no cover
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


SUPPORT_MEMORY_USAGE_RULES = """Support memory usage rules:
- Treat this as remembered customer context, not live system truth.
- Do not claim you checked, changed, refunded, cancelled, verified, located, or triggered anything unless your own tool/API result confirms it.
- If live system data is needed, say you need to check the relevant support system or call the appropriate tool."""

BANKING_FINTECH_USAGE_RULE = (
    "- Never ask for or repeat OTPs, passwords, full account numbers, full card numbers, Aadhaar, or PAN values."
)


class SupportRetriever:
    CACHE_TTL_SECONDS = 300
    SUPPORT_TYPE_INTROS = {
        "saas": "Customer account context:",
        "ecommerce": "Customer context:",
        "banking_fintech": "Customer account context:",
        "travel": "Traveller context:",
        "telecom": "Subscriber context:",
        "edtech_support": "Student account context:",
        "general_info": "Visitor context:",
    }

    def __init__(self, *, session: AsyncSession, cache_service: CacheService | None = None) -> None:
        self.session = session
        self.cache_service = cache_service

    async def get_for_customer(
        self,
        proxy_user_id: str,
        tenant_id: str,
        query: str | None = None,
        max_tokens: int = 600,
    ) -> SupportRetrieveResult:
        query_key = str(abs(hash((query or "").lower())) % 1_000_000)
        cache_key = f"support:{tenant_id}:{proxy_user_id}:{max_tokens}:{query_key}"
        if self.cache_service is not None:
            cached = await self.cache_service._get_json(cache_key)
            if cached:
                return SupportRetrieveResult(**cached)

        result = await self.session.execute(
            select(SupportMemory).where(
                SupportMemory.proxy_user_id == uuid.UUID(str(proxy_user_id)),
                SupportMemory.tenant_id == uuid.UUID(str(tenant_id)),
            )
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            return SupportRetrieveResult(system_prompt_addition="", context_token_count=0)

        prompt = self.build_system_prompt_addition(memory, query=query, max_tokens=max_tokens)
        output = SupportRetrieveResult(
            system_prompt_addition=prompt,
            context_token_count=_token_count(prompt),
        )
        if self.cache_service is not None:
            await self.cache_service._set_json(cache_key, asdict(output), ttl=self.CACHE_TTL_SECONDS)
        return output

    def build_system_prompt_addition(
        self,
        memory: SupportMemory,
        query: str | None = None,
        max_tokens: int = 600,
    ) -> str:
        safety_text = self._safety_section(memory)
        critical = [
            safety_text,
            self.SUPPORT_TYPE_INTROS.get(memory.support_type or "general_info", "Customer context:"),
            self._current_issue_section(memory),
            self._customer_identity_section(memory),
            self._sentiment_warning_section(memory),
            self._critical_risk_section(memory),
        ]
        normal = [
            self._support_context_section(memory),
            self._resolution_preference_section(memory, query=query),
            self._communication_section(memory),
            self._issue_history_summary(memory),
        ]
        critical_text = "\n\n".join(section for section in critical if section)
        normal_text = "\n\n".join(section for section in normal if section)
        prompt = "\n\n".join(part for part in [critical_text, normal_text] if part)
        return _fit_token_limit(prompt, max_tokens, pinned=critical_text)

    def _safety_section(self, memory: SupportMemory) -> str:
        if memory.support_type == "banking_fintech":
            return SUPPORT_MEMORY_USAGE_RULES + "\n" + BANKING_FINTECH_USAGE_RULE
        return SUPPORT_MEMORY_USAGE_RULES

    def _current_issue_section(self, memory: SupportMemory) -> str:
        issue = memory.current_open_issue or {}
        if not isinstance(issue, dict) or not issue:
            return ""
        issue_type = issue.get("issue_type") or issue.get("type") or "open issue"
        ref = issue.get("ref") or issue.get("ticket_ref") or "no ref"
        since = issue.get("since") or issue.get("last_update") or "unknown date"
        urgency = issue.get("urgency") or issue.get("status") or "needs follow-up"
        summary = issue.get("summary")
        line = f"Open issue: {issue_type} (Ref: {ref}) since {since} - {urgency}"
        if summary:
            line += f"\n - {summary}"
        return line

    def _customer_identity_section(self, memory: SupportMemory) -> str:
        identity = memory.customer_identity or {}
        if not identity:
            return ""
        name = identity.get("name") or "Customer"
        tier = identity.get("tier") or identity.get("customer_tier") or "standard"
        since = identity.get("customer_since") or identity.get("since")
        account = identity.get("account_ref")
        parts = [str(name), f"{tier} customer"]
        if since:
            parts.append(f"since {since}")
        if account:
            parts.append(f"account {account}")
        return "Customer identity:\n - " + " | ".join(parts)

    def _sentiment_warning_section(self, memory: SupportMemory) -> str:
        if memory.sentiment_pattern == "high_escalation_risk":
            return "High escalation risk - handle carefully and avoid asking them to repeat context."
        if memory.sentiment_pattern == "repeat_complainer":
            return "Repeat support contact - acknowledge prior attempts and focus on resolution."
        return ""

    def _critical_risk_section(self, memory: SupportMemory) -> str:
        risks = [
            item
            for item in memory.risk_signals or []
            if isinstance(item, dict)
            and (
                str(item.get("risk_type") or "").lower() in {"fraud", "compliance", "legal", "safety"}
                or str(item.get("severity") or "").lower() in {"critical", "high"}
            )
        ]
        if not risks:
            return ""
        lines = ["Critical support risks:"]
        for item in risks[:5]:
            risk_type = item.get("risk_type") or "risk"
            reason = item.get("reason") or item.get("summary") or ""
            status = item.get("status") or ""
            lines.append(f" - {risk_type}: {reason} {status}".strip())
        return "\n".join(lines)

    def _support_context_section(self, memory: SupportMemory) -> str:
        context = memory.support_context or {}
        if not context:
            return ""
        title_by_type = {
            "saas": "SaaS account context:",
            "ecommerce": "Order/support context:",
            "banking_fintech": "Banking support context:",
            "travel": "Booking context:",
            "telecom": "Telecom context:",
            "edtech_support": "Course support context:",
            "general_info": "Visitor intent context:",
        }
        return f"{title_by_type.get(memory.support_type or '', 'Support context:')}\n - {json.dumps(context, ensure_ascii=True, default=str)}"

    def _resolution_preference_section(self, memory: SupportMemory, *, query: str | None) -> str:
        preference = memory.resolution_preference or {}
        if not preference:
            return ""
        method = str(preference.get("method") or "").lower()
        query_text = (query or "").lower()
        if method and method not in query_text and not any(word in query_text for word in ["resolve", "fix", "issue"]):
            return ""
        return "Resolution preference:\n - " + json.dumps(preference, ensure_ascii=True, default=str)

    def _communication_section(self, memory: SupportMemory) -> str:
        bits = []
        if memory.communication_preference:
            bits.append(f"communication={json.dumps(memory.communication_preference, ensure_ascii=True, default=str)}")
        if memory.language_profile:
            bits.append(f"language={json.dumps(memory.language_profile, ensure_ascii=True, default=str)}")
        if not bits:
            return ""
        return "Communication style:\n - " + "\n - ".join(bits)

    def _issue_history_summary(self, memory: SupportMemory) -> str:
        issue_count = len(memory.issue_history or [])
        if issue_count == 0:
            return ""
        return f"Past support history: {issue_count} resolved or previous issues. Do not list all unless directly relevant."


def _token_count(text: str) -> int:
    if not text:
        return 0
    if tiktoken is None:
        return int(len(text.split()) * 1.3)
    try:
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return int(len(text.split()) * 1.3)


def _fit_token_limit(text: str, max_tokens: int, *, pinned: str) -> str:
    if _token_count(text) <= max_tokens:
        return text
    if _token_count(pinned) >= max_tokens:
        return pinned
    lines = text.splitlines()
    while len(lines) > 3 and _token_count("\n".join(lines)) > max_tokens:
        lines.pop()
    return "\n".join(lines)


__all__ = ["SupportRetriever"]
