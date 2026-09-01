from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prelaunch_traffic_replay_v2.py"
SPEC = importlib.util.spec_from_file_location("prelaunch_traffic_replay_v2", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


def test_v2_fixture_covers_all_write_phases() -> None:
    workflows = replay.load_fixture(replay.DEFAULT_FIXTURE)["workflows"]
    events = [event for workflow in workflows for event in workflow["events"]]
    tenant_events = [event for workflow in workflows if workflow["feature"] != "universal" for event in workflow["events"]]
    assert 40 <= len(workflows) <= 70
    assert {workflow["feature"] for workflow in workflows} == replay.FEATURES
    assert len(events) >= 50
    assert {event["source"]["service"] for event in tenant_events} == {"support-service", "billing-service"}
    assert any(len(workflow["events"]) > 1 for workflow in workflows)


def test_payload_contracts_are_path_specific() -> None:
    workflows = replay.load_fixture(replay.DEFAULT_FIXTURE)["workflows"]
    general = next(item for item in workflows if item["feature"] == "general")
    universal = next(item for item in workflows if item["feature"] == "universal")
    general_payload = replay.build_payload(general, general["events"][0])
    universal_payload = replay.build_payload(universal, universal["events"][0])
    assert set(general_payload) <= {"external_user_id", "agent_id", "messages", "metadata", "source"}
    assert set(universal_payload) == {"messages", "metadata", "idempotency_key"}
    assert "external_user_id" not in universal_payload
    assert "source" not in universal_payload


def test_replay_has_no_conflict_resolution_paths() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "/resolve" not in source
    assert "/clarifications/" not in source


def test_service_key_environment_name_is_stable() -> None:
    assert replay.service_key_env("support-service") == "MEMORYOS_SERVICE_KEY_SUPPORT_SERVICE"
