import uuid
from types import SimpleNamespace as Row

import pytest
from pydantic import ValidationError

from api.errors import APIError
from api.middleware.rate_limiter import RateLimiterMiddleware
from api.routers.service_corrections import actor_key
from api.schemas.service_corrections import ServiceCorrectionDecision
from api.services.service_correction_service import digest, request_digest


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/memories/service-corrections"),
    ("GET", "/v1/memories/service-corrections/" + str(uuid.uuid4())),
    ("POST", "/v1/memories/service-corrections/" + str(uuid.uuid4()) + "/decision"),
])
def test_operator_routes_require_admin(method, path):
    assert RateLimiterMiddleware._required_permission(method, path) == "admin"


@pytest.mark.parametrize("state", [Row(auth_scheme="bearer", org_role="org:admin"),
    Row(auth_scheme="apikey", api_key_id="bad"), Row(auth_scheme=None), Row(auth_scheme="apikey")])
def test_missing_or_bearer_principal_cannot_use_operator_api(state):
    with pytest.raises(APIError) as error:
        actor_key(Row(state=state))
    assert error.value.status_code == 403


def decision():
    return {"external_user_id": "synthetic", "target_memory_id": str(uuid.uuid4()),
        "target_version": "a" * 64, "review_version": "b" * 64, "action": "apply"}


@pytest.mark.parametrize("field,value", [("action", "rewrite"), ("review_version", "bad"),
    ("target_version", ""), ("target_memory_id", "plan"), ("external_user_id", ""),
    ("content", "Caller may not replace the retained value"), ("authority_priority", 100)])
def test_callers_cannot_omit_binding_or_supply_replacement_content_or_authority(field, value):
    with pytest.raises(ValidationError):
        ServiceCorrectionDecision(**dict(decision(), **{field: value}))


def test_request_binding_includes_action_owner_target_and_preview():
    original = ServiceCorrectionDecision(**decision())
    for field, value in (("action", "decline"), ("external_user_id", "other"),
        ("target_memory_id", uuid.uuid4()), ("target_version", "c" * 64), ("review_version", "d" * 64)):
        assert request_digest(original.model_copy(update={field: value})) != request_digest(original)


def test_scope_binding_preserves_json_types_and_ignores_object_key_order():
    assert digest({"project": True}) != digest({"project": "true"})
    assert digest({"project": 1}) != digest({"project": True})
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
