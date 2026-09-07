from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

MCP_UNIVERSAL_AUDIENCE = "memoryos-mcp-universal"
MCP_UNIVERSAL_ISSUER = "memoryos-api"
MCP_UNIVERSAL_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class UniversalMcpCapability:
    user_uui_id: str
    agent_id: str
    grant_id: str


def _secret() -> str:
    value = str(os.getenv("MCP_UNIVERSAL_CAPABILITY_SECRET") or "").strip()
    if not value:
        raise ValueError("MCP_UNIVERSAL_CAPABILITY_SECRET is not configured")
    return value


def issue_universal_mcp_capability(*, user_uui_id: str, agent_id: str, grant_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": MCP_UNIVERSAL_ISSUER,
            "aud": MCP_UNIVERSAL_AUDIENCE,
            "sub": str(user_uui_id),
            "agent_id": str(agent_id),
            "grant_id": str(grant_id),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=MCP_UNIVERSAL_TTL_SECONDS)).timestamp()),
        },
        _secret(),
        algorithm="HS256",
    )


def verify_universal_mcp_capability(token: str) -> UniversalMcpCapability | None:
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            audience=MCP_UNIVERSAL_AUDIENCE,
            issuer=MCP_UNIVERSAL_ISSUER,
        )
        user_uui_id = str(payload.get("sub") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        grant_id = str(payload.get("grant_id") or "").strip()
        if not user_uui_id or not agent_id or not grant_id:
            return None
        uuid.UUID(user_uui_id)
        uuid.UUID(agent_id)
        uuid.UUID(grant_id)
        return UniversalMcpCapability(user_uui_id=user_uui_id, agent_id=agent_id, grant_id=grant_id)
    except (JWTError, ValueError, TypeError, AttributeError):
        return None
