"""Opt-in dual-write helpers for tenant-envelope encrypted data."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from api.infra.envelope_encryption import EnvelopeEncryptionError, TenantEnvelopeCipher
from api.settings import get_settings


@lru_cache(maxsize=1)
def _active_cipher() -> TenantEnvelopeCipher | None:
    settings = get_settings()
    mode = settings.data_encryption_write_mode.strip().lower()
    if mode == "disabled":
        return None
    if mode != "dual-write":
        raise EnvelopeEncryptionError("DATA_ENCRYPTION_WRITE_MODE must be disabled or dual-write")
    return TenantEnvelopeCipher.from_settings(settings)


def encrypt_text_for_dual_write(
    *,
    tenant_id: str,
    record_type: str,
    record_id: str,
    value: str,
) -> dict[str, str | int] | None:
    cipher = _active_cipher()
    if cipher is None:
        return None
    return cipher.encrypt(
        tenant_id=tenant_id,
        record_type=record_type,
        record_id=record_id,
        plaintext=value,
    ).as_dict()


def encrypt_json_for_dual_write(
    *,
    tenant_id: str,
    record_type: str,
    record_id: str,
    value: dict[str, Any],
) -> dict[str, str | int] | None:
    canonical_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encrypt_text_for_dual_write(
        tenant_id=tenant_id,
        record_type=record_type,
        record_id=record_id,
        value=canonical_json,
    )


def encrypt_universal_text_for_dual_write(
    *,
    user_uui_id: str,
    record_type: str,
    record_id: str,
    value: str,
) -> dict[str, str | int] | None:
    """Dual-write a Passport value under its owner-specific KMS context.

    Passport memories may be readable by approved agents from more than one
    tenant.  Binding the envelope to the Passport owner—not an originating
    tenant—keeps the ciphertext readable only through the user-scoped access
    path and avoids incorrectly treating one tenant as the universal owner.
    The existing KMS context key is retained for backwards compatibility; its
    value is explicitly namespaced so it cannot collide with a tenant id.
    """
    if not user_uui_id:
        raise EnvelopeEncryptionError("user_uui_id is required for universal envelopes")
    return encrypt_text_for_dual_write(
        tenant_id=f"universal-user:{user_uui_id}",
        record_type=record_type,
        record_id=record_id,
        value=value,
    )
