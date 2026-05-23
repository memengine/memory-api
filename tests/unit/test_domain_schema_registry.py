from __future__ import annotations

from api.services.domain_schemas.base import BaseDomainSchema
from api.services.domain_schemas.registry import get_domain_schema
from api.services.domain_schemas.registry import register_domain_schema


class ToyDomainSchema(BaseDomainSchema):
    def get_domain(self) -> str:
        return "toy"


def test_edtech_domain_schema_is_registered() -> None:
    schema = get_domain_schema("edtech")
    assert schema is not None
    assert schema.get_domain() == "edtech"


def test_generic_domain_has_no_overlay() -> None:
    assert get_domain_schema(None) is None
    assert get_domain_schema("generic") is None


def test_contributors_can_register_new_domain_schema() -> None:
    register_domain_schema(ToyDomainSchema())
    assert isinstance(get_domain_schema("toy"), ToyDomainSchema)
