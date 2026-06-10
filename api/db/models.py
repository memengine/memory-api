from __future__ import annotations

import enum
import uuid
from datetime import date
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean
from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
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
EMPTY_JSONB_ARRAY = text("'[]'::jsonb")
EMPTY_TEXT_ARRAY = text("'{}'::varchar[]")
EMPTY_UUID_ARRAY = text("'{}'::uuid[]")


class Base(DeclarativeBase):
    metadata = MetaData()


class MemoryCategory(str, enum.Enum):
    preference = "preference"
    fact = "fact"
    goal = "goal"
    procedure = "procedure"
    relationship = "relationship"
    expertise = "expertise"


class SharedContextEntityType(str, enum.Enum):
    tech_stack = "tech_stack"
    company_fact = "company_fact"
    product_feature = "product_feature"
    team_process = "team_process"
    shared_goal = "shared_goal"
    organisation_policy = "organisation_policy"
    team_language = "team_language"
    shared_resource = "shared_resource"
    exam_date = "exam_date"
    grade_level = "grade_level"
    personal_skill = "personal_skill"
    personal_preference = "personal_preference"
    individual_goal = "individual_goal"
    learning_style = "learning_style"
    personal_fact = "personal_fact"
    marks_target = "marks_target"
    study_schedule = "study_schedule"


class CrossUserConflictStatus(str, enum.Enum):
    pending = "pending"
    clarification_queued = "clarification_queued"
    resolved = "resolved"
    ignored = "ignored"


