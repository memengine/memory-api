"""Registered-source ledger gate; synthetic data, no providers or database."""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api.db.models import Memory, MemoryCategory
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.conflict_resolver import ConflictResolver


def memory(*, registered=True, source_event=True, explicit=False):
    provenance = {"writer_id": str(uuid.uuid4())} if registered else {}
    if explicit:
        provenance["extraction_evidence"] = {"explicit_preference_v1": {
            "property_key": "examples.language", "value": "Python",
            "applicability": {"kind": "global"}, "turn_index": 0, "quote": "Python",
        }}
    return Memory(id=uuid.uuid4(), source_event_id=uuid.uuid4() if source_event else None,
                  category=MemoryCategory.preference if explicit else MemoryCategory.fact,
                  metadata_json={"provenance": provenance})


def record(row, *, tenant=True, owner=True):
    # This helper has no constructor dependencies; the real SQL store/rollback
    # path is covered separately by the isolated PostgreSQL regressions.
    resolver = object.__new__(ConflictResolver)
    resolver.session = SimpleNamespace()
    resolver._record_claim_for_memory(memory=row, tenant_id=str(uuid.uuid4()) if tenant else None,
        proxy_user_id=str(uuid.uuid4()) if owner else None, resolution="NEW",
        decision_evidence=None, predecessor_memory_id=None)


@pytest.mark.parametrize("error", [RuntimeError("ledger failed"), ValueError("invalid ledger input")])
def test_registered_source_propagates_ledger_errors(monkeypatch, error):
    callback = Mock(side_effect=error)
    monkeypatch.setattr(ClaimLedgerService, "record_memory", callback)
    with pytest.raises(type(error), match=str(error)):
        record(memory())
    callback.assert_called_once()


def test_registered_source_rejects_missing_recording_result(monkeypatch):
    monkeypatch.setattr(ClaimLedgerService, "record_memory", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="did not record"):
        record(memory())


@pytest.mark.parametrize("missing", ["tenant", "owner", "source_event"])
def test_registered_source_requires_complete_recording_context(monkeypatch, missing):
    callback = Mock()
    monkeypatch.setattr(ClaimLedgerService, "record_memory", callback)
    with pytest.raises(ValueError, match="requires owner and source event"):
        record(memory(source_event=missing != "source_event"), tenant=missing != "tenant", owner=missing != "owner")
    callback.assert_not_called()


def test_registered_source_does_not_force_disputed_revision_to_win(monkeypatch):
    row = memory()
    old_winner = uuid.uuid4()
    callback = Mock(return_value=SimpleNamespace(status="disputed", active_memory_id=old_winner))
    monkeypatch.setattr(ClaimLedgerService, "record_memory", callback)
    record(row)
    assert callback.return_value.active_memory_id == old_winner != row.id
    assert callback.return_value.status == "disputed"


def test_legacy_unregistered_recording_error_remains_best_effort(monkeypatch):
    callback = Mock(side_effect=RuntimeError("legacy ledger failure"))
    monkeypatch.setattr(ClaimLedgerService, "record_memory", callback)
    record(memory(registered=False))
    callback.assert_called_once()


def test_explicit_preference_recording_error_still_aborts(monkeypatch):
    monkeypatch.setattr(ClaimLedgerService, "record_memory", Mock(side_effect=RuntimeError("preference ledger failure")))
    with pytest.raises(RuntimeError, match="preference ledger failure"):
        record(memory(registered=False, explicit=True))


def test_explicit_preference_still_requires_its_active_winner(monkeypatch):
    monkeypatch.setattr(ClaimLedgerService, "record_memory", Mock(return_value=SimpleNamespace(
        status="disputed", active_memory_id=uuid.uuid4())))
    with pytest.raises(ValueError, match="did not activate"):
        record(memory(registered=False, explicit=True))
