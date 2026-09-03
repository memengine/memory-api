from __future__ import annotations

import json

from api.infra import protected_storage


class FakeCipher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def encrypt(self, *, tenant_id: str, record_type: str, record_id: str, plaintext: str):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "record_type": record_type,
                "record_id": record_id,
                "plaintext": plaintext,
            }
        )

        class Result:
            def as_dict(self):
                return {"version": 1, "ciphertext": "opaque"}

        return Result()


def test_text_dual_write_is_noop_when_encryption_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: None)

    assert protected_storage.encrypt_text_for_dual_write(
        tenant_id="tenant-a",
        record_type="memory-content",
        record_id="memory-1",
        value="private text",
    ) is None


def test_json_dual_write_encrypts_canonical_payload(monkeypatch) -> None:
    cipher = FakeCipher()
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: cipher)

    envelope = protected_storage.encrypt_json_for_dual_write(
        tenant_id="tenant-a",
        record_type="extraction-job-payload",
        record_id="job-1",
        value={"messages": [{"content": "private text"}], "job_id": "job-1"},
    )

    assert envelope == {"version": 1, "ciphertext": "opaque"}
    assert cipher.calls == [
        {
            "tenant_id": "tenant-a",
            "record_type": "extraction-job-payload",
            "record_id": "job-1",
            "plaintext": json.dumps(
                {"messages": [{"content": "private text"}], "job_id": "job-1"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    ]


def test_universal_text_dual_write_uses_a_namespaced_owner_context(monkeypatch) -> None:
    cipher = FakeCipher()
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: cipher)

    envelope = protected_storage.encrypt_universal_text_for_dual_write(
        user_uui_id="passport-user-1",
        record_type="universal_memory",
        record_id="memory-1",
        value="private preference",
    )

    assert envelope == {"version": 1, "ciphertext": "opaque"}
    assert cipher.calls == [
        {
            "tenant_id": "universal-user:passport-user-1",
            "record_type": "universal_memory",
            "record_id": "memory-1",
            "plaintext": "private preference",
        }
    ]
