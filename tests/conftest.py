from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TEST_ADMIN_SECRET = "test-secret-32-chars-minimum-xyz!"

os.environ["ADMIN_SECRET"] = TEST_ADMIN_SECRET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _ensure_admin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_SECRET", TEST_ADMIN_SECRET)
