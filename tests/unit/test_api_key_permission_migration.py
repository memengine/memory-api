from __future__ import annotations

from api.db.migrations.versions import backfill_api_key_permissions


def test_legacy_api_key_backfill_uses_safe_sdk_permissions(monkeypatch) -> None:
    statements: list[str] = []
    monkeypatch.setattr(
        backfill_api_key_permissions.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    backfill_api_key_permissions.upgrade()

    assert len(statements) == 1
    sql = statements[0]
    assert "cardinality(permissions) = 0" in sql
    assert "'read', 'write'" in sql
    assert "admin" not in sql
    assert "delete" not in sql
    assert backfill_api_key_permissions.down_revision == "phase3a_structured_claim"
