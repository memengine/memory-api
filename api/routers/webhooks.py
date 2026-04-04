from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request

from api.dependencies import get_webhook_service
from api.schemas.responses import WebhookData
from api.schemas.responses import WebhookResponse
from api.services.webhook_service import WebhookService
from api.routers.common import get_request_id
from api.routers.common import utc_now


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/clerk", response_model=WebhookResponse)
async def receive_clerk_webhook(
    request: Request,
    webhook_service: Annotated[WebhookService, Depends(get_webhook_service)],
) -> WebhookResponse:
    """Receive Clerk lifecycle webhooks.

    Parameters: raw request body and SVIX signature headers.
    Responses: receipt acknowledgement after signature verification and event processing.
    """
    payload = await request.body()
    received = await webhook_service.verify_and_process(
        payload=payload,
        headers={
            "svix-id": request.headers.get("svix-id", ""),
            "svix-timestamp": request.headers.get("svix-timestamp", ""),
            "svix-signature": request.headers.get("svix-signature", ""),
        },
    )
    return WebhookResponse(
        data=WebhookData(received=received),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
