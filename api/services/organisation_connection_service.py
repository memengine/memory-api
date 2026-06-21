from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC
from datetime import datetime
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import OrganisationDirectory
from api.db.models import ProxyUser
from api.db.models import Tenant
from api.db.models import UUIProxyLink
from api.db.models import UniversalMemory
from api.db.models import VerifiedOrgConnection
from api.errors import APIError
from api.services.proxy_user_service import ProxyUserService
from api.settings import get_settings


OAUTH_STATE_TTL_SECONDS = 10 * 60
OAUTH_STATE_PREFIX = "passport_oauth_state"


class OrganisationCredentialCipher:
    @staticmethod
    def _fernet() -> Fernet:
        key = get_settings().oauth_credential_encryption_key.strip()
        if not key:
            raise APIError(
                status_code=503,
                code="ORG_OAUTH_503",
                error="oauth_credential_encryption_not_configured",
            )
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise APIError(
                status_code=503,
                code="ORG_OAUTH_503",
                error="oauth_credential_encryption_invalid",
            ) from exc

    @classmethod
    def encrypt(cls, value: str) -> str:
        return cls._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    @classmethod
    def decrypt(cls, value: str) -> str:
        try:
            return cls._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise APIError(
                status_code=503,
                code="ORG_OAUTH_503",
                error="oauth_credential_decryption_failed",
            ) from exc