class ClarificationQueueStatus(str, enum.Enum):
    pending = "pending"
    triggered = "triggered"
    resolved = "resolved"
    expired = "expired"


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
    job_discarded = "job_discarded"
    conflict_resolved_by_tenant = "conflict_resolved_by_tenant"


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
    support_type_configured: Mapped[str | None] = mapped_column(String(30), nullable=True)
    support_type_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'single'"))
    support_types_allowed: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_ARRAY,
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
    global_agents: Mapped[list[GlobalAgent]] = relationship(
        back_populates="owner_tenant",
        cascade="all, delete-orphan",
    )
    uui_proxy_links: Mapped[list[UUIProxyLink]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    shared_context_signals: Mapped[list[SharedContextSignal]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    cross_user_conflicts: Mapped[list[CrossUserConflict]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    clarification_queue: Mapped[list[ClarificationQueue]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    edtech_memories: Mapped[list[EdTechMemory]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    support_memories: Mapped[list[SupportMemory]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
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
    versions: Mapped[list[MemoryVersion]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
    )
    shared_context_signals: Mapped[list[SharedContextSignal]] = relationship(
        back_populates="source_memory",
        foreign_keys="SharedContextSignal.source_memory_id",
    )
    cross_user_conflicts_as_a: Mapped[list[CrossUserConflict]] = relationship(
        back_populates="user_a_memory",
        foreign_keys="CrossUserConflict.user_a_memory_id",
    )
    cross_user_conflicts_as_b: Mapped[list[CrossUserConflict]] = relationship(
        back_populates="user_b_memory",
        foreign_keys="CrossUserConflict.user_b_memory_id",
    )


class MemoryVersion(Base):
    __tablename__ = "memory_versions"
    __table_args__ = (
        UniqueConstraint("memory_id", "version_number", name="uq_memory_versions_memory_version"),
        CheckConstraint(
            "change_type IN ('created', 'conflict_update', 'manual_edit', 'importance_decay', 'importance_boost', 'archived', 'conflict_resolved')",
            name="ck_memory_versions_change_type",
        ),
        CheckConstraint(
            "changed_by IN ('system', 'user', 'operator')",
            name="ck_memory_versions_changed_by",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'system'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    memory: Mapped[Memory] = relationship(back_populates="versions")


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
    uui_proxy_link: Mapped[UUIProxyLink | None] = relationship(
        back_populates="proxy_user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    shared_context_signals: Mapped[list[SharedContextSignal]] = relationship(
        back_populates="source_proxy_user",
        cascade="all, delete-orphan",
    )
    clarification_queue: Mapped[list[ClarificationQueue]] = relationship(
        back_populates="proxy_user",
        cascade="all, delete-orphan",
    )
    edtech_memory: Mapped[EdTechMemory | None] = relationship(
        back_populates="proxy_user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    support_memory: Mapped[SupportMemory | None] = relationship(
        back_populates="proxy_user",
        cascade="all, delete-orphan",
        uselist=False,
    )


class EdTechMemory(Base):
    __tablename__ = "edtech_memories"
    __table_args__ = (
        UniqueConstraint("proxy_user_id", "tenant_id", name="uq_edtech_memories_proxy_tenant"),
        CheckConstraint(
            "learner_type IS NULL OR learner_type IN ('school_student','competitive_exam','higher_education','professional_cert','skill_learner','medical_student')",
            name="ck_edtech_memories_learner_type",
        ),
        CheckConstraint(
            "learner_type_confidence IN ('high','low')",
            name="ck_edtech_memories_learner_type_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    learner_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    learner_type_confidence: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'high'"))
    primary_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_deadline_event: Mapped[str | None] = mapped_column(String(200), nullable=True)
    primary_deadline_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    grade_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    board_or_curriculum: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subjects: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    syllabus_stage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)

    strong_topics: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    weak_topics: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    concept_gaps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    misconceptions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)

    explanation_style: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    session_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    language_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    peak_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    exam_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exam_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    marks_target: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    mock_scores: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)

    progress_trend: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    competitive_exam_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    higher_education_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    professional_cert_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    skill_learner_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    medical_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)

    forgetting_stages: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    improvement_velocity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    streak: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_topic_studied: Mapped[str | None] = mapped_column(Text, nullable=True)

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_extraction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extraction_source_job_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=EMPTY_UUID_ARRAY,
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

    proxy_user: Mapped[ProxyUser] = relationship(back_populates="edtech_memory")
    tenant: Mapped[Tenant] = relationship(back_populates="edtech_memories")


class SupportMemory(Base):
    __tablename__ = "support_memories"
    __table_args__ = (
        UniqueConstraint("proxy_user_id", "tenant_id", name="uq_support_memories_proxy_tenant"),
        CheckConstraint(
            "support_type IS NULL OR support_type IN ('saas','ecommerce','banking_fintech','travel','telecom','edtech_support','general_info')",
            name="ck_support_memories_support_type",
        ),
        CheckConstraint(
            "support_type_source IN ('detected','tenant_configured')",
            name="ck_support_memories_support_type_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    support_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    support_type_source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'detected'"))
    customer_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    communication_preference: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    language_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)
    current_open_issue: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    issue_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    resolution_preference: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )
    sentiment_pattern: Mapped[str | None] = mapped_column(String(50), nullable=True)
    risk_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_ARRAY)
    support_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSONB_OBJECT)

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_extraction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extraction_source_job_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=EMPTY_UUID_ARRAY,
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

    proxy_user: Mapped[ProxyUser] = relationship(back_populates="support_memory")
    tenant: Mapped[Tenant] = relationship(back_populates="support_memories")


class SharedContextSignal(Base):
    __tablename__ = "shared_context_signals"
    __table_args__ = (
        Index(
            "ix_shared_context_signals_tenant_type_value",
            "tenant_id",
            "entity_type",
            "entity_value",
        ),
    )

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
    entity_type: Mapped[SharedContextEntityType] = mapped_column(
        Enum(SharedContextEntityType, name="shared_context_entity_type_enum"),
        nullable=False,
    )
    entity_value: Mapped[str] = mapped_column(Text, nullable=False)
    source_proxy_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.75"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_superseded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    superseded_by_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_context_signals.id", ondelete="SET NULL"),
        nullable=True,
    )

    tenant: Mapped[Tenant] = relationship(back_populates="shared_context_signals")
    source_proxy_user: Mapped[ProxyUser | None] = relationship(back_populates="shared_context_signals")
    source_memory: Mapped[Memory | None] = relationship(
        back_populates="shared_context_signals",
        foreign_keys=[source_memory_id],
    )
    superseded_by_signal: Mapped[SharedContextSignal | None] = relationship(
        remote_side="SharedContextSignal.id",
        foreign_keys=[superseded_by_signal_id],
    )


