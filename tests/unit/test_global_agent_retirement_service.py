from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.services.global_agent_retirement_service import GlobalAgentRetirementService


class Result:
    def __init__(self, *, one=None, rows=None): self.one=one; self.rows=rows or []
    def scalar_one_or_none(self): return self.one
    def scalars(self): return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_retirement_deactivates_agent_keys_and_grants_without_deleting_source() -> None:
    agent=SimpleNamespace(id=uuid.uuid4(),owner_tenant_id=uuid.uuid4(),is_active=True)
    key=SimpleNamespace(is_active=True);grant=SimpleNamespace(is_active=True,revoked_at=None)
    class Session:
        def __init__(self):self.calls=0;self.commits=0
        async def execute(self,_query):
            self.calls+=1
            return [Result(one=agent),Result(rows=[key]),Result(rows=[grant])][self.calls-1]
        async def commit(self):self.commits+=1
        async def refresh(self,_row):return None
    session=Session()
    result=await GlobalAgentRetirementService(session=session).retire(tenant_id=str(agent.owner_tenant_id),agent_id=str(agent.id))
    assert result is not None
    assert agent.is_active is False
    assert key.is_active is False
    assert grant.is_active is False and grant.revoked_at is not None
    assert result.revoked_api_keys==1 and result.revoked_grants==1
    assert session.commits==1
