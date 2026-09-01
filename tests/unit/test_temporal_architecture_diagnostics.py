from api.db.models import Memory, MemoryClaimRevision
from api.schemas.requests import MemoryRetrieveRequest
from api.services.vector_outbox import build_vector_payload


def test_memory_schema_supports_explicit_validity_interval() -> None:
    assert hasattr(Memory, "effective_from")
    assert hasattr(Memory, "effective_until")


def test_claim_schema_supports_closed_validity_interval() -> None:
    assert hasattr(MemoryClaimRevision, "effective_from")
    assert hasattr(MemoryClaimRevision, "effective_until")


def test_retrieval_contract_supports_historical_as_of_queries() -> None:
    assert "as_of" in MemoryRetrieveRequest.model_fields


def test_vector_payload_contract_carries_temporal_validity() -> None:
    class MemoryLike:
        id = "memory-1"
        content = "User lives in Jaipur."
        category = "fact"
        importance_score = 7.0
        confidence_score = 0.9
        is_archived = False
        tenant_id = "tenant-1"
        proxy_user_id = "proxy-1"
        user_id = None
        agent_id = None
        previous_version_id = None
        source_event_id = None
        metadata_json = {}
        created_at = None
        last_accessed_at = None
        expires_at = None
        effective_from = "2026-08-01T00:00:00+00:00"
        effective_until = "2026-09-01T00:00:00+00:00"

    payload = build_vector_payload(MemoryLike(), tenant_id=None, proxy_user_id=None)
    assert payload["effective_from"] == MemoryLike.effective_from
    assert payload["effective_until"] == MemoryLike.effective_until


def test_retrieval_contract_distinguishes_event_time_from_ingestion_age() -> None:
    fields = MemoryRetrieveRequest.model_fields
    assert "effective_at" in fields or "as_of" in fields
