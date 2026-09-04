from types import SimpleNamespace

import pytest

from api.db import database, vector_store
from api.infra.transport_security import (
    validate_database_transport,
    validate_qdrant_transport,
)


def test_development_allows_local_transport() -> None:
    assert validate_database_transport(
        "postgresql+asyncpg://postgres:password@localhost:5432/memoryos",
        app_env="development",
    ).startswith("postgresql+")
    assert validate_qdrant_transport("http://localhost:6333", app_env="development") == "http://localhost:6333"


@pytest.mark.parametrize("database_url", [
    "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos",
    "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?sslmode=prefer",
])
def test_production_rejects_database_without_required_tls(database_url: str) -> None:
    with pytest.raises(RuntimeError, match="must require TLS"):
        validate_database_transport(database_url, app_env="production")


def test_production_accepts_database_with_required_tls() -> None:
    database_url = "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?sslmode=require"
    assert validate_database_transport(database_url, app_env="production") == database_url


def test_async_database_url_translates_libpq_sslmode_without_dropping_tls() -> None:
    assert database.get_async_database_url(
        "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?sslmode=require"
    ) == "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?ssl=require"


def test_async_database_url_keeps_an_explicit_asyncpg_ssl_option() -> None:
    assert database.get_async_database_url(
        "postgresql://postgres:password@db.example.com:5432/memoryos?ssl=require&sslmode=require"
    ) == "postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?ssl=require"


def test_production_rejects_plaintext_qdrant() -> None:
    with pytest.raises(RuntimeError, match="must use HTTPS"):
        validate_qdrant_transport("http://qdrant.example.com:6333", app_env="production")


def test_production_configuration_validates_database_and_qdrant(monkeypatch) -> None:
    settings = SimpleNamespace(
        app_env="production",
        database_url="postgresql+asyncpg://postgres:password@db.example.com:5432/memoryos?sslmode=require",
        qdrant_url="https://qdrant.example.com",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr(vector_store, "get_settings", lambda: settings)

    assert database.get_database_url() == settings.database_url
    assert vector_store.get_qdrant_url() == settings.qdrant_url
