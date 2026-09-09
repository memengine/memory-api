from __future__ import annotations

import pytest
from starlette.requests import Request

from api.errors import APIError
from api.routers.mcp import _public_tenant_mcp_external_user_id


def _request(*, tenant_id: str = "tenant_a", user_id: str = "user_a", marker: str = "public-v1") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/mcp/tenant/context",
            "headers": [(b"x-memoryos-mcp-client", marker.encode())],
        }
    )
    request.state.tenant_id = tenant_id
    request.state.user_id = user_id
    return request


def test_public_tenant_mcp_profile_is_stable_and_tenant_scoped() -> None:
    first = _public_tenant_mcp_external_user_id(_request())
    assert first == _public_tenant_mcp_external_user_id(_request())
    assert first != _public_tenant_mcp_external_user_id(_request(tenant_id="tenant_b"))
    assert first != _public_tenant_mcp_external_user_id(_request(user_id="user_b"))
    assert first.startswith("mcp:")


def test_public_tenant_mcp_profile_requires_public_marker() -> None:
    with pytest.raises(APIError) as error:
        _public_tenant_mcp_external_user_id(_request(marker=""))

    assert error.value.code == "MCP_403"
