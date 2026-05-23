from sqlalchemy.dialects import postgresql

from api.db.models import AgentApiKey
from api.db.models import Base
from api.db.models import GlobalAgent
from api.db.models import PermissionGrant
from api.db.models import UUIProxyLink
from api.db.models import UniversalMemory
from api.db.models import UniversalUser


def _constraint_sql(constraint) -> str:
    return str(constraint.sqltext.compile(dialect=postgresql.dialect()))


def test_cross_agent_tables_are_registered() -> None:
    assert {
        "universal_users",
        "global_agents",
        "agent_api_keys",
        "permission_grants",
        "universal_memories",
        "uui_proxy_link",
    }.issubset(Base.metadata.tables.keys())


def test_universal_user_contract_fields_exist() -> None:
    indexes = {index.name: index for index in UniversalUser.__table__.indexes}

    assert "otp_code" in UniversalUser.__table__.c
    assert "otp_expires_at" in UniversalUser.__table__.c
    assert str(UniversalUser.__table__.c.id.server_default.arg) == "gen_random_uuid()"
    assert str(UniversalUser.__table__.c.created_at.server_default.arg) == "now()"
    assert str(UniversalUser.__table__.c.is_active.server_default.arg) == "true"
    assert str(UniversalUser.__table__.c.memory_count.server_default.arg) == "0"
    assert indexes["ix_universal_users_uui_token"].unique is True
    assert indexes["ix_universal_users_email"].unique is True


def test_global_agent_contract_fields_exist() -> None:
    categories_column = GlobalAgent.__table__.c.default_categories_requested

    assert "redirect_uri" in GlobalAgent.__table__.c
    assert str(GlobalAgent.__table__.c.redirect_uri.server_default.arg) == "''"
    assert isinstance(categories_column.type, postgresql.ARRAY)
    assert str(categories_column.server_default.arg) == "'{}'::varchar[]"
    assert str(GlobalAgent.__table__.c.is_verified.server_default.arg) == "false"
    assert str(GlobalAgent.__table__.c.is_public.server_default.arg) == "true"
    assert str(GlobalAgent.__table__.c.is_active.server_default.arg) == "true"


def test_agent_api_key_contract_fields_exist() -> None:
    indexes = {index.name: index for index in AgentApiKey.__table__.indexes}

    assert AgentApiKey.__table__.c.key_prefix.nullable is False
    assert "ix_agent_api_keys_key_prefix" in indexes
    assert "ix_agent_api_keys_global_agent_active" in indexes


def test_unique_constraint_permission_grants() -> None:
    constraints = {constraint.name for constraint in PermissionGrant.__table__.constraints}

    assert "uq_permission_grants_user_agent" in constraints


def test_unique_constraint_proxy_link() -> None:
    constraints = {constraint.name for constraint in UUIProxyLink.__table__.constraints}

    assert "uq_uui_proxy_link_proxy_user" in constraints


def test_categories_check() -> None:
    checks = {
        constraint.name: _constraint_sql(constraint)
        for constraint in UniversalMemory.__table__.constraints
        if getattr(constraint, "sqltext", None) is not None
    }

    assert "ck_universal_memories_category" in checks
    assert "preference" in checks["ck_universal_memories_category"]
    assert "expertise" in checks["ck_universal_memories_category"]


def test_access_type_check() -> None:
    checks = {
        constraint.name: _constraint_sql(constraint)
        for constraint in PermissionGrant.__table__.constraints
        if getattr(constraint, "sqltext", None) is not None
    }

    assert "ck_permission_grants_access_type" in checks
    assert "read_only" in checks["ck_permission_grants_access_type"]
    assert "read_write" in checks["ck_permission_grants_access_type"]
