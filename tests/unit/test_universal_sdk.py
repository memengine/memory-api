from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import httpx


SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from memoryos.universal import UniversalMemory  # noqa: E402


def test_consent_url_encodes_redirect_uri_and_state() -> None:
    url = UniversalMemory.consent_url(
        agent_id="agent-123",
        redirect_uri="https://example.com/callback?next=/home",
        state="session-abc",
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "consent.memoryos.io"
    assert parsed.path == "/consent"
    assert params["agent_id"] == ["agent-123"]
    assert params["redirect_uri"] == ["https://example.com/callback?next=/home"]
    assert params["state"] == ["session-abc"]


def test_consent_url_can_use_hosted_completion_page_without_redirect_uri() -> None:
    url = UniversalMemory.consent_url(
        agent_id="agent-123",
        state="session-abc",
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "consent.memoryos.io"
    assert parsed.path == "/consent"
    assert params["agent_id"] == ["agent-123"]
    assert "redirect_uri" not in params
    assert params["state"] == ["session-abc"]


def test_consent_url_can_preselect_categories() -> None:
    url = UniversalMemory.consent_url(
        agent_id="agent-123",
        categories=["preference", "goal", "preference", " "],
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert params["agent_id"] == ["agent-123"]
    assert params["categories"] == ["preference,goal"]


def test_add_uses_universal_auth_headers_and_returns_add_result() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        assert request.url.path == "/v1/universal/memories/add"
        return httpx.Response(
            200,
            headers={
                "X-MemoryOS-Quota-Mode": "FULL",
                "X-MemoryOS-Circuit-Status": "HEALTHY",
                "X-MemoryOS-Processing": "normal",
            },
            json={
                "request_id": "req_123",
                "timestamp": "2026-04-23T00:00:00Z",
                "job_id": "job_123",
                "status": "queued",
                "blocked_reason": None,
                "retry_after_seconds": None,
                "budget_remaining_pct": 0.95,
                "processing_eta_seconds": None,
                "processing_status": "normal",
            },
        )

    client = UniversalMemory(
        agent_api_key="agent_sk_test_123",
        uui_token="uui_test_123",
        base_url="https://api.example.com",
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.com",
        headers={
            "Authorization": "ApiKey agent_sk_test_123",
            "X-MemoryOS-UUI": "uui_test_123",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "memoryos-python/0.1.0",
        },
    )

    result = client.add([{"role": "user", "content": "Remember this."}])

    assert captured_headers["authorization"] == "ApiKey agent_sk_test_123"
    assert captured_headers["x-memoryos-uui"] == "uui_test_123"
    assert result.job_id == "job_123"
    assert result.status == "queued"


def test_get_returns_categories_and_permission_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/universal/memories/retrieve"
        return httpx.Response(
            200,
            headers={
                "X-MemoryOS-Quota-Mode": "FULL",
                "X-MemoryOS-Circuit-Status": "HEALTHY",
            },
            json={
                "request_id": "req_456",
                "timestamp": "2026-04-23T00:00:00Z",
                "data": [],
                "cached": False,
                "system_prompt_addition": "",
                "permission_error": "no_grant_for_user",
                "categories_available": ["preference", "expertise"],
                "is_passthrough": False,
            },
        )

    client = UniversalMemory(
        agent_api_key="agent_sk_test_123",
        uui_token="uui_test_123",
        base_url="https://api.example.com",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.com")

    result = client.get("What do you know about this user?")

    assert result.permission_status == "no_grant_for_user"
    assert result.categories_available == ["preference", "expertise"]
