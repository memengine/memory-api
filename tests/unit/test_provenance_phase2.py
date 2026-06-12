from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from api.services.conflict_resolver import ConflictResolver
from api.services.provenance_service import ProvenanceService
from api.services.provenance_service import payload_sha256
from api.schemas.provenance_schemas import AuthorityRules
from api.tasks.provenance_tasks import redact_job_payload


def test_payload_hash_is_stable_for_equivalent_messages() -> None:
    messages = [{"role": "user", "content": "My plan is Growth."}]
    assert payload_sha256(messages) == payload_sha256(list(messages))
    assert len(payload_sha256(messages)) == 64


def test_legacy_source_is_derived_from_authenticated_api_key() -> None:
    source = ProvenanceService.normalize_source(
        source=None,
        writer=None,
        api_key_id="12345678-1234-1234-1234-123456789abc",
        job_id="job-1",
    )
    assert source["service"] == "api-key-12345678-123"
    assert source["event_id"] == "job-1"


def test_registered_writer_becomes_default_source() -> None:
    writer = SimpleNamespace(service_key="billing-service")
    source = ProvenanceService.normalize_source(
        source={"event_id": "invoice-42", "observed_at": "2026-06-11T10:00:00Z"},
        writer=writer,
        api_key_id="key-id",
        job_id="job-1",
    )
    assert source["service"] == "billing-service"
    assert source["observed_at"] == datetime(2026, 6, 11, 10, 0, tzinfo=UTC)


def test_retention_redacts_messages_but_keeps_job_context() -> None:
    payload = {
        "job_id": "job-1",
        "source_event_id": "event-1",
        "messages": [{"role": "user", "content": "secret conversation"}],
    }
    assert redact_job_payload(payload) == {
        "job_id": "job-1",
        "source_event_id": "event-1",
        "messages_redacted": True,
    }


def test_authority_rules_reject_out_of_range_priority() -> None:
    try:
        AuthorityRules(categories={"fact": 101})
    except ValueError:
        pass
    else:
        raise AssertionError("AuthorityRules accepted an invalid priority.")


def test_higher_authority_writer_wins_conflict() -> None:
    resolver = object.__new__(ConflictResolver)
    resolver.provenance_snapshot = {
        "authority_rules": {"categories": {"fact": 90}},
        "observed_at": "2026-06-11T10:00:00+00:00",
    }
    existing = SimpleNamespace(
        category=SimpleNamespace(value="fact"),
        metadata_json={
            "provenance": {
                "authority_rules": {"categories": {"fact": 40}},
                "observed_at": "2026-06-11T09:00:00+00:00",
            }
        },
    )
    incoming = SimpleNamespace(category="fact")
    decision = resolver._authority_conflict_decision(incoming, existing)
    assert decision is not None
    assert decision.action == "UPDATE"


def test_older_equal_authority_event_is_rejected() -> None:
    resolver = object.__new__(ConflictResolver)
    resolver.provenance_snapshot = {
        "authority_rules": {"default_priority": 50},
        "observed_at": "2026-06-10T10:00:00+00:00",
    }
    existing = SimpleNamespace(
        category=SimpleNamespace(value="fact"),
        metadata_json={
            "provenance": {
                "authority_rules": {"default_priority": 50},
                "observed_at": "2026-06-11T10:00:00+00:00",
            }
        },
    )
    incoming = SimpleNamespace(category="fact")
    decision = resolver._authority_conflict_decision(incoming, existing)
    assert decision is not None
    assert decision.action == "REJECT"
