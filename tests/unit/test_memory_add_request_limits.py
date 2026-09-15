from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas.requests import MemoryAddRequest


def test_memory_add_request_accepts_documented_message_limits() -> None:
    request = MemoryAddRequest(
        external_user_id="customer-123",
        messages=[
            {
                "role": "user",
                "content": "x" * 16_000,
                "external_turn_id": "support-884:turn-12",
                "source_kind": "direct_user_input",
            }
        ],
    )

    assert request.messages[0].external_turn_id == "support-884:turn-12"
    assert request.messages[0].source_kind == "direct_user_input"


def test_memory_add_request_rejects_excessive_message_count_or_content() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(
            external_user_id="customer-123",
            messages=[{"role": "user", "content": "ok"}] * 65,
        )

    with pytest.raises(ValidationError):
        MemoryAddRequest(
            external_user_id="customer-123",
            messages=[{"role": "user", "content": "x" * 16_001}],
        )