class CrossUserConflict(Base):
    __tablename__ = "cross_user_conflicts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'clarification_queued', 'resolved', 'ignored')",
            name="ck_cross_user_conflicts_status",
        ),
        Index("ix_cross_user_conflicts_tenant_status", "tenant_id", "status"),
    )

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
    user_a_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_b_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
        nullable=True,
    )
    entity_type: Mapped[SharedContextEntityType] = mapped_column(
        Enum(SharedContextEntityType, name="shared_context_entity_type_enum"),
        nullable=False,
    )
    entity_value_a: Mapped[str] = mapped_column(Text, nullable=False)
    entity_value_b: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[CrossUserConflictStatus] = mapped_column(
        Enum(CrossUserConflictStatus, name="cross_user_conflict_status_enum"),
        nullable=False,
        server_default=text("'pending'"),
    )
    auto_resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    auto_resolution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_path: Mapped[str | None] = mapped_column(String(30), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_attention: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="cross_user_conflicts")
    user_a_memory: Mapped[Memory | None] = relationship(
        back_populates="cross_user_conflicts_as_a",
        foreign_keys=[user_a_memory_id],
    )
    user_b_memory: Mapped[Memory | None] = relationship(
        back_populates="cross_user_conflicts_as_b",
        foreign_keys=[user_b_memory_id],
    )
    clarification_queue: Mapped[list[ClarificationQueue]] = relationship(
        back_populates="conflict",
        cascade="all, delete-orphan",
    )


class ClarificationQueue(Base):
    __tablename__ = "clarification_queue"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'triggered', 'resolved', 'expired')",
            name="ck_clarification_queue_status",
        ),
        Index("ix_clarification_queue_proxy_status", "proxy_user_id", "status"),
        Index("ix_clarification_queue_tenant_status", "tenant_id", "status"),
    )

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
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_context: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cross_user_conflicts.id", ondelete="CASCADE"),
        nullable=True,
    )
    trigger_on: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'next_session'"),
    )
    status: Mapped[ClarificationQueueStatus] = mapped_column(
        Enum(ClarificationQueueStatus, name="clarification_queue_status_enum"),
        nullable=False,
        server_default=text("'pending'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() + interval '30 days'"),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="clarification_queue")
    proxy_user: Mapped[ProxyUser] = relationship(back_populates="clarification_queue")
    conflict: Mapped[CrossUserConflict | None] = relationship(back_populates="clarification_queue")


class UniversalUser(Base):
    __tablename__ = "universal_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    uui_token: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    otp_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    memory_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    permission_grants: Mapped[list[PermissionGrant]] = relationship(
        back_populates="universal_user",
        cascade="all, delete-orphan",
    )
    universal_memories: Mapped[list[UniversalMemory]] = relationship(
        back_populates="universal_user",
        cascade="all, delete-orphan",
    )
    memory_flags: Mapped[list[UserMemoryFlag]] = relationship(
        back_populates="universal_user",
        cascade="all, delete-orphan",
    )
    proxy_links: Mapped[list[UUIProxyLink]] = relationship(
        back_populates="universal_user",
        cascade="all, delete-orphan",
    )


class GlobalAgent(Base):
    __tablename__ = "global_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_categories_requested: Mapped[list[str]] = mapped_column(
        ARRAY(String()),
        nullable=False,
        server_default=EMPTY_TEXT_ARRAY,
    )
    redirect_uri: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        server_default=text("''"),
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    owner_tenant: Mapped[Tenant] = relationship(back_populates="global_agents")
    api_keys: Mapped[list[AgentApiKey]] = relationship(
        back_populates="global_agent",
        cascade="all, delete-orphan",
    )
    permission_grants: Mapped[list[PermissionGrant]] = relationship(
        back_populates="global_agent",
        cascade="all, delete-orphan",
    )
    universal_memories: Mapped[list[UniversalMemory]] = relationship(
        back_populates="source_agent",
        cascade="all, delete-orphan",
    )


