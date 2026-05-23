from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from api.db.models import EdTechMemory
from api.services.domain_projection_service import DomainProjectionService
from api.services.domain_schemas.base import BaseDomainSchema
from api.services.edtech.edtech_extractor import EdTechExtractor
from api.services.edtech.projections import build_edtech_universal_projections
from api.services.edtech.edtech_retriever import EdTechRetriever


class EdTechDomainSchema(BaseDomainSchema):
    def get_domain(self) -> str:
        return "edtech"

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
        result = EdTechExtractor(session=session, client=client).extract_and_merge_sync(
            messages=messages,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            job_id=job_id,
        )
        projection_meta = self._project_portable_memories(
            session=session,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        return {
            "edtech_fields_updated": result.fields_updated,
            "edtech_conflicts_resolved": result.conflicts_resolved,
            "edtech_nothing_to_extract": result.nothing_to_extract,
            "edtech_tokens_used": result.tokens_used,
            "edtech_provider_used": result.provider_used,
            **projection_meta,
        }

    @staticmethod
    def _project_portable_memories(
        *,
        session: Any,
        proxy_user_id: str,
        tenant_id: str,
        agent_id: str | None,
    ) -> dict[str, Any]:
        try:
            parsed_proxy_user_id = uuid.UUID(str(proxy_user_id))
            parsed_tenant_id = uuid.UUID(str(tenant_id))
        except (TypeError, ValueError):
            return {"edtech_universal_projection_skipped": "invalid_identity"}

        memory = session.execute(
            select(EdTechMemory).where(
                EdTechMemory.proxy_user_id == parsed_proxy_user_id,
                EdTechMemory.tenant_id == parsed_tenant_id,
            )
        ).scalar_one_or_none()
        if memory is None:
            return {"edtech_universal_projection_skipped": "no_edtech_memory"}

        projections = build_edtech_universal_projections(memory)
        projection_result = DomainProjectionService().project_to_universal_sync(
            session=session,
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            agent_id=agent_id,
            projections=projections,
        )
        return {
            "edtech_universal_projections_created": projection_result.created,
            "edtech_universal_projections_updated": projection_result.updated,
            "edtech_universal_projections_unchanged": projection_result.unchanged,
            "edtech_universal_projections_skipped": projection_result.skipped,
            "edtech_universal_projection_skip_reason": projection_result.skipped_reason,
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
        result = await EdTechRetriever(
            session=session,
            cache_service=cache_service,
        ).get_for_student(
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            query=query,
            max_tokens=max_tokens,
        )
        return result.system_prompt_addition, result.context_token_count


__all__ = ["EdTechDomainSchema"]
