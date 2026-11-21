"""Inspect one memory and its revision history.

Read-only: the script calls only

    GET /v1/memories/{memory_id}
    GET /v1/memories/{memory_id}/history

Live run (API key stays in the environment):

    export MEMORYOS_API_KEY=...
    export MEMORYOS_BASE_URL=http://127.0.0.1:18000   # optional, this is the default
    python examples/memory_history/inspect_memory_history.py <memory_id>

Mock run (no network; serves the synthetic record in ./fixtures):

    python examples/memory_history/inspect_memory_history.py --mock
    python examples/memory_history/inspect_memory_history.py --mock --missing
    python examples/memory_history/inspect_memory_history.py --mock --empty-history

Exit codes: 0 printed, 1 usage or environment problem, 2 memory not found,
3 any other API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC_MEMORY_ID = "mem_synthetic_0001"
DEFAULT_BASE_URL = "http://127.0.0.1:18000"


class MemoryAPIError(RuntimeError):
    """An error response from the MemoryOS API."""

    def __init__(
        self,
        status: int,
        code: str,
        error: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(error)
        self.status = status
        self.code = code
        self.error = error
        self.details = details


class Transport(Protocol):
    """The only thing this example needs from a client: a read-only GET."""

    def get(self, path: str) -> dict[str, Any]: ...


class _RejectRedirects(HTTPRedirectHandler):
    """Do not reissue requests that carry the API key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """Minimal read-only HTTP transport (standard library only)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._opener = build_opener(_RejectRedirects())

    def get(self, path: str) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            method="GET",
            headers={
                "Authorization": f"ApiKey {self.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise MemoryAPIError(
                    error.code, "REDIRECT_REJECTED", "API redirects are not followed"
                ) from None
            body = error.read().decode("utf-8", "replace")
            raise _error_from_body(error.code, body) from None
        except URLError as error:
            raise RuntimeError(
                f"could not reach {self.base_url}: {error.reason}"
            ) from None


class MockTransport:
    """Serves the synthetic fixtures so the example runs without a server."""

    def __init__(self, *, missing: bool = False, empty_history: bool = False) -> None:
        self.missing = missing
        self.empty_history = empty_history

    def get(self, path: str) -> dict[str, Any]:
        if self.missing:
            raise MemoryAPIError(
                404,
                "MEM_404",
                "memory_not_found",
                {"memory_id": SYNTHETIC_MEMORY_ID},
            )
        if path.endswith("/history"):
            data: Any = [] if self.empty_history else _read_fixture("history.json")
        else:
            data = _read_fixture("memory.json")
        return {"data": data, "request_id": "mock-request", "timestamp": "mock"}


def _read_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _error_from_body(status: int, body: str) -> MemoryAPIError:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return MemoryAPIError(status, "UNKNOWN", body.strip() or f"HTTP {status}")
    return MemoryAPIError(
        status,
        str(payload.get("code", "UNKNOWN")),
        str(payload.get("error", f"HTTP {status}")),
        payload.get("details"),
    )


def describe_source(memory: dict[str, Any]) -> str:
    """Where the current value came from, using the fields the API returns."""
    parts: list[str] = []
    if memory.get("source_event_id"):
        parts.append(f"event {memory['source_event_id']}")
    if memory.get("source_conversation_id"):
        parts.append(f"conversation {memory['source_conversation_id']}")
    if memory.get("agent_id"):
        parts.append(f"agent {memory['agent_id']}")
    provenance = memory.get("provenance")
    if provenance:
        parts.append(
            f"provenance {json.dumps(provenance, ensure_ascii=False, sort_keys=True)}"
        )
    return ", ".join(parts) if parts else "not recorded"


def print_report(memory: dict[str, Any], revisions: list[dict[str, Any]]) -> None:
    print(f"Memory {memory.get('id')}")
    print(f"  current value : {memory.get('content')}")
    print(f"  source        : {describe_source(memory)}")
    print(f"  updated at    : {memory.get('updated_at') or memory.get('created_at')}")
    print(
        "  category      : "
        f"{memory.get('category')} "
        f"(importance {memory.get('importance_score')}, "
        f"confidence {memory.get('confidence_score')})"
    )

    if not revisions:
        print("  revisions     : none recorded")
        return

    print(f"  revisions     : {len(revisions)}")
    for revision in revisions:
        reason = revision.get("change_reason") or "-"
        print(
            f"    v{revision.get('version_number')}  "
            f"{revision.get('created_at')}  "
            f"{revision.get('change_type')}  "
            f"reason={reason}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print one memory and its revision history (read-only).",
    )
    parser.add_argument(
        "memory_id",
        nargs="?",
        help=f"memory id to inspect (defaults to {SYNTHETIC_MEMORY_ID} with --mock)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="serve the synthetic fixtures in ./fixtures instead of calling the API",
    )
    parser.add_argument(
        "--missing",
        action="store_true",
        help="with --mock: pretend the record does not exist",
    )
    parser.add_argument(
        "--empty-history",
        action="store_true",
        help="with --mock: pretend the record has no revisions",
    )
    return parser.parse_args(argv)


def build_transport(args: argparse.Namespace) -> tuple[Transport, str]:
    if args.mock:
        transport = MockTransport(
            missing=args.missing, empty_history=args.empty_history
        )
        return transport, args.memory_id or SYNTHETIC_MEMORY_ID

    if not args.memory_id:
        raise SystemExit("memory_id is required unless --mock is used")

    api_key = os.environ.get("MEMORYOS_API_KEY")
    if not api_key:
        raise SystemExit(
            "MEMORYOS_API_KEY is not set (the key stays in the environment)"
        )

    base_url = (
        os.environ.get("MEMORYOS_BASE_URL")
        or os.environ.get("MEMORYOS_API_URL")
        or DEFAULT_BASE_URL
    )
    return HttpTransport(base_url, api_key), args.memory_id


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transport, memory_id = build_transport(args)

    try:
        memory = transport.get(f"/v1/memories/{memory_id}")["data"]
        revisions = transport.get(f"/v1/memories/{memory_id}/history")["data"]
    except MemoryAPIError as error:
        if error.status == 404:
            print(f"Memory {memory_id} was not found ({error.code}).", file=sys.stderr)
            return 2
        print(f"API error {error.status} {error.code}: {error.error}", file=sys.stderr)
        return 3
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 3

    print_report(memory, revisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
