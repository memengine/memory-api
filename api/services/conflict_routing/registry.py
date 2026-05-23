from __future__ import annotations

from api.services.conflict_routing.base import BaseEntityRouter
from api.services.conflict_routing.edtech_router import EdTechEntityRouter
from api.services.conflict_routing.generic_router import GenericEntityRouter
from api.services.conflict_routing.healthcare_router import HealthcareEntityRouter
from api.services.conflict_routing.hrtech_router import HRTechEntityRouter


DOMAIN_ROUTERS: dict[str, BaseEntityRouter] = {
    "edtech": EdTechEntityRouter(),
    "healthcare": HealthcareEntityRouter(),
    "hrtech": HRTechEntityRouter(),
    "generic": GenericEntityRouter(),
}


def get_router(domain_schema: str | None) -> BaseEntityRouter:
    normalized = (domain_schema or "").strip().lower()
    if normalized in DOMAIN_ROUTERS:
        return DOMAIN_ROUTERS[normalized]
    return DOMAIN_ROUTERS["generic"]
