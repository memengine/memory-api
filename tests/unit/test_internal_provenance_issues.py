from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from fastapi import HTTPException

from api.routers.internal import _list_provenance_issues


class _MappingsResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.statement = ""
        self.params: dict = {}

    async def execute(self, statement, params: dict) -> _MappingsResult:
        self.statement = str(statement)
        self.params = params
        return _MappingsResult(self.rows)


@pytest.mark.asyncio
async def test_provenance_issue_list_is_paginated_and_excludes_sensitive_fields() -> None:
    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    session = _Session([
        {
            "issue_key": f"service_writer:{tenant_id}:billing-service:key",
            "issue_type": "service_writer",
            "tenant_id": tenant_id,
            "tenant_name": "Acme",
            "source_label": "billing-service",
            "api_key_name": "Billing production",
            "api_key_prefix": "mem_ab12",
            "sample_reference": "billing-event-123",
            "occurrences": 7,
            "first_seen": now,
            "last_seen": now,
            "total_count": 2,
        }
    ])

    result = await _list_provenance_issues(
        session,
        issue_type="service_writer",
        tenant_id=str(tenant_id),
        search="billing",
        cursor=None,
        limit=1,
    )

    assert result.total_count == 2
    assert result.next_cursor == "1"
    assert result.data[0].occurrences == 7
    assert result.data[0].api_key_prefix == "mem_ab12"
    assert session.params["search_pattern"] == "%billing%"
    assert "payload_hash" not in session.statement
    assert "evidence_refs" not in session.statement
    assert "external_user_id" not in session.statement
    assert "asserted_value" not in session.statement
    assert not ({"memory_content", "user_id", "payload_hash", "raw_api_key"} & set(result.data[0].model_dump()))


@pytest.mark.asyncio
async def test_provenance_issue_list_rejects_invalid_filters() -> None:
    session = _Session([])
    with pytest.raises(HTTPException) as issue_error:
        await _list_provenance_issues(
            session, issue_type="secret_dump", tenant_id=None,
            search="", cursor=None, limit=50,
        )
    assert issue_error.value.status_code == 422

    with pytest.raises(HTTPException) as tenant_error:
        await _list_provenance_issues(
            session, issue_type="all", tenant_id="not-a-uuid",
            search="", cursor=None, limit=50,
        )
    assert tenant_error.value.status_code == 422