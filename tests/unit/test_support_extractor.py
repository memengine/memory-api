from api.db.models import SupportMemory
from api.services.support.support_extractor import SupportExtractor
from api.services.support.support_extractor import _normalize_routing
from api.services.support.support_extractor import _support_type_from_response


def test_support_extractor_merges_common_and_context_fields():
    memory = SupportMemory(
        customer_identity={"tier": "standard"},
        support_context={"order_context": {"status": "delayed"}},
        issue_history=[],
        risk_signals=[],
    )
    fields_updated: set[str] = set()
    extractor = object.__new__(SupportExtractor)

    extractor._merge_extracted(
        memory,
        {
            "customer_identity": {"name": "Aarav", "tier": "premium"},
            "support_context": {"order_context": {"order_id": "ORD-1"}},
            "issue_history": [{"issue_type": "delivery", "resolution": "reship"}],
            "sentiment_pattern": "frustrated",
        },
        fields_updated,
    )

    assert memory.customer_identity == {"tier": "premium", "name": "Aarav"}
    assert memory.support_context["order_context"] == {"status": "delayed", "order_id": "ORD-1"}
    assert memory.issue_history == [{"issue_type": "delivery", "resolution": "reship"}]
    assert memory.sentiment_pattern == "frustrated"
    assert {"customer_identity", "support_context", "issue_history", "sentiment_pattern"} <= fields_updated


def test_support_extractor_replaces_current_open_issue():
    memory = SupportMemory(current_open_issue={"issue_type": "old"})
    fields_updated: set[str] = set()
    extractor = object.__new__(SupportExtractor)

    extractor._merge_extracted(
        memory,
        {"current_open_issue": {"issue_type": "refund", "urgency": "high"}},
        fields_updated,
    )

    assert memory.current_open_issue == {"issue_type": "refund", "urgency": "high"}
    assert "current_open_issue" in fields_updated


def test_normalize_routing_single_mode_is_fixed():
    routing = _normalize_routing(
        support_type_mode="single",
        tenant_configured_type="banking_fintech",
        allowed_support_types=["ecommerce"],
        existing_support_type=None,
    )

    assert routing == {
        "support_type_mode": "single",
        "fixed_support_type": "banking_fintech",
        "allowed_support_types": ["banking_fintech"],
    }


def test_normalize_routing_multi_mode_uses_allowed_types():
    routing = _normalize_routing(
        support_type_mode="multi",
        tenant_configured_type=None,
        allowed_support_types=["saas", "ecommerce"],
        existing_support_type="banking_fintech",
    )

    assert routing["fixed_support_type"] is None
    assert routing["allowed_support_types"] == ["saas", "ecommerce"]


def test_support_type_from_response_accepts_high_confidence_allowed_type():
    support_type, source, confidence = _support_type_from_response(
        data={"support_type": "ecommerce", "support_type_confidence": 0.91},
        fallback_type="general_info",
        fallback_source="detected",
        fallback_confidence=0.35,
        support_type_mode="multi",
        allowed_support_types=["saas", "ecommerce", "general_info"],
    )

    assert support_type == "ecommerce"
    assert source == "allowed_detected"
    assert confidence == 0.91


def test_support_type_from_response_low_confidence_falls_back_to_general_info():
    support_type, source, confidence = _support_type_from_response(
        data={"support_type": "ecommerce", "support_type_confidence": 0.4},
        fallback_type="ecommerce",
        fallback_source="detected",
        fallback_confidence=0.55,
        support_type_mode="multi",
        allowed_support_types=["ecommerce", "general_info"],
    )

    assert support_type == "general_info"
    assert source == "allowed_detected"
    assert confidence == 0.4
