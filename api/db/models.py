from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean
from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship


UUID_SERVER_DEFAULT = text("gen_random_uuid()")
EMPTY_JSONB_OBJECT = text("'{}'::jsonb")
EMPTY_TEXT_ARRAY = text("'{}'::varchar[]")


class Base(DeclarativeBase):
    metadata = MetaData()


class MemoryCategory(str, enum.Enum):
    preference = "preference"
    fact = "fact"
    goal = "goal"
    procedure = "procedure"
    relationship = "relationship"
    expertise = "expertise"


class ConversationProcessingStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    done = "done"
    failed = "failed"


class AuditAction(str, enum.Enum):
    memory_created = "memory_created"
    updated = "updated"
    archived = "archived"
    deleted = "deleted"
    retrieved = "retrieved"
    proxy_user_deleted = "proxy_user_deleted"


class AgentMemoryScope(str, enum.Enum):
    private = "private"
    shared = "shared"


class PlanTier(str, enum.Enum):
    free = "free"
    starter = "starter"
    growth = "growth"
    enterprise = "enterprise"


class OveragePolicy(str, enum.Enum):
    block = "block"
    warn = "warn"
    charge = "charge"


class CallQualityBlockedLayer(str, enum.Enum):
    l1 = "L1"
    l2 = "L2"
    l3 = "L3"
    l4 = "L4"
    none = "NONE"


class QuotaMode(str, enum.Enum):
    full = "FULL"
    passthrough = "PASSTHROUGH"
    degraded_retrieve = "DEGRADED_RETRIEVE"
    blocked = "BLOCKED"


class VectorSyncOperation(str, enum.Enum):
    upsert = "upsert"
    delete = "delete"


class VectorSyncStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class ExtractionJobStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    dead = "dead"


class ApiVersionValue(str, enum.Enum):
    v1 = "v1"
    v2 = "v2"


class EmbeddingProvider(str, enum.Enum):
    gemini = "gemini"
    openai = "openai"
    local = "local"


class LLMProviderName(str, enum.Enum):
    gemini = "gemini"
    anthropic = "anthropic"
    cohere = "cohere"
    local = "local"


def enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    memory_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")
    memories: Mapped[list[Memory]] = relationship(back_populates="user", cascade="all, delete-orphan")
    conversations: Mapped[list[Conversation]] = relationship(back_populates="user", cascade="all, delete-orphan")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="user", cascade="all, delete-orphan")
    agents: Mapped[list[Agent]] = relationship(back_populates="user", cascade="all, delete-orphan")


