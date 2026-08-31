from sqlalchemy import CheckConstraint

from api.db.models import SupportMemory


def test_support_type_source_constraint_accepts_allowlist_detection() -> None:
    constraint = next(
        item
        for item in SupportMemory.__table__.constraints
        if isinstance(item, CheckConstraint)
        and item.name == "ck_support_memories_support_type_source"
    )

    sql = str(constraint.sqltext)
    assert "'detected'" in sql
    assert "'allowed_detected'" in sql
    assert "'tenant_configured'" in sql
