from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LifecycleReportResponse(BaseModel):
    tenant_id: str
    decayed_count: int = 0
    archived_count: int = 0
    promoted_to_hot: int = 0
    rescored_count: int = 0
    skipped: bool = False
    reason: str | None = None
    ran_at: datetime | None = None
    duration_seconds: float = 0.0


class LifecycleReportsResponse(BaseModel):
    data: list[LifecycleReportResponse]
    request_id: str
    timestamp: datetime