class EmbeddingModel(Base):
    __tablename__ = "embedding_models"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    provider: Mapped[EmbeddingProvider] = mapped_column(
        Enum(EmbeddingProvider, name="embedding_provider_enum"),
        nullable=False,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    memories: Mapped[list[Memory]] = relationship(back_populates="embedding_model")


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    aws_region: Mapped[str] = mapped_column(String(50), nullable=False)
    postgres_url_secret: Mapped[str] = mapped_column(String(100), nullable=False)
    qdrant_url_secret: Mapped[str] = mapped_column(String(100), nullable=False)
    redis_url_secret: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    gdpr_compliant: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    tenants: Mapped[list[Tenant]] = relationship(back_populates="region")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=text("'IN1'"),
    )
    clerk_org_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    plan_tier: Mapped[PlanTier] = mapped_column(
        Enum(PlanTier, name="plan_tier_enum"),
        nullable=False,
        server_default=text("'starter'"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    alert_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="tenant")
    region: Mapped[Region] = relationship(back_populates="tenants")
    proxy_users: Mapped[list[ProxyUser]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    deprecation_usage: Mapped[list[TenantDeprecationUsage]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    llm_provider_config: Mapped[LLMProviderConfig | None] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    key_hash: Mapped[str] = mapped_column(String(60), nullable=False)
    key_prefix: Mapped[str | None] = mapped_column(String(8), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String()),
        nullable=False,
        server_default=EMPTY_TEXT_ARRAY,
    )
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("60"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    tenant: Mapped[Tenant | None] = relationship(back_populates="api_keys")
    user: Mapped[User | None] = relationship(back_populates="api_keys")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_scope: Mapped[AgentMemoryScope] = mapped_column(
        Enum(AgentMemoryScope, name="agent_memory_scope_enum"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="agents")
    memories: Mapped[list[Memory]] = relationship(back_populates="agent")
    conversations: Mapped[list[Conversation]] = relationship(back_populates="agent")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processing_status: Mapped[ConversationProcessingStatus] = mapped_column(
        Enum(ConversationProcessingStatus, name="conversation_processing_status_enum"),
        nullable=False,
        server_default=text("'queued'"),
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    agent: Mapped[Agent | None] = relationship(back_populates="conversations")
    memories: Mapped[list[Memory]] = relationship(back_populates="source_conversation")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[MemoryCategory] = mapped_column(
        Enum(MemoryCategory, name="memory_category_enum"),
        nullable=False,
    )
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    embedding_id: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("embedding_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    access_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    user: Mapped[User] = relationship(back_populates="memories")
    proxy_user: Mapped[ProxyUser] = relationship(back_populates="memories")
    embedding_model: Mapped[EmbeddingModel] = relationship(back_populates="memories")
    agent: Mapped[Agent | None] = relationship(back_populates="memories")
    source_conversation: Mapped[Conversation] = relationship(back_populates="memories")
    previous_version: Mapped[Memory | None] = relationship(
        remote_side="Memory.id",
        back_populates="next_versions",
        foreign_keys=[previous_version_id],
    )
    next_versions: Mapped[list[Memory]] = relationship(back_populates="previous_version")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="memory")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    proxy_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action_enum"),
        nullable=False,
    )
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
        nullable=True,
    )
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="audit_logs")
    proxy_user: Mapped[ProxyUser | None] = relationship(back_populates="audit_logs")
    memory: Mapped[Memory | None] = relationship(back_populates="audit_logs")


class ProxyUser(Base):
    __tablename__ = "proxy_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_user_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    memory_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="proxy_users")
    memories: Mapped[list[Memory]] = relationship(back_populates="proxy_user")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="proxy_user")
    extraction_jobs: Mapped[list[ExtractionJob]] = relationship(
        back_populates="proxy_user",
        cascade="all, delete-orphan",
    )


class TenantBudget(Base):
    __tablename__ = "tenant_budgets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    plan_tier: Mapped[PlanTier] = mapped_column(
        Enum(PlanTier, name="plan_tier_enum"),
        nullable=False,
        server_default=text("'starter'"),
    )
    monthly_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_month_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    current_month_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    write_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    write_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    read_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_per_user_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("10"),
    )
    overage_policy: Mapped[OveragePolicy] = mapped_column(
        Enum(OveragePolicy, name="overage_policy_enum"),
        nullable=False,
        server_default=text("'warn'"),
    )
    alert_threshold_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("0.8"),
    )
    alert_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_notified_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_notified_mode: Mapped[QuotaMode | None] = mapped_column(
        Enum(QuotaMode, name="quota_mode_enum", values_callable=enum_values),
        nullable=True,
    )
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ApiDeprecatedField(Base):
    __tablename__ = "api_deprecated_fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    api_version: Mapped[ApiVersionValue] = mapped_column(
        Enum(ApiVersionValue, name="api_version_enum"),
        nullable=False,
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    deprecated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sunset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replacement_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    migration_guide_url: Mapped[str] = mapped_column(String(500), nullable=False)


class TenantDeprecationUsage(Base):
    __tablename__ = "tenant_deprecation_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    api_version: Mapped[ApiVersionValue] = mapped_column(
        Enum(ApiVersionValue, name="api_version_enum"),
        nullable=False,
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    first_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    usage_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="deprecation_usage")


class LLMProviderConfig(Base):
    __tablename__ = "llm_provider_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    embed_provider_primary: Mapped[LLMProviderName] = mapped_column(
        Enum(LLMProviderName, name="llm_provider_name_enum"),
        nullable=False,
        server_default=text("'gemini'"),
    )
    embed_provider_fallback: Mapped[LLMProviderName] = mapped_column(
        Enum(LLMProviderName, name="llm_provider_name_enum"),
        nullable=False,
        server_default=text("'cohere'"),
    )
    extract_provider_primary: Mapped[LLMProviderName] = mapped_column(
        Enum(LLMProviderName, name="llm_provider_name_enum"),
        nullable=False,
        server_default=text("'gemini'"),
    )
    extract_provider_fallback: Mapped[LLMProviderName] = mapped_column(
        Enum(LLMProviderName, name="llm_provider_name_enum"),
        nullable=False,
        server_default=text("'anthropic'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    tenant: Mapped[Tenant | None] = relationship(back_populates="llm_provider_config")


class CallQualityLog(Base):
    __tablename__ = "call_quality_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    layer_blocked_at: Mapped[CallQualityBlockedLayer] = mapped_column(
        Enum(CallQualityBlockedLayer, name="call_quality_blocked_layer_enum"),
        nullable=False,
        server_default=text("'NONE'"),
    )
    quality_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("0"),
    )
    semantic_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class VectorSyncOutbox(Base):
    __tablename__ = "vector_sync_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    operation: Mapped[VectorSyncOperation] = mapped_column(
        Enum(VectorSyncOperation, name="vector_sync_operation_enum"),
        nullable=False,
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    status: Mapped[VectorSyncStatus] = mapped_column(
        Enum(VectorSyncStatus, name="vector_sync_status_enum"),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ExtractionJobStatus] = mapped_column(
        Enum(ExtractionJobStatus, name="extraction_job_status_enum"),
        nullable=False,
        server_default=text("'queued'"),
    )
    queue_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("3"),
    )
    memories_created: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    proxy_user: Mapped[ProxyUser] = relationship(back_populates="extraction_jobs")
    dead_letter_entry: Mapped[DeadLetterJob | None] = relationship(
        back_populates="extraction_job",
        cascade="all, delete-orphan",
        uselist=False,
    )


