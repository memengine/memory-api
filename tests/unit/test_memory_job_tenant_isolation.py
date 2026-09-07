from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.errors import APIError
from api.routers.memories import get_memory_job_status


class _JobStatusService:
    def __init__(self, tenant_id: str | None) -> None:
        self.tenant_id = tenant_id

    async def get_job_status(self, *, job_id: str) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "job_id": job_id,
            "status": "queued",
            "memories_created": 0,
        }


@pytest.mark.parametrize("job_tenant_id", [None, str(uuid.uuid4())])
@pytest.mark.asyncio
async def test_job_status_hides_missing_or_foreign_tenant_ownership(
    job_tenant_id: str | None,
) -> None:
    caller_tenant_id = str(uuid.uuid4())
    request = SimpleNamespace(
        state=SimpleNamespace(tenant_id=caller_tenant_id, user_id=None),
        headers={},
    )

    with pytest.raises(APIError) as exc_info:
        await get_memory_job_status(
            request=request,
            job_id=uuid.uuid4(),
            memory_service=_JobStatusService(job_tenant_id),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "JOB_404"
