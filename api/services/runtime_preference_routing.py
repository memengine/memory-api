"""Server-owned routing shared by acceptance and the worker, never public metadata."""
from __future__ import annotations

import json

from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.preference_snapshot_service import PreferenceSnapshotCatalog
from api.settings import get_settings

RUNTIME_ROUTE = "preference_v1"
CATALOG_KEY = "runtime_preference_catalog"
MAX_CATALOG_BYTES = 32 * 1024


def runtime_enabled(tenant_id):
    settings = get_settings()
    return (settings.runtime_preference_enabled and settings.explicit_preference_claims_enabled
            and str(tenant_id) in settings.runtime_preference_tenant_ids.split(","))


def runtime_catalog(tenant):
    if tenant is None or not tenant.is_active:
        raise PreferenceAdmissionUnavailable("intent_tenant_unavailable")
    raw = (tenant.metadata_json or {}).get(CATALOG_KEY)
    if raw is None:
        raise PreferenceAdmissionUnavailable("intent_catalog_missing")
    if len(json.dumps(raw).encode("utf-8")) > MAX_CATALOG_BYTES:
        raise PreferenceAdmissionUnavailable("intent_catalog_oversized")
    try:
        if not isinstance(raw, dict) or set(raw) != {"properties", "scopes"}:
            raise ValueError("closed operator catalog required")
        catalog = PreferenceSnapshotCatalog.model_validate({"tenant_id": tenant.id, **raw})
    except (TypeError, ValueError) as exc:
        raise PreferenceAdmissionUnavailable("intent_catalog_invalid") from exc
    if any((tenant.metadata_json or {}).get(key) for key in ("domain_schema", "memory_domain")):
        raise PreferenceAdmissionUnavailable("intent_domain_overlay_ineligible")
    return catalog
