from __future__ import annotations

import pytest

from api.infra.envelope_encryption import (
    EncryptedValue,
    EnvelopeEncryptionError,
    TenantEnvelopeCipher,
)
from api.settings import Settings


class FakeDataKeyProvider:
    provider_name = "test-kms"

    def generate_data_key(self, *, tenant_id: str) -> tuple[bytes, bytes, str]:
        return b"k" * 32, f"wrapped:{tenant_id}".encode(), "test-key"

    def decrypt_data_key(self, *, tenant_id: str, encrypted_data_key: bytes) -> bytes:
        if encrypted_data_key != f"wrapped:{tenant_id}".encode():
            raise EnvelopeEncryptionError("wrong tenant key")
        return b"k" * 32


def test_round_trip_is_bound_to_tenant_and_record_identity() -> None:
    cipher = TenantEnvelopeCipher(key_provider=FakeDataKeyProvider())
    encrypted = cipher.encrypt(
        tenant_id="tenant-a",
        record_type="memory",
        record_id="memory-1",
        plaintext="private customer preference",
    )

    assert "private customer preference" not in str(encrypted.as_dict())
    assert cipher.decrypt(
        tenant_id="tenant-a",
        record_type="memory",
        record_id="memory-1",
        encrypted_value=EncryptedValue.from_dict(encrypted.as_dict()),
    ) == "private customer preference"

    with pytest.raises(EnvelopeEncryptionError):
        cipher.decrypt(
            tenant_id="tenant-b",
            record_type="memory",
            record_id="memory-1",
            encrypted_value=encrypted,
        )


def test_settings_fail_closed_until_aws_kms_is_configured() -> None:
    with pytest.raises(EnvelopeEncryptionError, match="disabled"):
        TenantEnvelopeCipher.from_settings(Settings())
