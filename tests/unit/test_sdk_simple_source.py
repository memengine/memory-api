from __future__ import annotations

import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

SDK_PATH = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from memoryos import Memory
from memoryos import MemorySource
from memoryos.types import AddRequest
from memoryos.types import ConversationMessage


def test_add_request_simple_mode_omits_source() -> None:
    request = AddRequest(
        external_user_id="user_123",
        messages=[ConversationMessage(role="user", content="I prefer short answers")],
    )

    payload = request.model_dump(mode="json", exclude_none=True)

    assert "source" not in payload
    assert payload["external_user_id"] == "user_123"


def test_memory_source_for_service_generates_safe_defaults() -> None:
    source = MemorySource.for_service("billing-service")

    assert source.service == "billing-service"
    assert source.event_id.startswith("sdk-")
    assert source.observed_at.tzinfo is not None
    assert source.scope == {}
    assert source.evidence == []


def test_memory_source_for_service_accepts_explicit_event() -> None:
    observed_at = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    source = Memory.source(
        "support-service",
        event_id="ticket-123",
        observed_at=observed_at,
        scope={"ticket_id": "TCK-123"},
        evidence=[{"source_type": "ticket", "reference": "TCK-123"}],
    )

    assert source.service == "support-service"
    assert source.event_id == "ticket-123"
    assert source.observed_at == observed_at
    assert source.scope == {"ticket_id": "TCK-123"}
    assert source.evidence[0].source_type == "ticket"
