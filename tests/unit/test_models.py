from sqlalchemy.dialects import postgresql

from api.db.models import ApiKey
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import Base
from api.db.models import CallQualityLog
from api.db.models import Conversation
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import QuotaMode
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.db.models import User


def test_expected_tables_are_registered() -> None:
    assert sorted(Base.metadata.tables.keys()) == [
        "agent_api_keys",
        "agents",
        "api_deprecated_fields",
        "api_keys",
        "audit_logs",
        "call_quality_log",
        "clarification_queue",
        "conversations",
        "cross_user_conflicts",
        "dead_letter_jobs",
        "embedding_models",
        "extraction_jobs",
        "global_agents",
        "llm_provider_config",
        "memories",
        "memory_versions",
        "permission_grants",
        "proxy_users",
        "regions",
        "shared_context_signals",
        "tenant_budgets",
        "tenant_deprecation_usage",
        "tenants",
        "universal_memories",
        "universal_memory_versions",
        "universal_users",
        "user_memory_flags",
        "users",
        "uui_proxy_link",
        "vector_sync_outbox",
    ]


def test_memory_indexes_match_requested_shape() -> None:
    dialect = postgresql.dialect()
    indexes = {index.name: index for index in Memory.__table__.indexes}

    assert {
        "ix_memories_user_category",
        "ix_memories_user_importance_score_desc",
        "ix_memories_user_last_accessed_at_desc",
        "ix_memories_user_is_archived",
        "ix_memories_metadata_gin",
    }.issubset(indexes)

    importance_sql = [
        str(expression.compile(dialect=dialect))
        for expression in indexes["ix_memories_user_importance_score_desc"].expressions
    ]
    last_accessed_sql = [
        str(expression.compile(dialect=dialect))
        for expression in indexes["ix_memories_user_last_accessed_at_desc"].expressions
    ]

    assert importance_sql == ["memories.user_id", "memories.importance_score DESC"]
    assert last_accessed_sql == ["memories.user_id", "memories.last_accessed_at DESC"]
    assert indexes["ix_memories_metadata_gin"].dialect_options["postgresql"]["using"] == "gin"


def test_server_side_defaults_are_defined_for_uuid_and_timestamps() -> None:
    assert str(Memory.__table__.c.id.server_default.arg) == "gen_random_uuid()"
    assert str(Memory.__table__.c.created_at.server_default.arg) == "now()"
    assert str(Memory.__table__.c.updated_at.server_default.arg) == "now()"
    assert str(Memory.__table__.c.last_accessed_at.server_default.arg) == "now()"


def test_postgres_specific_memory_columns_are_present() -> None:
    metadata_column = Memory.__table__.c.metadata
    conversation_fk = next(iter(Memory.__table__.c.source_conversation_id.foreign_keys))

    assert isinstance(metadata_column.type, postgresql.JSONB)
    assert str(metadata_column.server_default.arg) == "'{}'::jsonb"
    assert conversation_fk.target_fullname == "conversations.id"


def test_api_key_permissions_and_defaults_match_contract() -> None:
    permissions_column = ApiKey.__table__.c.permissions
    rate_limit_column = ApiKey.__table__.c.rate_limit_per_minute
    key_prefix_column = ApiKey.__table__.c.key_prefix

    assert isinstance(permissions_column.type, postgresql.ARRAY)
    assert str(permissions_column.server_default.arg) == "'{}'::varchar[]"
    assert str(rate_limit_column.server_default.arg) == "60"
    assert key_prefix_column.nullable is True


def test_user_counter_and_audit_log_metadata_defaults_match_contract() -> None:
    memory_count_column = User.__table__.c.memory_count
    audit_metadata_column = AuditLog.__table__.c.metadata

    assert str(memory_count_column.server_default.arg) == "0"
    assert isinstance(audit_metadata_column.type, postgresql.JSONB)
    assert str(audit_metadata_column.server_default.arg) == "'{}'::jsonb"


def test_tenant_budget_and_quality_log_defaults_match_contract() -> None:
    rate_limit_column = TenantBudget.__table__.c.rate_limit_per_user_per_minute
    quality_log_similarity_column = CallQualityLog.__table__.c.semantic_similarity
    write_calls_column = TenantBudget.__table__.c.write_calls
    write_call_limit_column = TenantBudget.__table__.c.write_call_limit
    read_calls_column = TenantBudget.__table__.c.read_calls

    assert str(rate_limit_column.server_default.arg) == "10"
    assert str(write_calls_column.server_default.arg) == "0"
    assert write_call_limit_column.nullable is True
    assert str(read_calls_column.server_default.arg) == "0"
    assert quality_log_similarity_column.nullable is True


def test_tenant_defaults_match_contract() -> None:
    metadata_column = Tenant.__table__.c.metadata

    assert str(Tenant.__table__.c.created_at.server_default.arg) == "now()"
    assert str(Tenant.__table__.c.plan_tier.server_default.arg) == "'starter'"
    assert str(Tenant.__table__.c.is_active.server_default.arg) == "true"
    assert Tenant.__table__.c.clerk_org_id.nullable is True
    assert Tenant.__table__.c.alert_webhook_url.type.length == 500
    assert isinstance(metadata_column.type, postgresql.JSONB)
    assert str(metadata_column.server_default.arg) == "'{}'::jsonb"


def test_enums_expose_requested_values() -> None:
    assert [member.value for member in MemoryCategory] == [
        "preference",
        "fact",
        "goal",
        "procedure",
        "relationship",
        "expertise",
    ]
    assert [member.value for member in AuditAction] == [
        "memory_created",
        "updated",
        "archived",
        "deleted",
        "retrieved",
        "proxy_user_deleted",
        "job_discarded",
        "conflict_resolved_by_tenant",
    ]


def test_conversation_status_default_is_queued() -> None:
    assert str(Conversation.__table__.c.processing_status.server_default.arg) == "'queued'"


def test_quota_mode_enum_values_match_contract() -> None:
    assert [member.value for member in QuotaMode] == [
        "FULL",
        "PASSTHROUGH",
        "DEGRADED_RETRIEVE",
        "BLOCKED",
    ]
