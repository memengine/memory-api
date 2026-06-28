from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from api.services.conflict_resolver import ConflictResolver
from api.services.provenance_service import ProvenanceService
from api.services.provenance_service import payload_sha256
from api.services.provenance_service import source_event_sha256
from api.services.provenance_service import SOURCE_EVENT_HASH_VERSION
from api.services.provenance_service import source_event_payload_matches
from api.schemas.provenance_schemas import AuthorityRules
from api.tasks.provenance_tasks import redact_job_payload


def test_payload_hash_is_stable_for_equivalent_messages() -> None:
    messages = [{"role": "user", "content": "My plan is Growth."}]
    assert payload_sha256(messages) == payload_sha256(list(messages))
    assert len(payload_sha256(messages)) == 64


def test_source_event_hash_is_stable_for_equivalent_envelopes() -> None:
    messages = [{"role": "assistant", "content": "The current plan is Growth."}]
    first = source_event_sha256(
        messages=messages,
        source={
            "service": "billing-service",
            "event_id": "invoice-42",
            "observed_at": "2026-06-14T10:00:00Z",
            "scope": {"workspace": "ws-1", "region": "IN1"},
            "evidence": [
                {"source_type": "invoice", "reference": "INV-42"},
                {"source_type": "subscription", "reference": "SUB-7"},
            ],
        },
    )
    second = source_event_sha256(
        messages=list(messages),
        source={
            "event_id": "invoice-42",
            "service": "billing-service",
            "observed_at": "2026-06-14T10:00:00+00:00",
            "scope": {"region": "IN1", "workspace": "ws-1"},
            "evidence": [
                {"source_type": "subscription", "reference": "SUB-7"},
                {"reference": "INV-42", "source_type": "invoice"},
            ],
        },
    )

    assert first == second
    assert len(first) == 64


def test_source_event_hash_changes_when_envelope_changes() -> None:
    messages = [{"role": "assistant", "content": "The current plan is Growth."}]
    base_source = {
        "service": "billing-service",
        "event_id": "invoice-42",
        "observed_at": "2026-06-14T10:00:00Z",
        "scope": {"workspace": "ws-1"},
        "evidence": [{"source_type": "invoice", "reference": "INV-42"}],
    }
    original = source_event_sha256(messages=messages, source=base_source)

    variants = [
        {**base_source, "observed_at": "2026-06-14T10:01:00Z"},
        {**base_source, "scope": {"workspace": "ws-2"}},
        {
            **base_source,
            "evidence": [{"source_type": "invoice", "reference": "INV-43"}],
        },
        {**base_source, "service": "support-service"},
        {**base_source, "event_id": "invoice-43"},
    ]

    assert all(
        source_event_sha256(messages=messages, source=variant) != original
        for variant in variants
    )


def test_source_event_payload_match_supports_legacy_message_hashes() -> None:
    messages = [{"role": "user", "content": "My plan is Growth."}]
    legacy_event = SimpleNamespace(
        payload_hash=payload_sha256(messages),
        processing_metadata={},
    )

    assert source_event_payload_matches(
        existing_event=legacy_event,
        messages=messages,
        incoming_hash="new-envelope-hash",
    )


def test_source_event_payload_match_uses_strict_v2_hash() -> None:
    event = SimpleNamespace(
        payload_hash="strict-envelope-hash",
        processing_metadata={"payload_hash_version": SOURCE_EVENT_HASH_VERSION},
    )

    assert source_event_payload_matches(
        existing_event=event,
        messages=[{"role": "user", "content": "Same messages are insufficient."}],
        incoming_hash="strict-envelope-hash",
    )
    assert not source_event_payload_matches(
        existing_event=event,
        messages=[{"role": "user", "content": "Same messages are insufficient."}],
        incoming_hash="changed-envelope-hash",
    )


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


def test_equal_authority_different_writers_require_clarification() -> None:
    resolver = object.__new__(ConflictResolver)
    resolver.provenance_snapshot = {
        "writer_id": "11111111-1111-1111-1111-111111111111",
        "authority_rules": {"categories": {"fact": 50}},
    }
    existing = SimpleNamespace(
        content="Customer's current subscription plan is Starter.",
        category=SimpleNamespace(value="fact"),
        metadata_json={
            "provenance": {
                "writer_id": "22222222-2222-2222-2222-222222222222",
                "authority_rules": {"categories": {"fact": 50}},
            }
        },
    )
    incoming = SimpleNamespace(
        content="Customer's current subscription plan is Growth.",
        category="fact",
    )

    assert resolver._is_equal_authority_cross_writer_conflict(incoming, existing)


def test_same_writer_does_not_trigger_equal_authority_clarification() -> None:
    writer_id = "11111111-1111-1111-1111-111111111111"
    resolver = object.__new__(ConflictResolver)
    resolver.provenance_snapshot = {
        "writer_id": writer_id,
        "authority_rules": {"default_priority": 50},
    }
    existing = SimpleNamespace(
        content="Customer's current subscription plan is Starter.",
        category=SimpleNamespace(value="fact"),
        metadata_json={
            "provenance": {
                "writer_id": writer_id,
                "authority_rules": {"default_priority": 50},
            }
        },
    )
    incoming = SimpleNamespace(
        content="Customer's current subscription plan is Growth.",
        category="fact",
    )

    assert not resolver._is_equal_authority_cross_writer_conflict(incoming, existing)