class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_retried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    extraction_job: Mapped[ExtractionJob] = relationship(back_populates="dead_letter_entry")


Index("ix_memories_user_category", Memory.user_id, Memory.category)
Index("ix_memories_user_importance_score_desc", Memory.user_id, Memory.importance_score.desc())
Index("ix_memories_user_last_accessed_at_desc", Memory.user_id, Memory.last_accessed_at.desc())
Index("ix_memories_user_is_archived", Memory.user_id, Memory.is_archived)
Index("ix_memories_proxy_user_category", Memory.proxy_user_id, Memory.category)
Index("ix_memories_proxy_user_importance_score_desc", Memory.proxy_user_id, Memory.importance_score.desc())
Index("ix_memories_proxy_user_last_accessed_at_desc", Memory.proxy_user_id, Memory.last_accessed_at.desc())
Index("ix_memories_proxy_user_is_archived", Memory.proxy_user_id, Memory.is_archived)
Index("ix_memories_embedding_model_id", Memory.embedding_model_id)
Index("ix_memories_metadata_gin", Memory.__table__.c.metadata, postgresql_using="gin")
Index("ix_api_keys_key_prefix", ApiKey.key_prefix)
Index("ix_proxy_users_tenant_hash", ProxyUser.tenant_id, ProxyUser.external_user_id_hash, unique=True)
Index("ix_proxy_users_tenant_active", ProxyUser.tenant_id, ProxyUser.last_active_at.desc())
Index("ix_tenants_region_id", Tenant.region_id)
Index(
    "ix_api_deprecated_fields_version_path",
    ApiDeprecatedField.api_version,
    ApiDeprecatedField.field_path,
    unique=True,
)
Index(
    "ix_tenant_deprecation_usage_tenant_version_path",
    TenantDeprecationUsage.tenant_id,
    TenantDeprecationUsage.api_version,
    TenantDeprecationUsage.field_path,
    unique=True,
)
Index(
    "ix_tenant_deprecation_usage_last_used",
    TenantDeprecationUsage.tenant_id,
    TenantDeprecationUsage.last_used_at.desc(),
)
Index("ix_llm_provider_config_tenant_id", LLMProviderConfig.tenant_id, unique=True)
Index("ix_vector_sync_outbox_status_created", VectorSyncOutbox.status, VectorSyncOutbox.created_at)
Index("ix_vector_sync_outbox_memory_id", VectorSyncOutbox.memory_id)
Index("ix_extraction_jobs_status_updated", ExtractionJob.status, ExtractionJob.updated_at)
Index("ix_extraction_jobs_tenant_status", ExtractionJob.tenant_id, ExtractionJob.status)
Index("ix_extraction_jobs_proxy_user", ExtractionJob.proxy_user_id)
Index("ix_extraction_jobs_status_stale_after", ExtractionJob.status, ExtractionJob.stale_after)
Index("ix_dead_letter_jobs_tenant_created", DeadLetterJob.tenant_id, DeadLetterJob.created_at.desc())
Index(
    "ix_embedding_models_single_active",
    EmbeddingModel.is_active,
    unique=True,
    postgresql_where=EmbeddingModel.is_active.is_(True),
)


__all__ = [
    "Agent",
    "AgentMemoryScope",
    "ApiKey",
    "ApiDeprecatedField",
    "ApiVersionValue",
    "AuditAction",
    "AuditLog",
    "Base",
    "CallQualityBlockedLayer",
    "CallQualityLog",
    "Conversation",
    "ConversationProcessingStatus",
    "DeadLetterJob",
    "ExtractionJob",
    "ExtractionJobStatus",
    "EmbeddingModel",
    "EmbeddingProvider",
    "LLMProviderConfig",
    "LLMProviderName",
    "Memory",
    "MemoryCategory",
    "OveragePolicy",
    "PlanTier",
    "ProxyUser",
    "QuotaMode",
    "Region",
    "Tenant",
    "TenantBudget",
    "TenantDeprecationUsage",
    "User",
    "VectorSyncOperation",
    "VectorSyncOutbox",
    "VectorSyncStatus",
]
