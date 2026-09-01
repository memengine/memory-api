from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "scripts" / "fixtures" / "prelaunch_traffic_v2.json"


def test_general_fixture_has_added_normal_conversation_diversity() -> None:
    workflows = json.loads(FIXTURE.read_text(encoding="utf-8"))["workflows"]
    added = [
        workflow
        for workflow in workflows
        if workflow["feature"] == "general" and 11 <= int(workflow["id"][1:]) <= 26
    ]
    categories = {workflow["category"] for workflow in added}
    messages = [
        message["content"].lower()
        for workflow in added
        for event in workflow["events"]
        for message in event["messages"]
        if message["role"] == "user"
    ]

    assert len(added) == 16
    assert sum(len(workflow["events"]) for workflow in added) == 22
    assert {"uncertain_preference", "tentative_goal", "active_goal", "recurring_procedure", "relationship"} <= categories
    assert sum(any(term in text for term in ("maybe", "may ", "might", "considering", "not sure", "still deciding")) for text in messages) >= 6
    assert sum("every " in text or "each month" in text or "before every" in text for text in messages) >= 5
    assert sum(any(term in text for term in ("sister", "mentor", "partner", "daughter")) for text in messages) >= 4
    assert sum("active goal" in text or "actively" in text for text in messages) >= 4


def test_added_events_have_unique_source_identities() -> None:
    workflows = json.loads(FIXTURE.read_text(encoding="utf-8"))["workflows"]
    identities = [
        (event["source"]["service"], event["source"]["event_id"])
        for workflow in workflows
        if workflow["feature"] != "universal"
        for event in workflow["events"]
    ]
    assert len(identities) == len(set(identities))
