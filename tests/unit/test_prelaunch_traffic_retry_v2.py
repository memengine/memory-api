from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prelaunch_traffic_retry_v2", ROOT / "scripts" / "prelaunch_traffic_retry_v2.py"
)
assert SPEC and SPEC.loader
retry = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = retry
SPEC.loader.exec_module(retry)


def test_scoped_fixture_preserves_users_and_changes_tenant_event_ids() -> None:
    source = retry.replay_v2.load_fixture(retry.replay_v2.DEFAULT_FIXTURE)
    scoped = retry.scoped_fixture(source, "retry01")
    original = next(w for w in source["workflows"] if w["feature"] == "edtech")
    changed = next(w for w in scoped["workflows"] if w["id"] == original["id"])
    assert changed["user_id"] == original["user_id"]
    assert changed["events"][0]["source"]["event_id"] == (
        original["events"][0]["source"]["event_id"] + "-retry01"
    )
