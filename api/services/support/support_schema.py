from __future__ import annotations

from typing import Any


SupportField = dict[str, Any]

ALLOWED_SUPPORT_TYPES = (
    "saas",
    "ecommerce",
    "banking_fintech",
    "travel",
    "telecom",
    "edtech_support",
    "general_info",
)

BASE_FIELDS: dict[str, SupportField] = {
    "support_type": {
        "category": "fact",
        "content_template": "Support type: {value}",
        "allowed_values": list(ALLOWED_SUPPORT_TYPES),
        "importance_base": 10.0,
        "description": "What kind of support product this tenant provides.",
    },
    "customer_identity": {
        "category": "fact",
        "content_template": "Customer: {name} | Account: {account_ref} | Tier: {tier}",
        "metadata_keys": ["name", "account_ref", "tier", "customer_since", "verified"],
        "importance_base": 9.5,
    },
    "communication_preference": {
        "category": "preference",
        "content_template": "Prefers {channel} - {style}",
        "metadata_keys": ["channel", "style", "response_length", "technical_comfort"],
        "allowed_channels": ["chat", "email", "call", "whatsapp"],
        "allowed_styles": ["formal", "casual", "direct", "detailed"],
        "importance_base": 8.0,
    },
    "language_profile": {
        "category": "preference",
        "content_template": "Language: {primary}, comfort: {comfort}",
        "metadata_keys": ["primary", "comfort", "regional_dialect"],
        "importance_base": 8.5,
    },
    "current_open_issue": {
        "category": "goal",
        "content_template": "Open issue: {issue_type} | Ref: {ref} | Since: {since}",
        "metadata_keys": ["issue_type", "summary", "ref", "since", "urgency", "status", "last_update"],
        "importance_base": 9.8,
        "description": "The most important unresolved support issue.",
    },
    "issue_history": {
        "category": "fact",
        "content_template": "Past issue: {issue_type} - resolved via {resolution}",
        "metadata_keys": ["issue_type", "summary", "resolution", "resolution_date", "satisfaction", "ticket_ref"],
        "importance_base": 8.5,
    },
    "resolution_preference": {
        "category": "preference",
        "content_template": "Prefers resolution via {method}",
        "metadata_keys": ["method", "escalation_threshold", "self_service_comfort"],
        "allowed_methods": ["refund", "replacement", "credit", "callback", "step_by_step", "escalate", "workaround"],
        "importance_base": 8.0,
    },
    "sentiment_pattern": {
        "category": "fact",
        "content_template": "Sentiment: {pattern} - {note}",
        "metadata_keys": ["pattern", "note", "escalation_risk"],
        "allowed_patterns": ["calm", "frustrated", "repeat_complainer", "easy_to_resolve", "high_escalation_risk"],
        "importance_base": 7.5,
    },
    "risk_signals": {
        "category": "fact",
        "content_template": "Risk: {risk_type} - {reason}",
        "metadata_keys": ["risk_type", "reason", "severity", "status"],
        "allowed_risk_types": ["churn", "refund", "fraud", "compliance", "legal", "safety", "vip"],
        "importance_base": 9.0,
    },
}

SAAS_FIELDS: dict[str, SupportField] = {
    "workspace_context": {"category": "fact", "metadata_keys": ["workspace", "role", "team_size"], "importance_base": 8.5},
    "subscription_context": {"category": "fact", "metadata_keys": ["plan", "billing_cycle", "seats"], "importance_base": 9.0},
    "integration_context": {"category": "fact", "metadata_keys": ["integration", "status", "last_error"], "importance_base": 8.5},
    "bug_history": {"category": "fact", "metadata_keys": ["bug", "status", "workaround"], "importance_base": 8.5},
    "feature_requests": {"category": "goal", "metadata_keys": ["feature", "priority", "use_case"], "importance_base": 8.0},
    "onboarding_blockers": {"category": "goal", "metadata_keys": ["blocker", "owner", "status"], "importance_base": 8.5},
}

ECOMMERCE_FIELDS: dict[str, SupportField] = {
    "order_context": {"category": "fact", "metadata_keys": ["order_id", "product", "status", "order_date", "expected_delivery", "amount"], "importance_base": 9.5},
    "return_refund_history": {"category": "fact", "metadata_keys": ["reason", "outcome", "count_lifetime", "last_return_date"], "importance_base": 8.0},
    "delivery_preferences": {"category": "preference", "metadata_keys": ["address_type", "preferred_slot", "delivery_instructions"], "importance_base": 7.0},
    "product_preferences": {"category": "preference", "metadata_keys": ["categories", "brands", "size_info", "avoid"], "importance_base": 6.5},
    "seller_issues": {"category": "fact", "metadata_keys": ["seller", "issue_type", "frequency"], "importance_base": 7.5},
}

