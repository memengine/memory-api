"""prefer OpenAI embeddings for new and upgraded environments

Revision ID: prefer_openai_embedding_model
Revises: razorpay_billing_subscriptions

This changes only the active model configuration. Existing memories retain
their embedding_model_id and therefore remain linked to their original vector
collection and dimensions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "prefer_openai_embedding_model"
down_revision: str | None = "razorpay_billing_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OPENAI_MODEL_ID = "openai-text-embedding-3-small-v1"


def upgrade() -> None:
    bind = op.get_bind()

    existing = bind.execute(
        sa.text(
            """
            SELECT provider, model_name, dimensions, qdrant_collection
            FROM embedding_models
            WHERE id = :model_id
            """
        ),
        {"model_id": OPENAI_MODEL_ID},
    ).mappings().one_or_none()

    expected = {
        "provider": "openai",
        "model_name": "text-embedding-3-small",
        "dimensions": 1536,
        "qdrant_collection": "memories_openai",
    }
    if existing is not None and dict(existing) != expected:
        raise RuntimeError(
            f"Embedding model {OPENAI_MODEL_ID} exists with an unexpected configuration. "
            "Refusing to activate it automatically."
        )

    if existing is None:
        bind.execute(
            sa.text(
                """
                INSERT INTO embedding_models (
                    id, provider, model_name, dimensions, qdrant_collection, is_active
                ) VALUES (
                    :model_id, 'openai', 'text-embedding-3-small', 1536,
                    'memories_openai', FALSE
                )
                """
            ),
            {"model_id": OPENAI_MODEL_ID},
        )

    # The partial unique index permits exactly one active model. Do this in two
    # statements so an upgraded database cannot briefly contain two actives.
    bind.execute(
        sa.text(
            """
            UPDATE embedding_models
            SET is_active = FALSE,
                deprecated_at = COALESCE(deprecated_at, NOW())
            WHERE is_active IS TRUE
              AND id <> :model_id
            """
        ),
        {"model_id": OPENAI_MODEL_ID},
    )
    bind.execute(
        sa.text(
            """
            UPDATE embedding_models
            SET is_active = TRUE,
                deprecated_at = NULL
            WHERE id = :model_id
            """
        ),
        {"model_id": OPENAI_MODEL_ID},
    )


def downgrade() -> None:
    # Do not delete the OpenAI row: memories may already reference it.
    # A downgrade leaves the currently working model active rather than causing
    # a deployment rollback to make all newly-created memories unreadable.
    pass
