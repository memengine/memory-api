from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa


def assert_no_remaining_nulls(
    bind: Any,
    *,
    table_name: str,
    column_name: str,
    id_column: str = "id",
    extra_where_sql: str | None = None,
    sample_limit: int = 5,
) -> None:
    where_clause = f"{column_name} IS NULL"
    if extra_where_sql:
        where_clause = f"{where_clause} AND ({extra_where_sql})"

    count_stmt = sa.text(
        f"""
        SELECT COUNT(*)
        FROM {table_name}
        WHERE {where_clause}
        """
    )
    remaining = int(bind.execute(count_stmt).scalar_one())
    if remaining == 0:
        return

    sample_stmt = sa.text(
        f"""
        SELECT {id_column}
        FROM {table_name}
        WHERE {where_clause}
        ORDER BY {id_column}
        LIMIT :limit
        """
    )
    sample_rows: Sequence[Any] = bind.execute(sample_stmt, {"limit": sample_limit}).scalars().all()
    sample_ids = [str(value) for value in sample_rows]
    raise RuntimeError(
        "Contract migration blocked: "
        f"{remaining} rows remain with NULL {column_name} in {table_name}. "
        f"Run the backfill and verify completion before applying the contract phase. "
        f"Sample {id_column} values: {sample_ids}"
    )


def assert_backfill_completed(
    bind: Any,
    *,
    task_name: str,
) -> None:
    result = bind.execute(
        sa.text(
            """
            SELECT status, processed_rows, total_rows
            FROM backfill_jobs
            WHERE task_name = :task_name
            ORDER BY started_at DESC
            LIMIT 1
            """
        ),
        {"task_name": task_name},
    ).mappings().first()
    if result is None:
        raise RuntimeError(
            f"Contract migration blocked: no backfill_jobs row found for task '{task_name}'. "
            "Run the backfill first and verify completion."
        )

    if result["status"] != "complete":
        raise RuntimeError(
            "Contract migration blocked: "
            f"latest backfill job for '{task_name}' is status='{result['status']}', "
            f"processed_rows={result['processed_rows']}, total_rows={result['total_rows']}."
        )
