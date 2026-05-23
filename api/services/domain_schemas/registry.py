from __future__ import annotations

from api.services.domain_schemas.base import BaseDomainSchema
from api.services.domain_schemas.edtech import EdTechDomainSchema


DOMAIN_SCHEMAS: dict[str, BaseDomainSchema] = {
    "edtech": EdTechDomainSchema(),
}


def register_domain_schema(schema: BaseDomainSchema) -> None:
    DOMAIN_SCHEMAS[schema.get_domain().strip().lower()] = schema


def get_domain_schema(domain_schema: str | None) -> BaseDomainSchema | None:
    normalized = (domain_schema or "").strip().lower()
    if not normalized or normalized == "generic":
        return None
    return DOMAIN_SCHEMAS.get(normalized)


__all__ = [
    "DOMAIN_SCHEMAS",
    "get_domain_schema",
    "register_domain_schema",
]