class PermissionGrant(Base):
    __tablename__ = "permission_grants"
    __table_args__ = (
        CheckConstraint(
            "access_type IN ('read_only', 'read_write')",
            name="ck_permission_grants_access_type",
        ),
        UniqueConstraint("user_uui_id", "agent_id", name="uq_permission_grants_user_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_uui_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    categories_allowed: Mapped[list[str]] = mapped_column(
        ARRAY(String()),
        nullable=False,
        server_default=EMPTY_TEXT_ARRAY,
    )
    access_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'read_only'"),
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    universal_user: Mapped[UniversalUser] = relationship(back_populates="permission_grants")
    global_agent: Mapped[GlobalAgent] = relationship(back_populates="permission_grants")


class UniversalMemory(Base):
    __tablename__ = "universal_memories"
    __table_args__ = (
        CheckConstraint(
            "category IN ('preference','fact','goal','procedure','relationship','expertise')",
            name="ck_universal_memories_category",
        ),
        CheckConstraint("importance_score > 0 AND importance_score <= 10", name="ck_universal_memories_importance"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_universal_memories_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    user_uui_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    embedding_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    is_flagged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=EMPTY_JSONB_OBJECT,
    )

    universal_user: Mapped[UniversalUser] = relationship(back_populates="universal_memories")
    source_agent: Mapped[GlobalAgent] = relationship(back_populates="universal_memories")
    flags: Mapped[list[UserMemoryFlag]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list[UniversalMemoryVersion]] = relationship(
        back_populates="universal_memory",
        cascade="all, delete-orphan",
    )


class UniversalMemoryVersion(Base):
    __tablename__ = "universal_memory_versions"
    __table_args__ = (
        UniqueConstraint("universal_memory_id", "version_number", name="uq_universal_memory_versions_memory_version"),
        CheckConstraint(
            "change_type IN ('created','user_corrected','user_removed','conflict_resolved','agent_updated','importance_decay','importance_boost','archived')",
            name="ck_universal_memory_versions_change_type",
        ),
        CheckConstraint(
            "changed_by IN ('user','system','agent')",
            name="ck_universal_memory_versions_changed_by",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    universal_memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    universal_memory: Mapped[UniversalMemory] = relationship(back_populates="versions")
    changed_by_agent: Mapped[GlobalAgent | None] = relationship()


class UserMemoryFlag(Base):
    __tablename__ = "user_memory_flags"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('incorrect','outdated','never_said_this')",
            name="ck_user_memory_flags_reason",
        ),
        CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_user_memory_flags_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_uui_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'pending'"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memory: Mapped[UniversalMemory] = relationship(back_populates="flags")
    universal_user: Mapped[UniversalUser] = relationship(back_populates="memory_flags")


class UUIProxyLink(Base):
    __tablename__ = "uui_proxy_link"
    __table_args__ = (
        UniqueConstraint("tenant_id", "proxy_user_id", name="uq_uui_proxy_link_tenant_proxy_user"),
        UniqueConstraint("proxy_user_id", name="uq_uui_proxy_link_proxy_user"),
    )

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
    proxy_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxy_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_uui_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("universal_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="uui_proxy_links")
    proxy_user: Mapped[ProxyUser] = relationship(back_populates="uui_proxy_link")
    universal_user: Mapped[UniversalUser] = relationship(back_populates="proxy_links")


class AgentApiKey(Base):
    __tablename__ = "agent_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )
    global_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    key_hash: Mapped[str] = mapped_column(String(60), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    global_agent: Mapped[GlobalAgent] = relationship(back_populates="api_keys")


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
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    error_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
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
    error_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
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
Index("ix_memory_versions_memory_id", MemoryVersion.memory_id)
Index("ix_memories_proxy_user_category", Memory.proxy_user_id, Memory.category)
Index("ix_memories_proxy_user_importance_score_desc", Memory.proxy_user_id, Memory.importance_score.desc())
Index("ix_memories_proxy_user_last_accessed_at_desc", Memory.proxy_user_id, Memory.last_accessed_at.desc())
Index("ix_memories_proxy_user_is_archived", Memory.proxy_user_id, Memory.is_archived)
Index("ix_memories_embedding_model_id", Memory.embedding_model_id)
Index("ix_memories_metadata_gin", Memory.__table__.c.metadata, postgresql_using="gin")
Index("ix_api_keys_key_prefix", ApiKey.key_prefix)
Index("ix_proxy_users_tenant_hash", ProxyUser.tenant_id, ProxyUser.external_user_id_hash, unique=True)
Index("ix_proxy_users_tenant_active", ProxyUser.tenant_id, ProxyUser.last_active_at.desc())
Index("ix_universal_users_uui_token", UniversalUser.uui_token, unique=True)
Index("ix_universal_users_email", UniversalUser.email, unique=True)
Index("ix_permission_grants_user_active", PermissionGrant.user_uui_id, PermissionGrant.is_active)
Index("ix_permission_grants_agent_active", PermissionGrant.agent_id, PermissionGrant.is_active)
Index(
    "ix_universal_memories_user_category_archived",
    UniversalMemory.user_uui_id,
    UniversalMemory.category,
    UniversalMemory.is_archived,
)
Index(
    "ix_universal_memories_user_importance_desc",
    UniversalMemory.user_uui_id,
    UniversalMemory.importance_score.desc(),
)
Index("ix_universal_memories_source_agent_id", UniversalMemory.source_agent_id)
Index("ix_user_memory_flags_user_status", UserMemoryFlag.user_uui_id, UserMemoryFlag.status)
Index(
    "ix_universal_memory_versions_memory_created",
    UniversalMemoryVersion.universal_memory_id,
    UniversalMemoryVersion.created_at.desc(),
)
Index("ix_agent_api_keys_key_prefix", AgentApiKey.key_prefix)
Index("ix_agent_api_keys_global_agent_active", AgentApiKey.global_agent_id, AgentApiKey.is_active)
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
Index("ix_edtech_memories_tenant_exam_date", EdTechMemory.tenant_id, EdTechMemory.exam_date)
Index("ix_edtech_memories_tenant_primary_deadline_date", EdTechMemory.tenant_id, EdTechMemory.primary_deadline_date)
Index("ix_edtech_memories_tenant_last_extraction", EdTechMemory.tenant_id, EdTechMemory.last_extraction_at)
Index("ix_support_memories_tenant_type", SupportMemory.tenant_id, SupportMemory.support_type)
Index("ix_support_memories_tenant_sentiment", SupportMemory.tenant_id, SupportMemory.sentiment_pattern)
Index("ix_support_memories_tenant_updated", SupportMemory.tenant_id, SupportMemory.updated_at.desc())
Index(
    "ix_embedding_models_single_active",
    EmbeddingModel.is_active,
    unique=True,
    postgresql_where=EmbeddingModel.is_active.is_(True),
)


__all__ = [
    "Agent",
    "AgentApiKey",
    "AgentMemoryScope",
    "ApiKey",
    "ApiDeprecatedField",
    "ApiVersionValue",
    "AuditAction",
    "AuditLog",
    "Base",
    "CallQualityBlockedLayer",
    "CallQualityLog",
    "ClarificationQueue",
    "ClarificationQueueStatus",
    "Conversation",
    "ConversationProcessingStatus",
    "CrossUserConflict",
    "CrossUserConflictStatus",
    "DeadLetterJob",
    "EdTechMemory",
    "ExtractionJob",
    "ExtractionJobStatus",
    "EmbeddingModel",
    "EmbeddingProvider",
    "GlobalAgent",
    "LLMProviderConfig",
    "LLMProviderName",
    "Memory",
    "MemoryCategory",
    "MemoryVersion",
    "OveragePolicy",
    "PlanTier",
    "PermissionGrant",
    "ProxyUser",
    "QuotaMode",
    "Region",
    "SharedContextEntityType",
    "SharedContextSignal",
    "SupportMemory",
    "Tenant",
    "TenantBudget",
    "TenantDeprecationUsage",
    "UUIProxyLink",
    "UserMemoryFlag",
    "User",
    "UniversalMemory",
    "UniversalMemoryVersion",
    "UniversalUser",
    "VectorSyncOperation",
    "VectorSyncOutbox",
    "VectorSyncStatus",
]
