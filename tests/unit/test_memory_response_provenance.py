from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from api.routers.memories import _memory_to_data


def test_list_memory_data_returns_external_conversation_id_in_provenance() -> None:
    now = datetime.now(UTC)
    memory = SimpleNamespace(
        id=uuid.uuid4(),
        content="User prefers concise answers.",
        category=SimpleNamespace(value="preference"),
        importance_score=7.0,
        confidence_score=0.92,
        created_at=now,
        updated_at=now,
        last_accessed_at=now,
        access_count=0,
        is_archived=False,
        agent_id=None,
        previous_version_id=None,
        source_conversation_id=uuid.uuid4(),
        source_event_id=None,
        metadata_json={
            "provenance": {
                "attestation": "client_asserted",
                "external_conversation_id": "vscode-chat-2026-09-12-01",
            }
        },
    )

    response = _memory_to_data(memory)

    assert response.provenance == {
        "attestation": "client_asserted",
        "external_conversation_id": "vscode-chat-2026-09-12-01",
    }
