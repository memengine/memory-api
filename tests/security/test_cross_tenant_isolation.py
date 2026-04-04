from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.db.vector_store import QdrantService
from api.services.proxy_user_service import ProxyUserService


def test_same_external_user_id_hashes_differ_across_tenants() -> None:
    external_user_id = "shared-customer-user"

    tenant_a_hash = ProxyUserService.hash_external_user_id(str(uuid.uuid4()), external_user_id)
    tenant_b_hash = ProxyUserService.hash_external_user_id(str(uuid.uuid4()), external_user_id)

    assert tenant_a_hash != tenant_b_hash


def test_qdrant_search_filters_by_tenant_and_proxy_user() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    client.query_points.return_value = SimpleNamespace(points=[])

    service = QdrantService(client=client)
    service.search_memories(
        query_embedding=[0.1, 0.2, 0.3],
        tenant_id="tenant-a",
        proxy_user_id="proxy-a",
        limit=5,
        include_archived=False,
    )

    must_conditions = client.query_points.call_args.kwargs["query_filter"].must
    assert [condition.key for condition in must_conditions] == [
        "tenant_id",
        "proxy_user_id",
        "is_archived",
    ]
    assert [condition.match.value for condition in must_conditions] == [
        "tenant-a",
        "proxy-a",
        False,
    ]
