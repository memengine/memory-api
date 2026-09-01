from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prelaunch_traffic_replay.py"
SPEC = importlib.util.spec_from_file_location("prelaunch_traffic_replay", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_fixture_has_expected_shape_and_diversity() -> None:
    data = replay.load_fixture(replay.DEFAULT_FIXTURE)
    workflows = data["workflows"]
    events = [event for workflow in workflows for event in workflow["events"]]

    assert 40 <= len(workflows) <= 60
    assert len({workflow["user_id"] for workflow in workflows}) == len(workflows)
    assert len({workflow["category"] for workflow in workflows}) >= 8
    assert len({event["source"]["service"] for event in events}) >= 7
    assert any(len(workflow["events"]) > 1 for workflow in workflows)
    assert any(event.get("duplicate_of") for event in events)
    assert all(event["messages"] for event in events)


def test_payload_uses_supported_ingestion_contract() -> None:
    workflow = replay.load_fixture(replay.DEFAULT_FIXTURE)["workflows"][0]
    payload = replay.build_payload(workflow, workflow["events"][0])

    assert set(payload) <= {"external_user_id", "agent_id", "messages", "metadata", "source"}
    assert payload["metadata"]["traffic_class"] == "synthetic_prelaunch"
    assert payload["source"]["event_id"]
    assert payload["source"]["service"]
    assert payload["source"]["observed_at"].endswith("Z")
