from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import User
from api.errors import APIError


class WebhookService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        webhook_secret: str | None = None,
    ) -> None:
        self.session = session
        self.webhook_secret = webhook_secret or os.getenv("CLERK_WEBHOOK_SECRET", "")

    async def verify_and_process(self, *, payload: bytes, headers: dict[str, str]) -> bool:
        self._verify_svix_signature(payload=payload, headers=headers)
        event = json.loads(payload.decode("utf-8"))
        event_type = str(event.get("type", ""))
        data = event.get("data", {}) or {}

        if event_type == "user.deleted":
            external_id = data.get("id")
            if external_id:
                await self.session.execute(delete(User).where(User.external_id == str(external_id)))
                await self.session.commit()
        return True

    def _verify_svix_signature(self, *, payload: bytes, headers: dict[str, str]) -> None:
        if not self.webhook_secret:
            raise APIError(
                status_code=500,
                code="WH_500",
                error="webhook_secret_missing",
            )

        svix_id = headers.get("svix-id", "")
        svix_timestamp = headers.get("svix-timestamp", "")
        svix_signature = headers.get("svix-signature", "")
        if not svix_id or not svix_timestamp or not svix_signature:
            raise APIError(
                status_code=401,
                code="WH_401",
                error="invalid_webhook_signature",
            )

        try:
            timestamp = int(svix_timestamp)
        except ValueError as exc:
            raise APIError(
                status_code=401,
                code="WH_401",
                error="invalid_webhook_signature",
            ) from exc

        if abs(int(time.time()) - timestamp) > 300:
            raise APIError(
                status_code=401,
                code="WH_401",
                error="webhook_timestamp_expired",
            )

        secret = self.webhook_secret
        if secret.startswith("whsec_"):
            secret = secret.split("_", 1)[1]
        try:
            decoded_secret = base64.b64decode(secret)
        except Exception:
            decoded_secret = secret.encode("utf-8")

        signed_content = f"{svix_id}.{svix_timestamp}.{payload.decode('utf-8')}".encode("utf-8")
        expected_signature = base64.b64encode(
            hmac.new(decoded_secret, signed_content, hashlib.sha256).digest()
        ).decode("utf-8")

        signatures = []
        for part in svix_signature.split(" "):
            if "," in part:
                version, value = part.split(",", 1)
                if version.strip() == "v1":
                    signatures.append(value.strip())

        if not any(hmac.compare_digest(expected_signature, signature) for signature in signatures):
            raise APIError(
                status_code=401,
                code="WH_401",
                error="invalid_webhook_signature",
            )
