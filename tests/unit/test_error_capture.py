from __future__ import annotations

import asyncio
import json

from api.tasks.extraction_tasks import _capture_error_detail
from api.tasks.extraction_tasks import classify_error


def test_classify_error_detects_llm_503() -> None:
    assert classify_error(RuntimeError("Gemini returned 503 Service Unavailable")) == "llm_provider_unavailable_503"


def test_classify_error_detects_rate_limit() -> None:
    assert classify_error(RuntimeError("429 rate limit quota exceeded")) == "llm_rate_limited_429"


def test_classify_error_detects_auth_failure() -> None:
    assert classify_error(RuntimeError("invalid api key 401")) == "llm_auth_failed"


def test_classify_error_detects_timeout() -> None:
    assert classify_error(asyncio.TimeoutError()) == "timeout"


def test_classify_error_detects_json_response_failure() -> None:
    error = json.JSONDecodeError("bad json", doc="{", pos=1)
    assert classify_error(error) == "llm_invalid_response"


def test_capture_error_detail_preserves_traceback_beginning_and_end() -> None:
    try:
        raise RuntimeError("final production failure line")
    except RuntimeError:
        detail = _capture_error_detail()

    assert "Traceback (most recent call last):" in detail
    assert "RuntimeError: final production failure line" in detail
