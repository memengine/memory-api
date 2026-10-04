"""Snapshot boundaries, not language comprehension or writer permission."""
import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from api.services.preference_snapshot_service import (
    PreferenceSnapshotCatalog,
    PreferenceSnapshotUnavailable,
    load_preference_snapshot,
)


def catalog(tenant_id=None, **changes):
    return PreferenceSnapshotCatalog.model_validate({
        "tenant_id": tenant_id or uuid.uuid4(),
        "properties": [{"key": "examples.language", "description": "Usual example language"}],
        "scopes": [{"ref": "global", "description": "General default", "applicability": {"kind": "global"}}],
        **changes,
    })


@pytest.mark.parametrize("change", [
    {"properties": []}, {"scopes": []}, {"authority": 100},
    {"properties": [{"key": "Bad key", "description": "Invalid"}]},
    {"properties": [{"key": "p", "description": "x"}] * 2},
    {"scopes": [{"ref": "a", "description": "x", "applicability": {"kind": "unresolved"}}]},
    {"scopes": [{"ref": "a", "description": "x", "applicability": {"kind": "global"}}] * 2},
    {"scopes": [{"ref": ref, "description": "x", "applicability": {"kind": "global"}} for ref in ("a", "b")]},
    {"properties": [{"key": f"p{i}", "description": "x"} for i in range(16)],
     "scopes": [{"ref": f"s{i}", "description": "x", "applicability":
                 {"kind": "context", "namespace": "project", "key": str(i)}} for i in range(3)]},
    {"properties": [{"key": "p", "description": "x" * 401}]},
])
def test_catalog_rejects_unknown_duplicate_or_oversized_identities(change):
    with pytest.raises(ValidationError):
        catalog(**change)


def test_catalog_accepts_exact_limit_and_is_immutable():
    offered = catalog(properties=[{"key": f"p{i}", "description": "x"} for i in range(16)], scopes=[
        {"ref": "global", "description": "x", "applicability": {"kind": "global"}},
        {"ref": "project", "description": "x", "applicability": {"kind": "context", "namespace": "project", "key": "Cedar"}},
    ])
    assert len(offered.properties) * len(offered.scopes) == 32
    with pytest.raises(ValidationError):
        offered.scopes[1].applicability.key = "Other"


def empty_factory(rows):
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.all.return_value = rows
    factory = MagicMock(return_value=session)
    return factory, session


def test_absence_comes_from_complete_exact_query_and_session_is_closed():
    offered, profile = catalog(), uuid.uuid4()
    factory, session = empty_factory([(SimpleNamespace(is_blocked=False), None, None, None, None)])
    snapshot = load_preference_snapshot(factory, tenant_id=offered.tenant_id, proxy_user_id=profile, catalog=offered)
    assert len(snapshot.identities) == 1
    assert snapshot.identities[0].target is None and snapshot.identities[0].claim_id is None
    assert len(snapshot.binding_version) == 64
    with pytest.raises(FrozenInstanceError):
        snapshot.proxy_user_id = uuid.uuid4()
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "memory_claims.claim_fingerprint IN" in sql
    assert "proxy_users.tenant_id =" in sql and "memories.user_id = users.id" in sql
    assert "memory_claim_revisions.claim_id = memory_claims.id" in sql
    assert "FOR UPDATE" not in sql and "ORDER BY memories.importance_score" not in sql
    session.execute.assert_called_once()
    session.__exit__.assert_called_once()
    session.commit.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize("rows", [[], [(SimpleNamespace(is_blocked=True), None, None, None, None)],
                                     [(SimpleNamespace(is_blocked=False), None, None, None, False)]])
def test_missing_blocked_or_inactive_owner_is_not_empty_profile(rows):
    offered = catalog()
    factory, session = empty_factory(rows)
    with pytest.raises(PreferenceSnapshotUnavailable, match="principal_unavailable"):
        load_preference_snapshot(factory, tenant_id=offered.tenant_id, proxy_user_id=uuid.uuid4(), catalog=offered)
    session.__exit__.assert_called_once()


def test_foreign_catalog_and_constructed_invalid_catalog_fail_before_sql():
    offered, factory = catalog(), MagicMock()
    with pytest.raises(PreferenceSnapshotUnavailable, match="catalog_tenant_mismatch"):
        load_preference_snapshot(factory, tenant_id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), catalog=offered)
    poisoned = offered.model_copy(update={"properties": ()})
    with pytest.raises(ValidationError):
        load_preference_snapshot(factory, tenant_id=offered.tenant_id, proxy_user_id=uuid.uuid4(), catalog=poisoned)
    factory.assert_not_called()


def test_sql_failure_propagates_without_fabricating_absence():
    offered = catalog()
    factory, session = empty_factory([])
    session.execute.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        load_preference_snapshot(factory, tenant_id=offered.tenant_id, proxy_user_id=uuid.uuid4(), catalog=offered)
    session.__exit__.assert_called_once()
