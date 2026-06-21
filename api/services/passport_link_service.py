from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import GlobalAgent
from api.db.models import ProxyUser
from api.errors import APIError
from api.services.organisation_connection_service import OrganisationConnectionService
from api.services.proxy_user_service import ProxyUserService


PASSPORT_LINK_TTL_SECONDS = 15 * 60
PASSPORT_LINK_PREFIX = "passport_link"


@dataclass(slots=True)
class PassportLinkToken:
    token: str
    expires_in_seconds: int


class PassportLinkService:
    def __init__(self, *, session: AsyncSession, cache_service: CacheService) -> None:
        self.session = session
        self.cache_service = cache_service

    async def issue(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        external_user_id: str,
    ) -> PassportLinkToken:
        tenant_uuid = uuid.UUID(str(tenant_id))
        agent = await self.session.get(GlobalAgent, uuid.UUID(str(agent_id)))
        if agent is None or agent.owner_tenant_id != tenant_uuid or not agent.is_active:
            raise APIError(status_code=404, code="AGN_404", error="global_agent_not_found")

        external_user_id_hash = ProxyUserService.hash_external_user_id(
            tenant_id,
            external_user_id,
        )
        proxy_user = (
            await self.session.execute(
                select(ProxyUser).where(
                    ProxyUser.tenant_id == tenant_uuid,
                    ProxyUser.external_user_id_hash == external_user_id_hash,
                )
            )
        ).scalar_one_or_none()
        if proxy_user is None:
            raise APIError(status_code=404, code="USR_404", error="proxy_user_not_found")

        raw_token = f"plink_{secrets.token_urlsafe(32)}"
        payload = json.dumps(
            {
                "tenant_id": str(tenant_uuid),
                "agent_id": str(agent.id),
                "proxy_user_id": str(proxy_user.id),
            },
            separators=(",", ":"),
        )
        stored = await self.cache_service.client.set(
            self._key(raw_token),
            payload,
            ex=PASSPORT_LINK_TTL_SECONDS,
            nx=True,
        )
        if not stored:
            raise APIError(status_code=503, code="PLINK_503", error="link_token_unavailable")
        return PassportLinkToken(
            token=raw_token,
            expires_in_seconds=PASSPORT_LINK_TTL_SECONDS,
        )

    async def consume(
        self,
        *,
        token: str,
        agent_id: str,
        user_uui_id: str,
    ):
        raw_payload = await self.cache_service.client.getdel(self._key(token))
        if not raw_payload:
            raise APIError(
                status_code=410,
                code="PLINK_410",
                error="passport_link_expired_or_used",
            )
        try:
            payload = json.loads(raw_payload)
            token_agent_id = uuid.UUID(str(payload["agent_id"]))
            tenant_id = uuid.UUID(str(payload["tenant_id"]))
            proxy_user_id = uuid.UUID(str(payload["proxy_user_id"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise APIError(
                status_code=422,
                code="PLINK_422",
                error="invalid_passport_link",
            ) from exc

        if token_agent_id != uuid.UUID(str(agent_id)):
            raise APIError(
                status_code=403,
                code="PLINK_403",
                error="passport_link_agent_mismatch",
            )

        user_uuid = uuid.UUID(str(user_uui_id))
        link = await OrganisationConnectionService(
            session=self.session,
            cache_service=self.cache_service,
        ).connect_proxy_user(
            universal_user_id=user_uuid,
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            method="link_token",
        )
        return link

    @staticmethod
    def _key(token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"{PASSPORT_LINK_PREFIX}:{digest}"
