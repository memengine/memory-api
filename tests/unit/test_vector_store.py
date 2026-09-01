from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from qdrant_client.http import models as qmodels

from api.db.vector_store import QdrantService
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry


def setup_function() -> None:
    state_client = MagicMock()
    state_client.get.return_value = None
    CircuitBreakerRegistry.reset(state_client=state_client)
    QdrantService._reset_shared_state()


def test_initialization_creates_collection_and_payload_indexes() -> None:
    client = MagicMock()
    client.collection_exists.return_value = False

    QdrantService(client=client)

    assert client.create_collection.call_count == 2
    created_collections = [
        call.kwargs["collection_name"] for call in client.create_collection.call_args_list
    ]
    assert created_collections == ["memories", "universal_memories"]

    for call in client.create_collection.call_args_list:
        create_collection_kwargs = call.kwargs
        assert create_collection_kwargs["vectors_config"].size == 1536
        assert create_collection_kwargs["vectors_config"].distance == qmodels.Distance.COSINE

    assert client.create_payload_index.call_count == 13
    indexed_fields = [
        (call.kwargs["collection_name"], call.kwargs["field_name"])
        for call in client.create_payload_index.call_args_list
    ]
    assert indexed_fields == [
        ("memories", "tenant_id"),
        ("memories", "proxy_user_id"),
        ("memories", "agent_id"),
        ("memories", "category"),
        ("memories", "importance_score"),
        ("memories", "is_archived"),
        ("memories", "created_at"),
        ("universal_memories", "user_uui_id"),
        ("universal_memories", "source_agent_id"),
        ("universal_memories", "category"),
        ("universal_memories", "importance_score"),
        ("universal_memories", "is_archived"),
        ("universal_memories", "created_at"),
    ]


def test_shared_client_is_reused_across_instances() -> None:
    with patch("api.db.vector_store.QdrantClient") as client_cls:
        client_instance = MagicMock()
        client_instance.collection_exists.return_value = True
        client_cls.return_value = client_instance

        first = QdrantService(url="http://example-qdrant")
        second = QdrantService(url="http://example-qdrant")

        assert first.client is second.client
        client_cls.assert_called_once()
    assert client_instance.create_payload_index.call_count == 13


def test_upsert_memory_retries_connection_errors() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    client.upsert.side_effect = [ConnectionError("temporary"), ConnectionError("temporary"), SimpleNamespace()]

    with patch("api.db.vector_store.time.sleep") as sleep_mock:
        service = QdrantService(client=client)
        result = service.upsert_memory(
            memory_id="memory-123",
            embedding=[0.1, 0.2, 0.3],
            payload={"tenant_id": "tenant-1", "proxy_user_id": "proxy-1"},
        )

    assert result is True
    assert client.upsert.call_count == 3
    sleep_mock.assert_any_call(0.5)
    sleep_mock.assert_any_call(1.0)


def test_search_memories_builds_expected_filter() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    expected_points = [SimpleNamespace(id="memory-1", score=0.99)]
    client.query_points.return_value = SimpleNamespace(points=expected_points)

    service = QdrantService(client=client)
    result = service.search_memories(
        query_embedding=[0.1, 0.2, 0.3],
        tenant_id="tenant-1",
        proxy_user_id="proxy-1",
        limit=7,
        category_filter="fact",
        agent_id="agent-1",
        include_archived=False,
    )

    assert result == expected_points

    query_kwargs = client.query_points.call_args.kwargs
    query_filter = query_kwargs["query_filter"]

    assert query_kwargs["limit"] == 7
    assert [condition.key for condition in query_filter.must] == [
        "tenant_id",
        "proxy_user_id",
        "category",
        "agent_id",
        "is_archived",
    ]
    assert [condition.match.value for condition in query_filter.must] == [
        "tenant-1",
        "proxy-1",
        "fact",
        "agent-1",
        False,
    ]


def test_delete_memory_uses_point_id_selector() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True

    service = QdrantService(client=client)
    result = service.delete_memory("memory-42")

    assert result is True
    points_selector = client.delete.call_args.kwargs["points_selector"]
    assert isinstance(points_selector, qmodels.PointIdsList)
    assert points_selector.points == ["memory-42"]


def test_delete_user_memories_returns_deleted_count() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    client.count.return_value = SimpleNamespace(count=3)

    service = QdrantService(client=client)
    deleted_count = service.delete_user_memories("user-7")

    assert deleted_count == 3
    delete_kwargs = client.delete.call_args.kwargs
    assert isinstance(delete_kwargs["points_selector"], qmodels.FilterSelector)
    assert delete_kwargs["points_selector"].filter.must[0].match.value == "user-7"


def test_delete_proxy_user_memories_returns_deleted_count() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    client.count.return_value = SimpleNamespace(count=2)

    service = QdrantService(client=client)
    deleted_count = service.delete_proxy_user_memories("tenant-7", "proxy-7")

    assert deleted_count == 2
    delete_kwargs = client.delete.call_args.kwargs
    selector = delete_kwargs["points_selector"]
    assert isinstance(selector, qmodels.FilterSelector)
    assert [condition.key for condition in selector.filter.must] == ["tenant_id", "proxy_user_id"]
    assert [condition.match.value for condition in selector.filter.must] == ["tenant-7", "proxy-7"]


def test_get_collection_stats_returns_dict_payload() -> None:
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = SimpleNamespace(
        model_dump=lambda: {"indexed_vectors_count": 10, "status": "green"}
    )

    service = QdrantService(client=client)

    assert service.get_collection_stats() == {
        "indexed_vectors_count": 10,
        "status": "green",
        "vectors_count": 10,
        "collection_name": "memories",
    }
