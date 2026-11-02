"""Extraction-to-resolver route coverage; providers and persistence isolated."""
import json
import uuid
from unittest.mock import MagicMock

import pytest

from api.db.models import ProxyUser
from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext
from api.services.extraction_service import ExtractionService
from tests.unit.test_extraction_service import FakeLLMService, _spec
from tests.unit.test_conflict_resolver import FakeSession, source_response


@pytest.mark.asyncio
@pytest.mark.parametrize("service_envelope", [False, True])
async def test_request_contract_selects_expected_resolver_path(tmp_path, service_envelope):
    text = "This synthetic account uses billing currency INR."
    extraction_llm = FakeLLMService(json.dumps({"memories": [{
        "content": text, "category": "fact", "importance_score": 7.0,
        "confidence": 0.99, "reasoning": "Directly reported fact.",
        "evidence_turns": [0], "evidence_relation": "direct_user_statement",
        "evidence_spans": [{"turn_index": 0, "quote": text}],
    }], "nothing_to_extract": False}))
    service = ExtractionService(llm_service=extraction_llm, spec_path=_spec(tmp_path))
    tenant, owner, user = [uuid.uuid4() for _ in range(3)]
    messages = [{"role": "user", "content": text, "turn_id": "synthetic-turn"}]
    result = await service.extract(messages=messages, proxy_user_id=str(owner),
        tenant_id=str(tenant), job_id=str(uuid.uuid4()),
        source_context={"service": "billing", "event_id": "synthetic-event"} if service_envelope else None)
    (candidate,) = result.memories_to_store
    assert (candidate.validated_evidence.get("grounding_mode") == "verified_source_spans") == (not service_envelope)
    model = MagicMock()
    from types import SimpleNamespace
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(source_response()), total_tokens=20)
    vectors = MagicMock()
    vectors.search_memories.return_value = []
    resolver = ConflictResolver(session=FakeSession(proxy_user=ProxyUser(id=owner, tenant_id=tenant)),
        qdrant_service=vectors, embedder=lambda _: [0.1]*3, llm_service=model, source_messages=messages)
    resolver._record_claim_for_memory = MagicMock()
    stored = resolver.check_and_store([candidate], user_id=str(user), tenant_id=str(tenant),
        proxy_user_id=str(owner), source_context=SourceMemoryContext((), complete=True))
    assert [row.resolution for row in stored] == ["NEW"]
    assert resolver.last_source_decision_calls == (0 if service_envelope else 1)
    assert model.complete_sync.call_count == (0 if service_envelope else 1)
