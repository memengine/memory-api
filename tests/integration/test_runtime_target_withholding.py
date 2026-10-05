"""SQL-scoped partial reads, never classifier accuracy or production load claims."""
import copy
import uuid

import pytest

from api.db.models import ExtractionJob, Memory, ProxyUser
from api.schemas.preferences import PreferenceReadContext
from api.services.runtime_withholding import MAX_PENDING_FOOTPRINTS
from api.settings import get_settings
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_intent_admission import args
from tests.integration.test_preference_snapshots import PROJECT
from tests.integration.test_runtime_pending_visibility import read
from tests.integration.test_runtime_preference_ingestion import ingestion as _ingestion
from tests.integration.test_runtime_preference_worker import enable, seed, worker
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_preference_worker import sql_scope as _sql_scope
from tests.integration.test_runtime_recovery import mark_dead
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope, ingestion, forbid_real_providers = _sql_scope, _ingestion, _forbid


def prepare(scope, ingestion, monkeypatch, *, scope_ref="global", raw=None):
    old = seed(scope, monkeypatch)
    unrelated = fact(scope, "my exam is on October 18")
    enable(scope, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, model, _ = worker(scope)
    captured = instance.transactions.capture(**args(scope, job_id))
    instance.transactions.admit(raw or reply([intent(scope_ref=scope_ref)]), llm_service=model,
        **args(scope, job_id), capture_id=captured.capture_id)
    return old, unrelated, job_id, instance


def context(scope_ref="global"):
    return PreferenceReadContext(property_keys=["examples.language"], applicability=
        {"kind": "global"} if scope_ref == "global" else PROJECT)


@pytest.mark.parametrize("explicit", [False, True])
def test_known_preference_hold_returns_unrelated_fact_not_old_default(sql_scope, ingestion, monkeypatch, explicit):
    old, unrelated, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    items, visibility, decisions = read(sql_scope, [old, unrelated], context=context() if explicit else None)
    assert [m.id for m in items] == [str(unrelated)]
    assert visibility.scope == "affected_context" and visibility.state == "processing" and not decisions


def test_scoped_hold_blocks_global_fallback_but_not_global_read(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, _ = prepare(sql_scope, ingestion, monkeypatch, scope_ref="cedar")
    items, visibility, decisions = read(sql_scope, [old, unrelated], context=context("cedar"))
    assert [m.id for m in items] == [str(unrelated)] and not decisions
    assert visibility.scope == "affected_context"
    items, _, _ = read(sql_scope, [old, unrelated], context=context())
    assert str(old) in {m.id for m in items}


@pytest.mark.parametrize("change", ["missing", "digest", "foreign", "unknown", "route", "disabled", "overflow"])
def test_unknown_corrupt_or_unapproved_coverage_retains_full_hold(sql_scope, ingestion, monkeypatch, change):
    old, unrelated, job_id, _ = prepare(sql_scope, ingestion, monkeypatch)
    if change == "disabled":
        monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", False)
    elif change == "overflow":
        with sql_scope.factory.begin() as session:
            original = session.get(ExtractionJob, job_id)
            for _ in range(MAX_PENDING_FOOTPRINTS):
                from api.services.preference_intent_admission_service import _hash
                extra_id = uuid.uuid4()
                extra = copy.deepcopy(original.runtime_withholding_footprint)
                extra["job_id"] = str(extra_id)
                extra["projection_sha256"] = _hash({k: v for k, v in extra.items() if k != "projection_sha256"})
                session.add(ExtractionJob(id=extra_id, tenant_id=sql_scope.tenant_id,
                    proxy_user_id=sql_scope.proxy_id, external_user_id=sql_scope.external_id,
                    runtime_route=original.runtime_route, runtime_withholding_footprint=extra, payload={}))
    else:
        with sql_scope.factory.begin() as session:
            job = session.get(ExtractionJob, job_id)
            raw = copy.deepcopy(job.runtime_withholding_footprint)
            if change == "missing":
                raw = None
            elif change == "digest":
                raw["projection_sha256"] = "0" * 64
            elif change == "foreign":
                from api.services.preference_intent_admission_service import _hash
                raw["proxy_user_id"] = str(uuid.uuid4())
                raw["projection_sha256"] = _hash({k: v for k, v in raw.items() if k != "projection_sha256"})
            elif change == "route":
                job.runtime_route = "unsupported"
            else:
                from api.services.preference_intent_admission_service import _hash
                raw.update(coverage="user_learned_context", reason="unknown_preference", preference_fingerprints=[])
                raw["projection_sha256"] = _hash({k: v for k, v in raw.items() if k != "projection_sha256"})
            job.runtime_withholding_footprint = raw
    items, visibility, _ = read(sql_scope, [old, unrelated], context=context())
    assert not items and visibility.scope == "user_learned_context"


def test_any_unknown_job_widens_an_otherwise_known_owner(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    ingestion.send(messages=[{"role": "user", "content": "I have another update"}])
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert not items and visibility.scope == "user_learned_context"


def test_dead_job_preserves_partial_hold_and_blocked_owner_returns_nothing(sql_scope, ingestion, monkeypatch):
    old, unrelated, job_id, _ = prepare(sql_scope, ingestion, monkeypatch)
    mark_dead(sql_scope, job_id)
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert [m.id for m in items] == [str(unrelated)] and visibility.state == "unavailable"
    with sql_scope.factory.begin() as session:
        session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
    assert read(sql_scope, [old, unrelated])[0] == []


def test_coverage_loss_after_ranking_is_rechecked_in_final_snapshot(sql_scope, ingestion, monkeypatch):
    old, unrelated, job_id, _ = prepare(sql_scope, ingestion, monkeypatch)
    def lose_coverage():
        with sql_scope.factory.begin() as session:
            session.get(ExtractionJob, job_id).runtime_withholding_footprint = None
    items, visibility, _ = read(sql_scope, [old, unrelated], after_candidates=lose_coverage)
    assert not items and visibility.scope == "user_learned_context"


def test_applied_receipt_after_ranking_releases_hold_and_selects_current(sql_scope, ingestion, monkeypatch):
    old, unrelated, job_id, instance = prepare(sql_scope, ingestion, monkeypatch)
    items, visibility, _ = read(sql_scope, [old, unrelated], context=context(),
        after_candidates=lambda: instance.process(job_id))
    assert visibility is None and str(old) not in {m.id for m in items}
    assert any("Python" in m.content for m in items) and str(unrelated) in {m.id for m in items}


def test_acceptance_after_snapshot_is_visible_on_next_read(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    items, visibility, _ = read(sql_scope, [old, unrelated], after_guard=lambda: ingestion.send())
    assert [m.id for m in items] == [str(unrelated)] and visibility.scope == "affected_context"
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert not items and visibility.scope == "user_learned_context"


def test_fact_target_version_drift_widens_instead_of_guessing_successor(sql_scope, ingestion, monkeypatch):
    old = seed(sql_scope, monkeypatch)
    held_fact = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    # Capture both real targets, then fix a controlled UPDATE; no receipt shortcut.
    from tests.integration.test_preference_intent_transaction import FACT, TEXT
    from tests.integration.test_source_decision_postgres import _decision
    new_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": TEXT}])["job_id"])
    new, classifier, _ = worker(sql_scope, [_decision(held_fact, relation="supersedes")])
    captured = new.transactions.capture(**args(sql_scope, new_id))
    new.transactions.admit(reply(nonpreferences=[FACT]), llm_service=classifier,
        **args(sql_scope, new_id), capture_id=captured.capture_id)
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    unrelated = fact(sql_scope, "my city is Pune")
    items, visibility, _ = read(sql_scope, [old, held_fact, unrelated])
    assert [m.id for m in items] == [str(unrelated)] and visibility.scope == "affected_context"
    with sql_scope.factory.begin() as session:
        session.get(Memory, held_fact).content = "newer exam fact"
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert not items and visibility.scope == "user_learned_context" and visibility.state == "unavailable"
