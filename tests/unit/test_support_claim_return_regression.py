from __future__ import annotations

import uuid

from api.db.models import SupportMemory
from api.services.support import support_extractor as module
from api.services.support.support_extractor import SupportExtractor


def test_record_field_claims_returns_claim_ledger_result(monkeypatch) -> None:
    expected = [object()]

    class FakeClaimLedgerService:
        def __init__(self, session) -> None:
            self.session = session

        def record_domain_fields(self, **kwargs):
            return expected

    monkeypatch.setattr(module, "ClaimLedgerService", FakeClaimLedgerService)
    extractor = SupportExtractor.__new__(SupportExtractor)
    extractor.session = object()
    memory = SupportMemory(
        id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        support_type="saas",
    )
    claims = extractor._record_field_claims(
        memory, fields_updated={"support_type"}, job_id=str(uuid.uuid4())
    )
    assert claims is expected
