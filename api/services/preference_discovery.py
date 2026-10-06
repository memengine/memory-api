"""Semantic nominations discover keys; owned SQL/catalog bindings govern scopes."""
from dataclasses import dataclass

from sqlalchemy import literal_column, select

from api.db.models import Memory, MemoryClaim, ProxyUser, Tenant
from api.schemas.preferences import (
    PreferenceApplicability,
    PreferenceReadContext,
    PreferenceScopeRequest,
)
from api.services.preference_claim_service import (
    PREFERENCE_MARKER,
    legacy_memory_predicate,
    preference_declaration,
    preference_fingerprint,
)
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.runtime_preference_routing import runtime_catalog
from api.settings import get_settings


def discovery_enabled(tenant_id):
    settings = get_settings()
    return (settings.runtime_preference_discovery_enabled and settings.explicit_preference_claims_enabled
        and str(tenant_id) in settings.runtime_preference_tenant_ids.split(","))


def preference_query(*, tenant_id, owner, fingerprints=None, predicates=None):
    # A complete bounded identity set, not whichever scopes vector search found.
    query = select(MemoryClaim, Memory).outerjoin(
        Memory, MemoryClaim.active_memory_id == Memory.id,
    ).join(ProxyUser, MemoryClaim.proxy_user_id == ProxyUser.id).where(
        MemoryClaim.tenant_id == tenant_id, MemoryClaim.proxy_user_id == owner,
        ProxyUser.tenant_id == tenant_id,
        ProxyUser.is_blocked.is_(False),
    )
    if predicates is None:
        query = query.where(MemoryClaim.claim_fingerprint.in_(fingerprints))
    else:
        # Server-owned constants keep the partial index usable by prepared generic plans.
        query = query.where(MemoryClaim.category == literal_column("'preference'"), MemoryClaim.predicate_key.in_(predicates),
            MemoryClaim.status.in_((literal_column("'active'"), literal_column("'disputed'")))).limit(33)
    return query


async def preference_rows(session, **kwargs):
    query = preference_query(**kwargs)
    return (await session.execute(query.execution_options(populate_existing=True))).all()


@dataclass(frozen=True)
class PreferenceDiscovery:
    context: PreferenceReadContext | None
    scope_request: PreferenceScopeRequest | None
    rows: tuple
    nominated_keys: tuple[str, ...]


async def discover_preferences(session, *, tenant_id, owner, nominee_ids, cold_start, is_current,
                               created_after=None, withheld_fingerprints=()):
    tenant = await session.get(Tenant, tenant_id, populate_existing=True)
    catalog = runtime_catalog(tenant)  # Invalid configuration fails closed, never guesses global.
    scopes = {preference_fingerprint(p.key, scope.applicability): (p.key, scope.applicability)
        for p in catalog.properties for scope in catalog.scopes}
    query = select(Memory).join(ProxyUser, Memory.proxy_user_id == ProxyUser.id).where(
        Memory.proxy_user_id == owner, ProxyUser.tenant_id == tenant_id, ProxyUser.is_blocked.is_(False),
        ~legacy_memory_predicate())
    if cold_start:
        query = query.where(Memory.category == "preference", Memory.is_archived.is_(False))
        query = query.order_by(Memory.importance_score.desc(), Memory.id).limit(50)
    else:
        # The retriever normalizes, deduplicates and caps IDs before this boundary.
        query = query.where(Memory.id.in_(nominee_ids[:50])).limit(50)
    nominees = (await session.scalars(query.execution_options(populate_existing=True))).all()
    keys = set()
    for memory in nominees:
        declaration = preference_declaration((memory.metadata_json or {}).get("provenance"))
        if memory.category.value != "preference" or declaration is None:
            continue
        identity = scopes.get(preference_fingerprint(declaration.property_key, declaration.applicability))
        if identity is not None:
            keys.add(identity[0])
    if not keys:
        return PreferenceDiscovery(None, None, (), ())
    # Catalog removal cannot prove that a previously stored scope disappeared.
    # Read every active owned scope for the nominated properties, with +1 coverage.
    rows = tuple(await preference_rows(session, tenant_id=tenant_id, owner=owner,
        predicates=[f"{PREFERENCE_MARKER}.{key}" for key in keys]))
    if len(rows) > 32:
        raise PreferenceAdmissionUnavailable("preference_discovery_scope_coverage_exceeded")
    required, options = set(), {}
    for claim, memory in rows:
        identity = scopes.get(claim.claim_fingerprint)
        if identity is None:
            raise PreferenceAdmissionUnavailable("preference_discovery_scope_catalog_incomplete")
        key, scope = identity
        if key not in keys or claim.predicate_key != f"{PREFERENCE_MARKER}.{key}" or claim.scope != scope.model_dump(exclude_none=True):
            raise PreferenceAdmissionUnavailable("preference_discovery_scope_binding_changed")
        if scope.kind != "context":
            continue
        # A corrupt binding is not proof of absence; do not activate a global fallback.
        if (memory is not None and memory.proxy_user_id == owner and
            (memory.is_archived or not is_current(memory)
             or (created_after is not None and memory.created_at < created_after))):
            continue
        required.add(key)
        options[scope.model_dump_json()] = scope
    for fp in withheld_fingerprints:
        identity = scopes.get(fp)
        if identity is not None and identity[0] in keys and identity[1].kind == "context":
            required.add(identity[0])
            options[identity[1].model_dump_json()] = identity[1]
    global_scope = PreferenceApplicability(kind="global")
    global_keys = {key for key in keys - required if preference_fingerprint(key, global_scope) in scopes}
    context = PreferenceReadContext(applicability=global_scope, property_keys=sorted(global_keys)) if global_keys else None
    if required:
        # Include an offered global scope as an explicit choice, never a default guess.
        if any(preference_fingerprint(key, global_scope) in scopes for key in required):
            options[global_scope.model_dump_json()] = global_scope
    request = PreferenceScopeRequest(property_keys=sorted(required), available_scopes=[options[key] for key in sorted(options)]) if required else None
    return PreferenceDiscovery(context, request, rows, tuple(sorted(keys)))
