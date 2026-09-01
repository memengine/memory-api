from __future__ import annotations

import uuid

from api.db.models import EdTechMemory
from api.services.edtech import edtech_extractor as module
from api.services.edtech.edtech_extractor import EdTechExtractor


def test_record_field_claims_returns_claim_ledger_result(monkeypatch) -> None:
    expected = [object()]

    class FakeClaimLedgerService:
        def __init__(self, session) -> None:
            self.session = session

        def record_domain_fields(self, **kwargs):
            return expected

    monkeypatch.setattr(module, "ClaimLedgerService", FakeClaimLedgerService)
    extractor = EdTechExtractor.__new__(EdTechExtractor)
    extractor.session = object()
    memory = EdTechMemory(
        id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        grade_level="Class 12",
    )
    claims = extractor._record_field_claims(
        memory, fields_updated={"grade_level"}, job_id=str(uuid.uuid4())
    )
    assert claims is expected