BANKING_FINTECH_FIELDS: dict[str, SupportField] = {
    "account_context": {"category": "fact", "metadata_keys": ["account_type", "tier", "since", "products_held"], "importance_base": 9.5},
    "complaint_history": {"category": "fact", "metadata_keys": ["complaint_type", "status", "ticket_ref", "amount_disputed", "resolution_days"], "importance_base": 9.0},
    "transaction_context": {"category": "fact", "metadata_keys": ["txn_type", "amount", "date", "merchant", "status"], "importance_base": 9.5},
    "kyc_status": {"category": "fact", "metadata_keys": ["status", "pending_docs", "expiry_date"], "importance_base": 8.5},
    "loan_emi_context": {"category": "fact", "metadata_keys": ["product", "issue_type", "emi_date", "outstanding"], "importance_base": 9.0},
    "fraud_sensitivity": {"category": "fact", "metadata_keys": ["alert_type", "status", "reported_date"], "importance_base": 10.0},
}

TRAVEL_FIELDS: dict[str, SupportField] = {
    "booking_context": {"category": "fact", "metadata_keys": ["pnr", "route", "date", "status", "booking_type", "amount"], "importance_base": 9.5},
    "loyalty_context": {"category": "fact", "metadata_keys": ["program", "tier", "points", "expiry"], "importance_base": 8.5},
    "travel_preferences": {"category": "preference", "metadata_keys": ["seat_pref", "meal_pref", "class_pref", "special_assistance"], "importance_base": 8.0},
    "disruption_history": {"category": "fact", "metadata_keys": ["type", "compensation", "compensation_amount", "date", "satisfaction"], "importance_base": 8.5},
    "frequent_routes": {"category": "fact", "metadata_keys": ["origin", "destination", "frequency", "purpose"], "importance_base": 7.0},
}

TELECOM_FIELDS: dict[str, SupportField] = {
    "plan_context": {"category": "fact", "metadata_keys": ["plan_name", "data_limit", "monthly_cost", "renewal_date", "add_ons"], "importance_base": 9.0},
    "device_context": {"category": "fact", "metadata_keys": ["device_model", "os_version", "sim_type", "imei_last4"], "importance_base": 8.0},
    "network_issues": {"category": "fact", "metadata_keys": ["issue_type", "location", "frequency", "last_reported"], "importance_base": 8.5},
    "service_requests": {"category": "fact", "metadata_keys": ["type", "status", "request_date", "ref_number"], "importance_base": 8.0},
    "port_context": {"category": "goal", "metadata_keys": ["target_operator", "status", "reason", "requested_date"], "importance_base": 9.0},
}

EDTECH_SUPPORT_FIELDS: dict[str, SupportField] = {
    "enrollment_context": {"category": "fact", "metadata_keys": ["course_name", "platform", "status", "enrollment_date", "completion_pct"], "importance_base": 9.5},
    "access_issues": {"category": "fact", "metadata_keys": ["issue_type", "status", "reported_date", "device"], "importance_base": 9.0},
    "payment_context": {"category": "fact", "metadata_keys": ["amount", "method", "issue", "transaction_ref"], "importance_base": 9.0},
    "certificate_context": {"category": "goal", "metadata_keys": ["course", "status", "expected_date", "issue"], "importance_base": 8.5},
}

GENERAL_INFO_FIELDS: dict[str, SupportField] = {
    "intent_context": {"category": "goal", "metadata_keys": ["intent_type", "details", "sales_stage", "urgency"], "importance_base": 8.5},
    "product_interest": {"category": "fact", "metadata_keys": ["product", "feature", "use_case", "budget_signal"], "importance_base": 8.0},
    "objections": {"category": "fact", "metadata_keys": ["objection_type", "detail", "addressed"], "importance_base": 8.5},
    "referral_context": {"category": "fact", "metadata_keys": ["source", "page", "campaign", "first_visit_date"], "importance_base": 7.0},
}

SUPPORT_TYPE_TO_EXTENSIONS: dict[str, dict[str, SupportField]] = {
    "saas": SAAS_FIELDS,
    "ecommerce": ECOMMERCE_FIELDS,
    "banking_fintech": BANKING_FINTECH_FIELDS,
    "travel": TRAVEL_FIELDS,
    "telecom": TELECOM_FIELDS,
    "edtech_support": EDTECH_SUPPORT_FIELDS,
    "general_info": GENERAL_INFO_FIELDS,
}

SUPPORT_NEVER_STORE = (
    "Full card numbers.",
    "Full account numbers.",
    "Full IMEI numbers.",
    "Passwords, PINs, or OTP values.",
    "Aadhaar or PAN numbers.",
    "Passport numbers.",
    "Specific patient medical data.",
    "Names of third parties mentioned casually.",
    "Agent names or agent IDs.",
)


def active_fields_for(support_type: str | None) -> dict[str, SupportField]:
    normalized = support_type if support_type in SUPPORT_TYPE_TO_EXTENSIONS else "general_info"
    return {**BASE_FIELDS, **SUPPORT_TYPE_TO_EXTENSIONS[normalized]}


__all__ = [
    "ALLOWED_SUPPORT_TYPES",
    "BASE_FIELDS",
    "SUPPORT_TYPE_TO_EXTENSIONS",
    "SUPPORT_NEVER_STORE",
    "active_fields_for",
]
