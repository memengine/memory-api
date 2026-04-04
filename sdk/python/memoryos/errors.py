from __future__ import annotations

from typing import Any


class MemoryOSError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.details = details


class AuthError(MemoryOSError):
    pass


class RateLimitError(MemoryOSError):
    pass


class NotFoundError(MemoryOSError):
    pass


def map_sdk_error(
    *,
    status_code: int | None,
    message: str,
    code: str | None = None,
    request_id: str | None = None,
    details: Any | None = None,
) -> MemoryOSError:
    error_cls: type[MemoryOSError]
    if status_code == 401:
        error_cls = AuthError
    elif status_code == 404:
        error_cls = NotFoundError
    elif status_code == 429:
        error_cls = RateLimitError
    else:
        error_cls = MemoryOSError

    return error_cls(
        message,
        status_code=status_code,
        code=code,
        request_id=request_id,
        details=details,
    )
