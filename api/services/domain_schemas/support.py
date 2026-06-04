from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from api.db.models import Tenant
from api.services.domain_schemas.base import BaseDomainSchema
from api.services.support.support_extractor import SupportExtractor
from api.services.support.support_retriever import SupportRetriever


class SupportDomainSchema(BaseDomainSchema):
    def get_domain(self) -> str:
        return "support"

    def extract_overlay_sync(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
        agent_id: str | None = None,
        client: Any | None = None,
    ) -> dict[str, Any]:
        support_config = self._tenant_support_config(session=session, tenant_id=tenant_id)
        result = SupportExtractor(session=session, client=client).extract_and_merge_sync(
            messages=messages,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            job_id=job_id,
            tenant_configured_type=support_config["support_type_configured"],
            support_type_mode=support_config["support_type_mode"],
            allowed_support_types=support_config["support_types_allowed"],
        )
        return {
            "support_fields_updated": result.fields_updated,
            "support_nothing_to_extract": result.nothing_to_extract,
            "support_tokens_used": result.tokens_used,
            "support_provider_used": result.provider_used,
            "support_type": result.support_type,
            "support_type_source": result.support_type_source,
            "support_type_confidence": result.support_type_confidence,
            "support_redactions_count": result.redactions_count,
        }

    @staticmethod
    def _tenant_support_config(*, session: Any, tenant_id: str) -> dict[str, Any]:
        tenant = session.execute(select(Tenant).where(Tenant.id == uuid.UUID(str(tenant_id)))).scalar_one_or_none()
        if tenant is None:
            return {"support_type_configured": None, "support_type_mode": "auto", "support_types_allowed": []}
        return {
            "support_type_configured": tenant.support_type_configured,
            "support_type_mode": tenant.support_type_mode or "single",
            "support_types_allowed": list(tenant.support_types_allowed or []),
        }

    async def build_retrieve_context(
        self,
        *,
        session: Any,
        cache_service: Any | None,
        proxy_user_id: str,
        tenant_id: str,
        query: str | None,
        max_tokens: int,
    ) -> tuple[str, int]:
        result = await SupportRetriever(session=session, cache_service=cache_service).get_for_customer(
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            query=query,
            max_tokens=max_tokens,
        )
        return result.system_prompt_addition, result.context_token_count


__all__ = ["SupportDomainSchema"]
