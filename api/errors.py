from __future__ import annotations

from typing import Any


class APIError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        error: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.code = code
        self.error = error
        self.details = details
