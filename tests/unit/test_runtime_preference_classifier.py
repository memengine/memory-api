"""Controlled schema/parser checks, not semantic accuracy evidence."""
import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.runtime_preference_worker import classify_intents
from api.settings import Settings
from tests.unit.test_preference_intent_contract import offered, reply


def record():
    return SimpleNamespace(snapshot=offered(), source_conversation_id=uuid.uuid4())


class Model:
    def __init__(self, text):
        self.text, self.calls = text, []

    def complete_sync(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.text)


def test_closed_format_and_verbatim_binding():
    model = Model(json.dumps(reply()))
    messages = [{"role": "user", "content": "Use Python instead of C++", "source_kind": "client_assertion"}]
    result = classify_intents(record(), messages, model)
    assert result.preferences[0].value == "Python"
    format = model.calls[0]["response_format"]
    assert format.name == "memory_preference_intent_v1" and format.strict
    assert format.schema["additionalProperties"] is False
    assert set(format.schema["required"]) == {"preferences", "nonpreferences"}
    prompt = json.loads(model.calls[0]["user_message"])
    assert prompt["turns"] == messages


@pytest.mark.parametrize("raw,role", [
    ("not json", "user"),
    (json.dumps({**reply(), "authority_priority": 100}), "user"),
    (json.dumps(reply()), "assistant"),
    (json.dumps(reply()), "tool"),
    (json.dumps(reply(preferences=[{**reply()["preferences"][0], "quote": "a forged quote"}])), "user"),
])
def test_model_output_cannot_forge_evidence_or_authority(raw, role):
    with pytest.raises(PreferenceAdmissionUnavailable, match="response_invalid"):
        classify_intents(record(), [{"role": role, "content": "Use Python instead of C++"}], Model(raw))


def test_prompt_and_reply_limits_reject_instead_of_truncate():
    model = Model("x" * 65537)
    with pytest.raises(PreferenceAdmissionUnavailable, match="response_oversized"):
        classify_intents(record(), [{"role": "user", "content": "hello"}], model)
    model = Model(json.dumps(reply(preferences=[])))
    with pytest.raises(PreferenceAdmissionUnavailable, match="prompt_oversized"):
        classify_intents(record(), [{"role": "user", "content": "x" * 131073}], model)
    assert model.calls == []


def test_rollout_defaults_and_normalized_bounded_allowlist():
    settings = Settings(_env_file=None, RUNTIME_PREFERENCE_ENABLED=False, RUNTIME_PREFERENCE_TENANT_IDS="")
    assert not settings.runtime_preference_enabled and settings.runtime_preference_tenant_ids == ""
    tenant = uuid.uuid4()
    settings = Settings(_env_file=None, RUNTIME_PREFERENCE_TENANT_IDS=f" {str(tenant).upper()} ")
    assert settings.runtime_preference_tenant_ids == str(tenant)
    for value in ("bad", f"{tenant},{tenant}", ",".join(str(uuid.uuid4()) for _ in range(65))):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, RUNTIME_PREFERENCE_TENANT_IDS=value)
