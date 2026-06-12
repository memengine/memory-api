from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ApiKey
from api.db.models import MemorySourceEvent
from api.db.models import ServiceWriter
from api.errors import APIError


def payload_sha256(messages: list[dict[str, str]]) -> str:
    canonical = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_provenance_snapshot(event: MemorySourceEvent) -> dict[str, Any]:
    return {
        "source_event_id": str(event.id),
        "event_id": event.source_event_id,
        "service": event.source_service,
        "writer_id": str(event.writer_id) if event.writer_id else None,
        "authority_rules": (
            dict(event.writer.authority_rules or {})
            if event.writer is not None
            else {}
        ),
        "observed_at": event.observed_at.isoformat(),
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "payload_hash": event.payload_hash,
        "scope": event.scope or {},
        "evidence": event.evidence_refs or [],
        "processing": event.processing_metadata or {},
    }


class ProvenanceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve_writer(
        self,
        *,
        tenant_id: str,
        api_key_id: str | None,
        requested_service: str | None,
    ) -> ServiceWriter | None:
        tenant_uuid = uuid.UUID(str(tenant_id))
        writer: ServiceWriter | None = None

        if requested_service:
            writer = (
                await self.session.execute(
                    select(ServiceWriter).where(
                        ServiceWriter.tenant_id == tenant_uuid,
                        ServiceWriter.service_key == requested_service,
                    )
                )
            ).scalar_one_or_none()
            if writer is None or not writer.is_active:
                raise APIError(
                    status_code=422,
                    code="PROV_422",
                    error="source_service_not_registered",
                    details={"service": requested_service},
                )
        elif api_key_id:
            writer = (
                await self.session.execute(
                    select(ServiceWriter).where(
                        ServiceWriter.tenant_id == tenant_uuid,
                        ServiceWriter.api_key_id == uuid.UUID(str(api_key_id)),
                        ServiceWriter.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()

        if writer is not None and writer.api_key_id is not None:
            if api_key_id is None or str(writer.api_key_id) != str(api_key_id):
                raise APIError(
                    status_code=403,
                    code="PROV_403",
                    error="source_service_credential_mismatch",
                )
        return writer

    async def validate_api_key(self, *, tenant_id: str, api_key_id: str | None) -> ApiKey | None:
        if api_key_id is None:
            return None
        api_key = await self.session.get(ApiKey, uuid.UUID(str(api_key_id)))
        if api_key is None or str(api_key.tenant_id) != str(tenant_id):
            raise APIError(status_code=422, code="PROV_422", error="invalid_writer_api_key")
        return api_key

    @staticmethod
    def normalize_source(
        *,
        source: dict[str, Any] | None,
        writer: ServiceWriter | None,
        api_key_id: str | None,
        job_id: str,
    ) -> dict[str, Any]:
        source = source or {}
        service = str(
            source.get("service")
            or (writer.service_key if writer is not None else f"api-key-{str(api_key_id or 'legacy')[:12]}")
        )
        event_id = str(source.get("event_id") or job_id)
        observed_at = source.get("observed_at") or datetime.now(UTC)
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        evidence = [
            {
                "source_type": str(item.get("source_type")),
                "reference": str(item.get("reference")),
                "content_hash": item.get("content_hash"),
            }
            for item in list(source.get("evidence") or [])
        ]
        return {
            "service": service,
            "event_id": event_id,
            "observed_at": observed_at,
            "scope": dict(source.get("scope") or {}),
            "evidence": evidence,
        }
