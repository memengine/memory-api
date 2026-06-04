from api.services.support.pii_redaction import count_redactions
from api.services.support.pii_redaction import redact_support_pii


def test_redacts_indian_financial_identifiers():
    payload = {
        "support_context": {
            "transaction_context": {
                "card": "4111-1111-1111-1111",
                "aadhaar": "1234 5678 9012",
                "pan": "ABCDE1234F",
                "otp": "OTP is 123456",
            }
        }
    }

    redacted = redact_support_pii(payload, support_type="banking_fintech")

    rendered = str(redacted)
    assert "4111-1111-1111-1111" not in rendered
    assert "1234 5678 9012" not in rendered
    assert "ABCDE1234F" not in rendered
    assert "OTP is 123456" not in rendered
    assert rendered.count("[REDACTED]") >= 4


def test_pnr_is_allowed_but_passport_is_redacted_for_travel():
    payload = {"support_context": {"booking": {"pnr": "ABC123", "passport": "A1234567"}}}

    redacted = redact_support_pii(payload, support_type="travel")

    assert redacted["support_context"]["booking"]["pnr"] == "ABC123"
    assert redacted["support_context"]["booking"]["passport"] == "[REDACTED]"


def test_counts_redactions_without_mutating_payload():
    payload = {"text": "PAN ABCDE1234F"}

    assert count_redactions(payload, support_type="banking_fintech") == 1
    assert payload["text"] == "PAN ABCDE1234F"
