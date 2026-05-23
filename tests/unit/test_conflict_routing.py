from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.services.conflict_resolver import classify_resolution_path
from api.services.conflict_routing.generic_router import GenericEntityRouter
from api.services.conflict_routing.registry import get_router


def memory(content: str, proxy_user_id: uuid.UUID | None = None):
    return SimpleNamespace(
        content=content,
        proxy_user_id=proxy_user_id or uuid.uuid4(),
    )


def test_edtech_router_routes_personal_entities_to_user_session() -> None:
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "exam_date",
            memory("My exam is in March"),
            memory("My exam moved to April"),
            "edtech",
        )
        == "user_session"
    )


def test_healthcare_router_routes_medication_to_user_session() -> None:
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "medication",
            memory("I take medicine A"),
            memory("I now take medicine B"),
            "healthcare",
        )
        == "user_session"
    )


def test_hrtech_router_routes_role_requirement_to_tenant_review() -> None:
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "role_requirement",
            memory("The backend role requires Python"),
            memory("The backend role requires Go"),
            "hrtech",
        )
        == "tenant_review"
    )


def test_unknown_domain_uses_generic_content_signals() -> None:
    assert isinstance(get_router("unknown-domain"), GenericEntityRouter)
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "custom_entity",
            memory("I prefer Python examples"),
            memory("I now prefer TypeScript examples"),
            "unknown-domain",
        )
        == "user_session"
    )


def test_registered_router_unknown_entity_falls_back_to_generic() -> None:
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "new_future_entity",
            memory("Our team uses Python"),
            memory("Our stack moved to Go"),
            "edtech",
        )
        == "tenant_review"
    )


def test_generic_falls_back_to_ownership_when_signals_are_tied() -> None:
    same_user = uuid.uuid4()
    assert (
        classify_resolution_path(
            SimpleNamespace(),
            "anything",
            memory("Python", same_user),
            memory("Go", same_user),
            None,
        )
        == "user_session"
    )
