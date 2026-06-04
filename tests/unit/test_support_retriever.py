from api.db.models import SupportMemory
from api.services.support.support_retriever import SupportRetriever


def test_retriever_prioritizes_open_issue_and_risk():
    memory = SupportMemory(
        support_type="banking_fintech",
        customer_identity={"name": "Aarav", "tier": "premium", "customer_since": "2023"},
        current_open_issue={
            "issue_type": "fraud alert",
            "ref": "CASE-1",
            "since": "today",
            "urgency": "critical",
        },
        sentiment_pattern="high_escalation_risk",
        risk_signals=[{"risk_type": "fraud", "reason": "unauthorized UPI debit", "severity": "critical"}],
        issue_history=[{"issue_type": "card block", "resolution": "blocked card"}],
        support_context={"transaction_context": {"merchant": "Unknown"}},
    )

    prompt = SupportRetriever(session=None).build_system_prompt_addition(memory, query="fraud issue", max_tokens=120)

    assert "Support memory usage rules" in prompt
    assert "Do not claim you checked" in prompt
    assert "Never ask for or repeat OTPs" in prompt
    assert "Open issue: fraud alert" in prompt
    assert "High escalation risk" in prompt
    assert "fraud: unauthorized UPI debit" in prompt


def test_retriever_skips_unrelated_resolution_preference():
    memory = SupportMemory(
        support_type="ecommerce",
        resolution_preference={"method": "refund"},
        support_context={"order_context": {"status": "delayed"}},
    )

    prompt = SupportRetriever(session=None).build_system_prompt_addition(memory, query="where is my order")

    assert "Support memory usage rules" in prompt
    assert "Resolution preference" not in prompt


def test_retriever_always_pins_live_truth_boundary_under_token_pressure():
    memory = SupportMemory(
        support_type="ecommerce",
        customer_identity={"name": "Aditya", "tier": "vip", "customer_since": "2024"},
        current_open_issue={
            "issue_type": "refund",
            "ref": "ORD-44821",
            "since": "yesterday",
            "urgency": "high",
            "summary": "Customer wants refund if order is delayed.",
        },
        support_context={
            "order_context": {
                "order_id": "ORD-44821",
                "status": "delayed",
                "history": ["contacted support", "prefers refund", "asked for update"],
            }
        },
    )

    prompt = SupportRetriever(session=None).build_system_prompt_addition(memory, query="refund", max_tokens=35)

    assert "remembered customer context, not live system truth" in prompt
    assert "refunded" in prompt
    assert "unless your own tool/API result confirms it" in prompt
    assert "Open issue: refund" in prompt


def test_retriever_only_adds_banking_pii_rule_for_banking_support():
    ecommerce_memory = SupportMemory(
        support_type="ecommerce",
        support_context={"order_context": {"order_id": "ORD-44821"}},
    )
    banking_memory = SupportMemory(
        support_type="banking_fintech",
        support_context={"transaction_context": {"status": "failed"}},
    )

    ecommerce_prompt = SupportRetriever(session=None).build_system_prompt_addition(ecommerce_memory)
    banking_prompt = SupportRetriever(session=None).build_system_prompt_addition(banking_memory)

    assert "Never ask for or repeat OTPs" not in ecommerce_prompt
    assert "Never ask for or repeat OTPs" in banking_prompt
