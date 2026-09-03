from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse

SECURE_DEPLOYMENT_ENVIRONMENTS = frozenset({"production", "staging"})
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg2"})
_TLS_VALUES = frozenset({"1", "true", "yes", "require", "verify-ca", "verify-full"})


def requires_secure_transport(app_env: str | None) -> bool:
    return str(app_env or "").strip().lower() in SECURE_DEPLOYMENT_ENVIRONMENTS


def validate_database_transport(database_url: str, *, app_env: str | None) -> str:
    """Reject non-TLS PostgreSQL endpoints in deployed environments.

    The error deliberately contains no endpoint details because connection URLs
    commonly contain credentials.
    """
    if not requires_secure_transport(app_env):
        return database_url

    parsed = urlparse(database_url)
    if parsed.scheme not in _POSTGRES_SCHEMES or not parsed.hostname:
        raise RuntimeError("Production and staging require a PostgreSQL DATABASE_URL.")
    if not _has_tls_option(parse_qs(parsed.query)):
        raise RuntimeError(
            "Production and staging DATABASE_URL values must require TLS "
            "(for example, ssl=require or sslmode=require)."
        )
    return database_url


def validate_qdrant_transport(qdrant_url: str, *, app_env: str | None) -> str:
    """Reject plaintext Qdrant endpoints in deployed environments."""
    if not requires_secure_transport(app_env):
        return qdrant_url

    parsed = urlparse(qdrant_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("Production and staging QDRANT_URL values must use HTTPS.")
    return qdrant_url


def _has_tls_option(query: dict[str, list[str]]) -> bool:
    values: Iterable[str] = (*query.get("sslmode", []), *query.get("ssl", []))
    return any(str(value).strip().lower() in _TLS_VALUES for value in values)