class OrganisationConnectionService:
    def __init__(self, *, session: AsyncSession, cache_service: CacheService) -> None:
        self.session = session
        self.cache_service = cache_service

    async def ensure_private_directory(self, tenant_id: uuid.UUID) -> OrganisationDirectory:
        existing = (
            await self.session.execute(
                select(OrganisationDirectory).where(OrganisationDirectory.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
        directory = OrganisationDirectory(
            tenant_id=tenant.id,
            display_name=str(getattr(tenant, "company_name", None) or "Organisation"),
            category="other",
            oauth_enabled=False,
            link_token_enabled=True,
            is_public=False,
            is_verified=False,
        )
        self.session.add(directory)
        await self.session.flush()
        return directory

    async def upsert_connection(
        self,
        *,
        universal_user_id: uuid.UUID,
        organisation: OrganisationDirectory,
        method: str,
        external_account_id: str | None = None,
        proxy_user_id: uuid.UUID | None = None,
    ) -> VerifiedOrgConnection:
        connection = (
            await self.session.execute(
                select(VerifiedOrgConnection).where(
                    VerifiedOrgConnection.user_uui_id == universal_user_id,
                    VerifiedOrgConnection.org_directory_id == organisation.id,
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        external_ref = (
            hashlib.sha256(f"{organisation.id}:{external_account_id}".encode("utf-8")).hexdigest()
            if external_account_id
            else None
        )
        if connection is None:
            connection = VerifiedOrgConnection(
                user_uui_id=universal_user_id,
                tenant_id=organisation.tenant_id,
                org_directory_id=organisation.id,
                proxy_user_id=proxy_user_id,
                connection_method=method,
                external_account_ref=external_ref,
                verified_at=now,
                last_verified_at=now,
                is_active=True,
            )
            self.session.add(connection)
        else:
            connection.proxy_user_id = proxy_user_id or connection.proxy_user_id
            connection.connection_method = method
            connection.external_account_ref = external_ref or connection.external_account_ref
            connection.last_verified_at = now
            connection.is_active = True
            connection.revoked_at = None
            connection.revoked_by = None
        await self.session.flush()
        return connection

    async def connect_proxy_user(
        self,
        *,
        universal_user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
        method: str,
    ) -> UUIProxyLink:
        organisation = await self.ensure_private_directory(tenant_id)
        link = (
            await self.session.execute(
                select(UUIProxyLink).where(UUIProxyLink.proxy_user_id == proxy_user_id)
            )
        ).scalar_one_or_none()
        if link is not None and link.user_uui_id != universal_user_id:
            raise APIError(status_code=409, code="PLINK_409", error="proxy_user_already_linked")
        if link is None:
            link = UUIProxyLink(
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                user_uui_id=universal_user_id,
            )
            self.session.add(link)
        await self.upsert_connection(
            universal_user_id=universal_user_id,
            organisation=organisation,
            method=method,
            proxy_user_id=proxy_user_id,
        )
        return link

    async def initiate_oauth(
        self,
        *,
        universal_user_id: uuid.UUID,
        organisation_id: uuid.UUID,
        redirect_uri: str,
    ) -> str:
        organisation = await self.session.get(OrganisationDirectory, organisation_id)
        if organisation is None or not organisation.is_public:
            raise APIError(status_code=404, code="ORG_404", error="organisation_not_found")
        required = (
            organisation.oauth_enabled,
            organisation.oauth_client_id,
            organisation.oauth_client_secret_ciphertext,
            organisation.oauth_authorization_url,
            organisation.oauth_token_url,
            organisation.oauth_userinfo_url,
        )
        if not all(required):
            raise APIError(status_code=409, code="ORG_OAUTH_409", error="organisation_oauth_not_available")

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        payload = json.dumps(
            {
                "user_uui_id": str(universal_user_id),
                "organisation_id": str(organisation.id),
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "initiated_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        stored = await self.cache_service.client.set(
            f"{OAUTH_STATE_PREFIX}:{state}",
            payload,
            ex=OAUTH_STATE_TTL_SECONDS,
            nx=True,
        )
        if not stored:
            raise APIError(status_code=503, code="ORG_OAUTH_503", error="oauth_state_unavailable")

        query = urlencode(
            {
                "client_id": organisation.oauth_client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(organisation.oauth_scopes or []),
                "state": state,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{organisation.oauth_authorization_url}?{query}"

    async def complete_oauth(self, *, code: str, state: str) -> OrganisationDirectory:
        raw_state = await self.cache_service.client.getdel(f"{OAUTH_STATE_PREFIX}:{state}")
        if not raw_state:
            raise APIError(status_code=410, code="ORG_OAUTH_410", error="oauth_state_expired")
        try:
            payload = json.loads(raw_state)
            user_uui_id = uuid.UUID(str(payload["user_uui_id"]))
            organisation_id = uuid.UUID(str(payload["organisation_id"]))
            redirect_uri = str(payload["redirect_uri"])
            code_verifier = str(payload["code_verifier"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise APIError(status_code=422, code="ORG_OAUTH_422", error="invalid_oauth_state") from exc

        organisation = await self.session.get(OrganisationDirectory, organisation_id)
        if organisation is None or not organisation.oauth_enabled:
            raise APIError(status_code=404, code="ORG_404", error="organisation_not_found")
        if not all(
            (
                organisation.oauth_client_id,
                organisation.oauth_client_secret_ciphertext,
                organisation.oauth_token_url,
                organisation.oauth_userinfo_url,
            )
        ):
            raise APIError(status_code=409, code="ORG_OAUTH_409", error="organisation_oauth_not_available")

        secret = OrganisationCredentialCipher.decrypt(organisation.oauth_client_secret_ciphertext)
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                token_response = await client.post(
                    organisation.oauth_token_url,
                    data={
                        "client_id": organisation.oauth_client_id,
                        "client_secret": secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                access_token = str(token_payload.get("access_token") or "")
                if not access_token:
                    raise ValueError("missing access token")
                userinfo_response = await client.get(
                    organisation.oauth_userinfo_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                userinfo_response.raise_for_status()
                userinfo = userinfo_response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise APIError(status_code=502, code="ORG_OAUTH_502", error="oauth_provider_failed") from exc

        external_account_id = next(
            (
                str(userinfo[key])
                for key in ("sub", "id", "user_id", "email")
                if userinfo.get(key) is not None
            ),
            "",
        )
        if not external_account_id:
            raise APIError(status_code=422, code="ORG_OAUTH_422", error="oauth_identity_missing")

        proxy_hash = ProxyUserService.hash_external_user_id(
            str(organisation.tenant_id),
            external_account_id,
        )
        proxy_user = (
            await self.session.execute(
                select(ProxyUser).where(
                    ProxyUser.tenant_id == organisation.tenant_id,
                    ProxyUser.external_user_id_hash == proxy_hash,
                )
            )
        ).scalar_one_or_none()
        if proxy_user is not None:
            link = (
                await self.session.execute(
                    select(UUIProxyLink).where(UUIProxyLink.proxy_user_id == proxy_user.id)
                )
            ).scalar_one_or_none()
            if link is not None and link.user_uui_id != user_uui_id:
                raise APIError(status_code=409, code="ORG_CONN_409", error="organisation_account_already_connected")
            if link is None:
                self.session.add(
                    UUIProxyLink(
                        tenant_id=organisation.tenant_id,
                        proxy_user_id=proxy_user.id,
                        user_uui_id=user_uui_id,
                    )
                )

        await self.upsert_connection(
            universal_user_id=user_uui_id,
            organisation=organisation,
            method="oauth",
            external_account_id=external_account_id,
            proxy_user_id=proxy_user.id if proxy_user else None,
        )
        await self.session.commit()
        return organisation

    async def connection_memory_count(self, connection_id: uuid.UUID) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(UniversalMemory)
                    .where(
                        UniversalMemory.source_org_connection_id == connection_id,
                        UniversalMemory.is_archived.is_(False),
                    )
                )
            ).scalar_one()
            or 0
        )
