from __future__ import annotations

import uuid

from api.services.mcp_universal_capability_service import (
    issue_universal_mcp_capability,
    verify_universal_mcp_capability,
)


def test_capability_round_trip_requires_the_configured_signing_secret(monkeypatch) -> None:
    monkeypatch.setenv("MCP_UNIVERSAL_CAPABILITY_SECRET", "test-capability-secret")
    user_id, agent_id, grant_id = (str(uuid.uuid4()) for _ in range(3))

    token = issue_universal_mcp_capability(
        user_uui_id=user_id,
        agent_id=agent_id,
        grant_id=grant_id,
    )

    assert verify_universal_mcp_capability(token) is not None
    monkeypatch.setenv("MCP_UNIVERSAL_CAPABILITY_SECRET", "other-secret")
    assert verify_universal_mcp_capability(token) is None


def test_capability_rejects_non_uuid_claims(monkeypatch) -> None:
    monkeypatch.setenv("MCP_UNIVERSAL_CAPABILITY_SECRET", "test-capability-secret")
    token = issue_universal_mcp_capability(
        user_uui_id="not-a-uuid",
        agent_id=str(uuid.uuid4()),
        grant_id=str(uuid.uuid4()),
    )

    assert verify_universal_mcp_capability(token) is None
