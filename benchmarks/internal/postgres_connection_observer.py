from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import psycopg2

from api.db.database import get_sync_database_url


def sanitized_query_shape(query: str) -> str:
    compact = " ".join(str(query or "").split())
    compact = re.sub(r"'(?:''|[^'])*'", "?", compact)
    compact = re.sub(r"\b\d+(?:\.\d+)?\b", "?", compact)
    return compact[:240]


def database_url_from_compose(*, project: str, compose_file: Path, env_file: Path) -> str:
    if project != "memoryos-scale":
        raise RuntimeError("PostgreSQL observer requires the memoryos-scale Compose project.")
    completed = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose_file), "--env-file", str(env_file), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to read disposable Compose database configuration.")
    config = json.loads(completed.stdout)
    service = config["services"]["postgres"]
    environment = service.get("environment", {})
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)
    ports = service.get("ports", [])
    published = next(str(port["published"]) for port in ports if int(port["target"]) == 5432)
    user = str(environment["POSTGRES_USER"])
    password = quote(str(environment["POSTGRES_PASSWORD"]), safe="")
    database = str(environment["POSTGRES_DB"])
    if database != "memoryos_scale":
        raise RuntimeError("PostgreSQL observer requires the disposable memoryos_scale database.")
    return f"postgresql+asyncpg://{quote(user, safe='')}:{password}@127.0.0.1:{published}/{database}"


def require_safe_environment() -> None:
    if os.getenv("APP_ENV", "").strip().lower() != "benchmark":
        raise RuntimeError("PostgreSQL observer requires APP_ENV=benchmark.")
    if os.getenv("MEMORYOS_SCALE_DEDICATED") != "1":
        raise RuntimeError("PostgreSQL observer requires the dedicated scale marker.")
    if "/memoryos_scale" not in os.getenv("DATABASE_URL", ""):
        raise RuntimeError("PostgreSQL observer requires the disposable memoryos_scale database.")


def observe(*, duration_seconds: int, interval_seconds: float, output: Path) -> dict:
    require_safe_environment()
    samples: list[dict] = []
    failures: list[dict] = []
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        captured_at = datetime.now(UTC).isoformat()
        try:
            dsn = get_sync_database_url().replace("postgresql+psycopg2://", "postgresql://", 1)
            connection = psycopg2.connect(dsn, connect_timeout=2)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                    GROUP BY state
                    """
                )
                states = {str(state or "unknown"): int(count) for state, count in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT COALESCE(NULLIF(application_name, ''), 'unlabeled'), state, count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                    GROUP BY application_name, state
                    """
                )
                applications: dict[str, dict[str, int]] = {}
                for application_name, state, count in cursor.fetchall():
                    applications.setdefault(str(application_name), {})[str(state or "unknown")] = int(count)
                cursor.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
                max_connections = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT pid, COALESCE(NULLIF(application_name, ''), 'unlabeled'), state,
                           round(extract(epoch FROM (clock_timestamp() - xact_start))::numeric, 3),
                           round(extract(epoch FROM (clock_timestamp() - query_start))::numeric, 3),
                           wait_event_type, wait_event,
                           pg_blocking_pids(pid),
                           backend_xid::text, backend_xmin::text,
                           left(regexp_replace(query, '[[:space:]]+', ' ', 'g'), 240)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND xact_start IS NOT NULL
                      AND (state = 'idle in transaction'
                           OR clock_timestamp() - xact_start >= interval '2 seconds')
                    ORDER BY xact_start
                    LIMIT 100
                    """
                )
                long_transactions = [
                    {
                        "pid": int(pid),
                        "application_name": str(application_name),
                        "state": str(state or "unknown"),
                        "transaction_age_seconds": float(transaction_age or 0),
                        "query_age_seconds": float(query_age or 0),
                        "wait_event_type": str(wait_event_type) if wait_event_type else None,
                        "wait_event": str(wait_event) if wait_event else None,
                        "blocking_pids": [int(value) for value in (blocking_pids or [])],
                        "backend_xid": str(backend_xid) if backend_xid else None,
                        "backend_xmin": str(backend_xmin) if backend_xmin else None,
                        "query_shape": sanitized_query_shape(str(query_shape)),
                    }
                    for pid, application_name, state, transaction_age, query_age,
                    wait_event_type, wait_event, blocking_pids, backend_xid, backend_xmin,
                    query_shape in cursor.fetchall()
                ]
            connection.close()
            samples.append({
                "captured_at": captured_at,
                "total": sum(states.values()),
                "states": states,
                "applications": applications,
                "max_connections": max_connections,
                "long_transactions": long_transactions,
            })
        except Exception as exc:
            failures.append({"captured_at": captured_at, "reason": type(exc).__name__, "message": str(exc)[:200]})
        time.sleep(interval_seconds)

    totals = [sample["total"] for sample in samples]
    payload = {
        "schema_version": "1.0",
        "duration_seconds": duration_seconds,
        "interval_seconds": interval_seconds,
        "sample_count": len(samples),
        "failure_count": len(failures),
        "first_total": totals[0] if totals else None,
        "last_total": totals[-1] if totals else None,
        "max_total": max(totals) if totals else None,
        "samples": samples,
        "failures": failures,
        "holdout_used": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compose-project")
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.compose_project or args.compose_file or args.env_file:
        if not (args.compose_project and args.compose_file and args.env_file):
            raise RuntimeError("Compose project, Compose file, and env file must be provided together.")
        os.environ["DATABASE_URL"] = database_url_from_compose(
            project=args.compose_project,
            compose_file=args.compose_file,
            env_file=args.env_file,
        )
    payload = observe(duration_seconds=args.duration_seconds, interval_seconds=args.interval_seconds, output=args.output)
    print(json.dumps({key: payload[key] for key in ("sample_count", "failure_count", "first_total", "last_total", "max_total")}))


if __name__ == "__main__":
    main()
