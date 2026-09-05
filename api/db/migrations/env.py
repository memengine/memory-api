"""Alembic environment scaffold."""

from pathlib import Path
import sys
from logging.config import fileConfig

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.db.database import get_sync_database_url
from api.db.models import Base
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy import pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_sync_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Do not read Alembic's local-development fallback URL here. ECS injects
    # DATABASE_URL through Secrets Manager, and get_sync_database_url keeps
    # managed Postgres URLs compatible with psycopg2 for migrations.
    connectable = create_engine(get_sync_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
