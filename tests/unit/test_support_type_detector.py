from api.services.support.support_type_detector import SupportTypeDetector


def test_tenant_configured_type_overrides_detection():
    detector = SupportTypeDetector()

    result = detector.detect_result(
        [{"role": "user", "content": "My order is delayed and I want a refund."}],
        tenant_configured_type="banking_fintech",
    )

    assert result.support_type == "banking_fintech"
    assert result.source == "tenant_configured"
    assert result.confidence == 1.0


def test_detects_ecommerce_from_order_signals():
    detector = SupportTypeDetector()

    assert detector.detect([{"role": "user", "content": "My Flipkart order tracking is stuck."}]) == "ecommerce"


def test_defaults_to_general_info_when_no_signal_matches():
    detector = SupportTypeDetector()

    assert detector.detect([{"role": "user", "content": "hello there"}]) == "general_info"


def test_detection_respects_allowed_support_types():
    detector = SupportTypeDetector()

    result = detector.detect_result(
        [{"role": "user", "content": "My order is delayed but this tenant only allows SaaS and General."}],
        allowed_support_types=["saas", "general_info"],
    )

    assert result.support_type in {"saas", "general_info"}
    assert "ecommerce" not in result.scores
