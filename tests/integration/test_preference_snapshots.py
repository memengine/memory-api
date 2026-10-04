"""Real PostgreSQL snapshots over synthetic owners; no provider or live wiring."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from api.db.models import Memory, MemoryClaim, MemoryClaimRevision, ProxyUser, User
from api.services.preference_claim_service import set_choice_state
from api.services.preference_snapshot_service import (
    PreferenceSnapshotUnavailable,
    load_preference_snapshot,
)
from api.tasks.extraction_tasks import _ensure_proxy_backing_user
from tests.integration.test_preference_choice_policy import withhold
from tests.integration.test_source_decision_postgres import _declare, _preference
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope
from tests.unit.test_preference_snapshot_service import catalog

sql_scope = _sql_scope
PROJECT = {"kind": "context", "namespace": "project", "key": "Cedar"}


@pytest.fixture(autouse=True)
def forbid_real_providers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("snapshot tests must not invoke a real provider")
    for method in ("complete", "complete_sync"):
        monkeypatch.setattr(f"api.services.llm_service.LLMService.{method}", forbidden)
    for method in ("embed", "embed_sync"):
        monkeypatch.setattr(f"api.services.embedding_service.EmbeddingService.{method}", forbidden)


def offered(scope):
    return catalog(scope.tenant_id, properties=[
        {"key": "examples.language", "description": "Usual example language"},
        {"key": "answer.detail", "description": "Usual answer detail"},
    ], scopes=[
        {"ref": "global", "description": "General", "applicability": {"kind": "global"}},
        {"ref": "cedar", "description": "Cedar only", "applicability": PROJECT},
    ])


def read(scope, **kwargs):
    return load_preference_snapshot(scope.factory, tenant_id=scope.tenant_id,
        proxy_user_id=scope.proxy_id, catalog=offered(scope), **kwargs)


def identity(snapshot, scope_ref="global", key="examples.language"):
    return next(item for item in snapshot.identities if (item.property_key, item.scope_ref) == (key, scope_ref))


def seed(owner, monkeypatch, **kwargs):
    result, _ = _declare(owner, monkeypatch, _preference(**kwargs))
    return uuid.UUID(result["stored_memories"][0]["id"])


def test_exact_coverage_scope_and_detached_stable_versions(sql_scope, monkeypatch):
    first = read(sql_scope)
    assert len(first.identities) == 4 and all(item.target is None for item in first.identities)
    global_id = seed(sql_scope, monkeypatch)
    project_id = seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    second = read(sql_scope)
    assert identity(second).target.memory_id == global_id
    assert identity(second, "cedar").target.memory_id == project_id
    assert identity(second, "cedar").target.value == "Python"
    assert identity(second, key="answer.detail").target is None
    assert second.binding_version != first.binding_version
    assert read(sql_scope) == second
    # A detached result remains usable after every loader session has closed.
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, project_id)
        memory.metadata_json = {**memory.metadata_json, "changed_after_snapshot": True}
    assert read(sql_scope).binding_version != second.binding_version
    assert identity(second, "cedar").target.memory_id == project_id


def test_absence_cannot_be_inferred_from_top_fifty_or_legacy_rows(sql_scope, monkeypatch):
    target_id = seed(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        target = session.get(Memory, target_id)
        target.importance_score = 1
        session.add_all([Memory(id=uuid.uuid4(), user_id=target.user_id, proxy_user_id=sql_scope.proxy_id,
            content=f"Unstructured historical preference {i}", category="preference", importance_score=10,
            confidence_score=0.5, embedding_id=str(uuid.uuid4()),
            embedding_model_id=target.embedding_model_id,
            source_conversation_id=target.source_conversation_id, metadata_json={}) for i in range(65)])
    snapshot = read(sql_scope)
    assert identity(snapshot).target.memory_id == target_id
    assert identity(snapshot, "cedar").target is None
    assert identity(snapshot, key="answer.detail").target is None


@pytest.mark.parametrize("change", ["ledger", "provenance"])
def test_authority_changes_are_bound_and_never_downgraded(sql_scope, monkeypatch, change):
    target_id = seed(sql_scope, monkeypatch)
    before = read(sql_scope)
    assert identity(before).target.authority == 20
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target_id)
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
        if change == "ledger":
            claim.authority_priority = 60
        else:
            provenance = {**memory.metadata_json["provenance"], "authority_rules": {"default_priority": 60}}
            memory.metadata_json = {**memory.metadata_json, "provenance": provenance}
    after = read(sql_scope)
    assert identity(after).target.authority == 60
    assert identity(after).binding_version != identity(before).binding_version


def test_hold_is_durable_target_and_generation_invalidates_old_snapshot(sql_scope, monkeypatch):
    target_id = seed(sql_scope, monkeypatch)
    before = read(sql_scope)
    (result, _), _ = withhold(sql_scope, monkeypatch, str(target_id))
    assert result["extraction_metadata"]["preference_choices"]["withheld"] == 1
    held = read(sql_scope)
    assert identity(held).target.choice_state == "current_choice_unresolved"
    assert identity(held).target.is_current
    assert held.binding_version != before.binding_version
    with sql_scope.factory.begin() as session:
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
        set_choice_state(session.get(Memory, target_id), claim, unresolved=False)
    after = read(sql_scope)
    assert after.binding_version not in {before.binding_version, held.binding_version}


@pytest.mark.parametrize("condition", ["archived", "expired", "scheduled"])
def test_ineligible_winner_is_present_not_verified_absence(sql_scope, monkeypatch, condition):
    target_id = seed(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target_id)
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
        if condition == "archived":
            memory.is_archived, claim.status = True, "archived"
            session.get(MemoryClaimRevision, claim.winning_revision_id).status = "archived"
        elif condition == "expired":
            memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            memory.effective_from = datetime.now(UTC) + timedelta(days=1)
    target = identity(read(sql_scope)).target
    assert target.memory_id == target_id and not target.is_current


@pytest.mark.parametrize("condition", [
    "null_winner", "bad_value", "bad_scope", "bad_category", "bad_revision", "missing_revision",
    "bad_state", "foreign_memory", "foreign_backing_user", "bad_authority", "malformed_metadata",
])
def test_inconsistent_ledger_never_becomes_absence(sql_scope, monkeypatch, condition):
    target_id = seed(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target_id)
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
        if condition == "null_winner":
            claim.active_memory_id = None
        elif condition == "bad_value":
            claim.active_value = "Go"
        elif condition == "bad_scope":
            claim.scope = PROJECT
        elif condition == "bad_category":
            claim.category = "fact"
        elif condition == "bad_revision":
            session.get(MemoryClaimRevision, claim.winning_revision_id).asserted_value = "Go"
        elif condition == "missing_revision":
            claim.winning_revision_id = None
        elif condition == "bad_state":
            claim.status = "disputed"
        elif condition == "bad_authority":
            claim.authority_priority = -1
        elif condition == "malformed_metadata":
            memory.metadata_json = {"provenance": {"extraction_evidence": {"explicit_preference_v1": {}}}}
        else:
            foreign_id = uuid.uuid4()
            session.add(ProxyUser(id=foreign_id, tenant_id=sql_scope.tenant_id, external_user_id=str(foreign_id),
                external_user_id_hash=str(foreign_id).replace("-", "") * 2))
            sql_scope.proxy_ids.append(foreign_id)
            session.flush()
            foreign_user = _ensure_proxy_backing_user(session, str(foreign_id))
            if condition == "foreign_memory":
                memory.proxy_user_id = foreign_id
            else:
                memory.user_id = foreign_user.id
    with pytest.raises(PreferenceSnapshotUnavailable):
        read(sql_scope)


@pytest.mark.parametrize("condition", ["blocked", "wrong_tenant", "missing", "inactive_backing"])
def test_unavailable_principal_cannot_obtain_empty_or_foreign_snapshot(sql_scope, monkeypatch, condition):
    seed(sql_scope, monkeypatch)
    profile_id = sql_scope.proxy_id
    if condition in {"blocked", "inactive_backing"}:
        with sql_scope.factory.begin() as session:
            if condition == "blocked":
                session.get(ProxyUser, profile_id).is_blocked = True
            else:
                session.scalars(select(User).where(User.external_id == f"proxy::{profile_id}")).one().is_active = False
    elif condition == "wrong_tenant":
        foreign_tenant = uuid.uuid4()
        with pytest.raises(PreferenceSnapshotUnavailable, match="principal_unavailable"):
            load_preference_snapshot(sql_scope.factory, tenant_id=foreign_tenant, proxy_user_id=profile_id,
                catalog=catalog(foreign_tenant))
        return
    else:
        profile_id = uuid.uuid4()
    with pytest.raises(PreferenceSnapshotUnavailable, match="principal_unavailable"):
        load_preference_snapshot(sql_scope.factory, tenant_id=sql_scope.tenant_id, proxy_user_id=profile_id,
            catalog=offered(sql_scope))


def test_read_is_one_select_no_lock_or_connection_retained(sql_scope, monkeypatch):
    target_id = seed(sql_scope, monkeypatch)
    statements = []
    engine = sql_scope.factory.kw["bind"]
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        before = read(sql_scope)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert "FOR UPDATE" not in statements[0].upper()
    assert engine.pool.checkedout() == 0
    # An independent connection can update immediately after the read.
    def change():
        with sql_scope.factory.begin() as session:
            memory = session.get(Memory, target_id)
            memory.metadata_json = {**memory.metadata_json, "concurrent_change": True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(change).result(timeout=5)
    assert read(sql_scope).binding_version != before.binding_version


def test_concurrent_first_writes_change_previously_verified_absence(sql_scope, monkeypatch):
    before = read(sql_scope)
    assert identity(before).target is None
    # This tests snapshot invalidation, not admission of a model proposal.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: _declare(sql_scope, monkeypatch, _preference(value))[0], ["C++", "Python"]))
    created_ids = {uuid.UUID(row["id"]) for result in results for row in result["stored_memories"]}
    after = read(sql_scope)
    assert identity(after).target.memory_id in created_ids
    assert identity(after).binding_version != identity(before).binding_version
    assert after.binding_version != before.binding_version
