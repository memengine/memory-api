from __future__ import annotations

CLAIM_SCHEMA_VERSION = 1
CLAIM_PROCESSOR_VERSION = "claim-ledger-v1"
TENANT_BACKFILL_PROCESSOR_VERSION = "tenant-provenance-backfill-v1"
PASSPORT_BACKFILL_PROCESSOR_VERSION = "passport-provenance-backfill-v1"
LEGACY_PROCESSOR_VERSION = "legacy"
SUPPORTED_CLAIM_SCHEMA_VERSIONS = frozenset({CLAIM_SCHEMA_VERSION})


def processor_version_for_resolution(
    resolution_reason: str | None,
    *,
    passport: bool = False,
) -> str:
    normalized = (resolution_reason or "").strip().lower()
    if "backfill" in normalized or normalized.startswith("legacy_"):
        return (
            PASSPORT_BACKFILL_PROCESSOR_VERSION
            if passport
            else TENANT_BACKFILL_PROCESSOR_VERSION
        )
    return CLAIM_PROCESSOR_VERSION


def supports_claim_schema(version: int) -> bool:
    return version in SUPPORTED_CLAIM_SCHEMA_VERSIONS