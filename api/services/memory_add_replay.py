"""Request-binding helpers; MemoryService enforces durable SQL admission."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi.encoders import jsonable_encoder

from api.errors import APIError

REPLAY_BINDING_FIELD = "_memory_add_replay_binding"


def durable_add_eligible(*, tenant_id, source=None, agent_id=None,
                         trusted_submission_kind=None, turn_preparation=None):
    # Existing service-event/agent/MCP/prepared-turn contracts stay separate.
    return bool(tenant_id and source is None and agent_id is None
                and trusted_submission_kind is None and turn_preparation is None)


def request_key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def memory_add_replay_binding(
    *, tenant_id: str | None, owner_id: str | None,
    messages: list[dict[str, Any]], metadata: dict[str, Any],
    agent_id: str | None = None, source: dict[str, Any] | None = None,
    evidence_mode: str = "conversation_evidence", conversation_id: str | None = None,
    preference_claims: list[dict[str, Any]] | None = None,
    api_key_id: str | None = None, trusted_submission_kind: str | None = None,
    turn_preparation: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = jsonable_encoder({
        "messages": messages, "metadata": metadata, "agent_id": agent_id,
        "source": source, "evidence_mode": evidence_mode,
        "conversation_id": conversation_id, "preference_claims": preference_claims,
        "api_key_id": api_key_id, "trusted_submission_kind": trusted_submission_kind,
        "turn_preparation": turn_preparation,
    })
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    return {"version": 1, "tenant_id": tenant_id, "owner_id": owner_id,
            "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def verified_memory_add_replay(
    cached: dict[str, Any], expected: dict[str, Any],
) -> dict[str, Any]:
    binding = cached.get(REPLAY_BINDING_FIELD)
    if not isinstance(binding, dict) or binding.get("version") != 1:
        raise APIError(status_code=409, code="IDEMPOTENCY_409",
                       error="memory_add_replay_unverifiable")
    if binding != expected:
        raise APIError(status_code=409, code="IDEMPOTENCY_409",
                       error="memory_add_replay_mismatch")
    # This internal binding is not customer-visible job status/provenance.
    return {key: value for key, value in cached.items() if key != REPLAY_BINDING_FIELD}
