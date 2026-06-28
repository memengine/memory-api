from __future__ import annotations

import pytest

from api.db.models import MemoryClaimRevision
from api.db.models import UniversalMemoryClaimRevision
from api.routers.internal import provenance_versions
from api.services.claim_versions import CLAIM_PROCESSOR_VERSION
from api.services.claim_versions import CLAIM_SCHEMA_VERSION
from api.services.claim_versions import PASSPORT_BACKFILL_PROCESSOR_VERSION
from api.services.claim_versions import TENANT_BACKFILL_PROCESSOR_VERSION
from api.services.claim_versions import processor_version_for_resolution
from api.services.claim_versions import supports_claim_schema


class _MappingsResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statement = ""

    async def execute(self, statement):
        self.statement = str(statement)
        return _MappingsResult(self.rows)


def test_current_claim_version_contract_is_explicit() -> None:
    assert CLAIM_SCHEMA_VERSION == 1
    assert CLAIM_PROCESSOR_VERSION == "claim-ledger-v1"
    assert supports_claim_schema(1)
    assert not supports_claim_schema(0)
    assert not supports_claim_schema(2)


def test_processor_versions_distinguish_live_and_backfill_writes() -> None:
    assert processor_version_for_resolution("memory_extraction") == CLAIM_PROCESSOR_VERSION
    assert (
        processor_version_for_resolution("legacy_source_event_recovered")
        == TENANT_BACKFILL_PROCESSOR_VERSION
    )
    assert (
        processor_version_for_resolution(
            "legacy passport provenance backfill",
            passport=True,
        )
        == PASSPORT_BACKFILL_PROCESSOR_VERSION
    )


@pytest.mark.parametrize(
    "model",
    [MemoryClaimRevision, UniversalMemoryClaimRevision],
)
def test_revision_models_have_backward_compatible_version_defaults(model) -> None:
    columns = model.__table__.c

    assert columns.schema_version.nullable is False
    assert columns.processor_version.nullable is False
    assert str(columns.schema_version.server_default.arg) == "1"
    assert "legacy" in str(columns.processor_version.server_default.arg)


@pytest.mark.asyncio
async def test_operator_version_distribution_reports_both_scopes() -> None:
    session = _Session(
        [
            {
                "scope": "tenant",
                "schema_version": 1,
                "processor_version": "legacy",
                "revision_count": 12,
            },
            {
                "scope": "passport",
                "schema_version": 1,
                "processor_version": CLAIM_PROCESSOR_VERSION,
                "revision_count": 4,
            },
        ]
    )

    result = await provenance_versions(session=session)

    assert result.current_schema_version == CLAIM_SCHEMA_VERSION
    assert result.current_processor_version == CLAIM_PROCESSOR_VERSION
    assert [bucket.scope for bucket in result.data] == ["tenant", "passport"]
    assert result.data[0].revision_count == 12
    assert "memory_claim_revisions" in session.statement
    assert "universal_memory_claim_revisions" in session.statement