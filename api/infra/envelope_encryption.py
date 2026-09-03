"""Tenant-scoped envelope encryption primitives for sensitive stored values.

This module intentionally does not enable encryption by itself. Storage models
must migrate field-by-field so retrieval, provenance, and deletion stay
correct. In production the wrapping key belongs in a cloud KMS, never in the
database that stores the ciphertext.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.settings import Settings


class EnvelopeEncryptionError(RuntimeError):
    """Raised when envelope encryption cannot safely encrypt or decrypt data."""


class DataKeyProvider(Protocol):
    provider_name: str

    def generate_data_key(self, *, tenant_id: str) -> tuple[bytes, bytes, str]: ...

    def decrypt_data_key(self, *, tenant_id: str, encrypted_data_key: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """Portable, JSON-safe envelope returned by :class:`TenantEnvelopeCipher`."""

    version: int
    provider: str
    key_id: str
    encrypted_data_key: str
    nonce: str
    ciphertext: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "version": self.version,
            "provider": self.provider,
            "key_id": self.key_id,
            "encrypted_data_key": self.encrypted_data_key,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> EncryptedValue:
        try:
            return cls(
                version=int(value["version"]),
                provider=str(value["provider"]),
                key_id=str(value["key_id"]),
                encrypted_data_key=str(value["encrypted_data_key"]),
                nonce=str(value["nonce"]),
                ciphertext=str(value["ciphertext"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EnvelopeEncryptionError("invalid encrypted-value envelope") from exc


class AwsKmsDataKeyProvider:
    provider_name = "aws-kms"

    def __init__(self, *, key_id: str, client=None) -> None:
        if not key_id.strip():
            raise EnvelopeEncryptionError("DATA_ENCRYPTION_KMS_KEY_ID is required for aws-kms")
        self.key_id = key_id
        self.client = client or self._build_client()

    @staticmethod
    def _build_client():
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise EnvelopeEncryptionError("boto3 is required for aws-kms encryption") from exc
        return boto3.client(
            "kms",
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        )

    def generate_data_key(self, *, tenant_id: str) -> tuple[bytes, bytes, str]:
        response = self.client.generate_data_key(
            KeyId=self.key_id,
            KeySpec="AES_256",
            EncryptionContext={"memoryos_tenant_id": tenant_id},
        )
        return bytes(response["Plaintext"]), bytes(response["CiphertextBlob"]), str(response["KeyId"])

    def decrypt_data_key(self, *, tenant_id: str, encrypted_data_key: bytes) -> bytes:
        response = self.client.decrypt(
            CiphertextBlob=encrypted_data_key,
            EncryptionContext={"memoryos_tenant_id": tenant_id},
        )
        return bytes(response["Plaintext"])


class TenantEnvelopeCipher:
    """AES-256-GCM encryption bound to one tenant, record type, and record id."""

    VERSION = 1

    def __init__(self, *, key_provider: DataKeyProvider) -> None:
        self.key_provider = key_provider

    @classmethod
    def from_settings(cls, settings: Settings) -> TenantEnvelopeCipher:
        provider = settings.data_encryption_provider.strip().lower()
        if provider != "aws-kms":
            raise EnvelopeEncryptionError(
                "tenant envelope encryption is disabled; configure DATA_ENCRYPTION_PROVIDER=aws-kms"
            )
        return cls(
            key_provider=AwsKmsDataKeyProvider(key_id=settings.data_encryption_kms_key_id)
        )

    @staticmethod
    def _aad(*, tenant_id: str, record_type: str, record_id: str) -> bytes:
        if not tenant_id or not record_type or not record_id:
            raise EnvelopeEncryptionError("tenant_id, record_type, and record_id are required")
        return f"memoryos:envelope:v1:{tenant_id}:{record_type}:{record_id}".encode()

    def encrypt(
        self,
        *,
        tenant_id: str,
        record_type: str,
        record_id: str,
        plaintext: str,
    ) -> EncryptedValue:
        data_key, encrypted_data_key, key_id = self.key_provider.generate_data_key(tenant_id=tenant_id)
        if len(data_key) != 32:
            raise EnvelopeEncryptionError("data-key provider returned a non-AES-256 key")
        nonce = os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self._aad(tenant_id=tenant_id, record_type=record_type, record_id=record_id),
        )
        return EncryptedValue(
            version=self.VERSION,
            provider=self.key_provider.provider_name,
            key_id=key_id,
            encrypted_data_key=base64.b64encode(encrypted_data_key).decode("ascii"),
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    def decrypt(
        self,
        *,
        tenant_id: str,
        record_type: str,
        record_id: str,
        encrypted_value: EncryptedValue,
    ) -> str:
        if encrypted_value.version != self.VERSION:
            raise EnvelopeEncryptionError("unsupported encrypted-value version")
        if encrypted_value.provider != self.key_provider.provider_name:
            raise EnvelopeEncryptionError("encrypted value belongs to a different key provider")
        try:
            encrypted_data_key = base64.b64decode(encrypted_value.encrypted_data_key, validate=True)
            nonce = base64.b64decode(encrypted_value.nonce, validate=True)
            ciphertext = base64.b64decode(encrypted_value.ciphertext, validate=True)
        except ValueError as exc:
            raise EnvelopeEncryptionError("invalid encrypted-value encoding") from exc
        data_key = self.key_provider.decrypt_data_key(
            tenant_id=tenant_id,
            encrypted_data_key=encrypted_data_key,
        )
        if len(data_key) != 32:
            raise EnvelopeEncryptionError("data-key provider returned a non-AES-256 key")
        try:
            return AESGCM(data_key).decrypt(
                nonce,
                ciphertext,
                self._aad(tenant_id=tenant_id, record_type=record_type, record_id=record_id),
            ).decode("utf-8")
        except Exception as exc:
            raise EnvelopeEncryptionError("encrypted value could not be authenticated") from exc
