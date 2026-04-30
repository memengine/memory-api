from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config.plan_limits import apply_plan_limits
from api.dependencies import get_webhook_service
from api.db.database import get_db_session
from api.errors import APIError
from api.schemas.responses import WebhookData
from api.schemas.responses import WebhookResponse
from api.services.webhook_service import WebhookService
from api.routers.common import get_request_id
from api.routers.common import utc_now


# Required Clerk webhook events:
# user.created, user.updated (existing)
# organization.created, organization.updated,
# organization.deleted (new - must be enabled in
# Clerk dashboard -> Webhooks -> your endpoint -> Events)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


async def _get_tenant_id_by_org(session: AsyncSession, org_id: str) -> str | None:
    result = await session.execute(
        text("SELECT id FROM tenants WHERE clerk_org_id = :org_id"),
        {"org_id": org_id},
    )
    tenant_id = result.scalar_one_or_none()
    return str(tenant_id) if tenant_id is not None else None


async def _create_or_find_tenant_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    org_name: str,
) -> str | None:
    result = await session.execute(
        text(
            """
            INSERT INTO tenants (company_name, clerk_org_id, is_active, plan_tier)
            VALUES (:company_name, :clerk_org_id, TRUE, 'free')
            ON CONFLICT (clerk_org_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "company_name": org_name,
            "clerk_org_id": org_id,
        },
    )
    tenant_id = result.scalar_one_or_none()
    if tenant_id is not None:
        return str(tenant_id)
    return await _get_tenant_id_by_org(session, org_id)


async def _ensure_budget_row(session: AsyncSession, *, tenant_id: str) -> None:
    await session.execute(
        text(
            """
            INSERT INTO tenant_budgets (tenant_id, plan_tier)
            VALUES (:tenant_id, 'free')
            ON CONFLICT (tenant_id) DO NOTHING
            """
        ),
        {"tenant_id": tenant_id},
    )


async def _handle_organization_created(session: AsyncSession, data: dict[str, object]) -> None:
    org_id = str(data.get("id") or "").strip()
    if not org_id:
        return None

    org_name = str(data.get("name") or data.get("slug") or org_id).strip()
    tenant_id = await _get_tenant_id_by_org(session, org_id)
    if tenant_id is None:
        tenant_id = await _create_or_find_tenant_for_org(
            session,
            org_id=org_id,
            org_name=org_name,
        )
    if tenant_id is None:
        return None

    await _ensure_budget_row(session, tenant_id=tenant_id)
    await session.commit()

    try:
        await session.run_sync(lambda sync_session: apply_plan_limits(tenant_id, "free", sync_session))
    except Exception:
        logger.exception("Failed to apply free plan limits for Clerk org %s", org_id)


async def _handle_organization_deleted(session: AsyncSession, data: dict[str, object]) -> None:
    org_id = str(data.get("id") or "").strip()
    if not org_id:
        return None
    await session.execute(
        text(
            """
            UPDATE tenants
            SET is_active = FALSE
            WHERE clerk_org_id = :org_id
            """
        ),
        {"org_id": org_id},
    )
    await session.commit()


async def _handle_organization_updated(session: AsyncSession, data: dict[str, object]) -> None:
    org_id = str(data.get("id") or "").strip()
    if not org_id:
        return None
    new_name = str(data.get("name") or data.get("slug") or org_id).strip()
    await session.execute(
        text(
            """
            UPDATE tenants
            SET company_name = :company_name
            WHERE clerk_org_id = :org_id
            """
        ),
        {
            "company_name": new_name,
            "org_id": org_id,
        },
    )
    await session.commit()


async def _handle_organization_event(
    session: AsyncSession,
    *,
    event_type: str,
    data: dict[str, object],
) -> None:
    if event_type == "organization.created":
        await _handle_organization_created(session, data)
    elif event_type == "organization.deleted":
        await _handle_organization_deleted(session, data)
    elif event_type == "organization.updated":
        await _handle_organization_updated(session, data)


@router.post("/clerk", response_model=WebhookResponse)
async def receive_clerk_webhook(
    request: Request,
    webhook_service: Annotated[WebhookService, Depends(get_webhook_service)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WebhookResponse:
    """Receive Clerk lifecycle webhooks.

    Parameters: raw request body and SVIX signature headers.
    Responses: receipt acknowledgement after signature verification and event processing.
    """
    payload = await request.body()
    headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    try:
        webhook_service._verify_svix_signature(payload=payload, headers=headers)
    except APIError as exc:
        raise APIError(status_code=400, code=exc.code, error=exc.error) from exc

    event = json.loads(payload.decode("utf-8"))
    event_type = str(event.get("type", ""))
    data = event.get("data", {}) or {}
    received = True

    if event_type.startswith("organization."):
        try:
            await _handle_organization_event(
                session,
                event_type=event_type,
                data=dict(data),
            )
        except Exception:
            logger.exception("Failed to process Clerk organization webhook: %s", event_type)
            received = True
    else:
        received = await webhook_service.verify_and_process(payload=payload, headers=headers)

    return WebhookResponse(
        data=WebhookData(received=received),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
