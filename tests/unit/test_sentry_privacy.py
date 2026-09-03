from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from api import main
from api.settings import Settings


def test_sentry_disables_default_pii_without_explicit_opt_in(monkeypatch) -> None:
    init = Mock()
    monkeypatch.setattr(main.sentry_sdk, "init", init)
    monkeypatch.setattr(
        main.sentry_sdk,
        "Hub",
        SimpleNamespace(current=SimpleNamespace(client=None)),
    )
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(sentry_dsn="https://public@example.ingest.sentry.io/1"),
    )

    main._configure_sentry()

    assert init.call_args.kwargs["send_default_pii"] is False


def test_sentry_pii_requires_explicit_configuration(monkeypatch) -> None:
    init = Mock()
    monkeypatch.setattr(main.sentry_sdk, "init", init)
    monkeypatch.setattr(
        main.sentry_sdk,
        "Hub",
        SimpleNamespace(current=SimpleNamespace(client=None)),
    )
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            sentry_dsn="https://public@example.ingest.sentry.io/1",
            sentry_send_default_pii=True,
        ),
    )

    main._configure_sentry()

    assert init.call_args.kwargs["send_default_pii"] is True


def test_sentry_event_scrubber_removes_customer_data() -> None:
    event = {
        "user": {"id": "customer-123", "email": "customer@example.com"},
        "request": {
            "method": "POST",
            "url": "https://api.example.com/v1/memories/add?user=customer-123",
            "headers": {"Authorization": "ApiKey secret"},
            "data": {"messages": [{"content": "private conversation"}]},
        },
        "breadcrumbs": {"values": [{"message": "private conversation"}]},
        "extra": {"job_payload": {"messages": ["private conversation"]}},
        "exception": {"values": [{"type": "ProviderError", "value": "private conversation"}]},
    }

    scrubbed = main._scrub_sentry_event(event, {})

    assert scrubbed["request"] == {"method": "POST"}
    assert "user" not in scrubbed
    assert "breadcrumbs" not in scrubbed
    assert "extra" not in scrubbed
    assert scrubbed["exception"]["values"][0] == {"type": "ProviderError"}
    assert event["request"]["data"]["messages"][0]["content"] == "private conversation"
