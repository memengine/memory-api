import pytest

from api.services.evidence_policy import (
    _explicit_confirmation_denial,
    _explicit_proposal_ordinal,
)


@pytest.mark.parametrize(
    "text",
    [
        "आमतौर पर मुझे इसी तरह की सहायता चाहिए।",
        "मैं उस विकल्प से सहमत हूं।",
    ],
)
def test_positive_hindi_words_do_not_trigger_denial_substrings(text: str) -> None:
    assert _explicit_confirmation_denial(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "नहीं, इसे याद मत रखिए।",
        "यह गलत है।",
    ],
)
def test_standalone_hindi_denials_remain_blocked(text: str) -> None:
    assert _explicit_confirmation_denial(text) is True


@pytest.mark.parametrize(
    ("text", "ordinal"),
    [
        ("Former option ko store karna.", 1),
        ("दूसरे प्रस्ताव को सहेजिए।", 2),
        ("पहले विकल्प को याद रखिए।", 1),
        ("प्रस्ताव संख्या 2 स्वीकार है।", 2),
    ],
)
def test_multilingual_explicit_ordinals(text: str, ordinal: int) -> None:
    active = [{"ordinal": 1}, {"ordinal": 2}]
    assert _explicit_proposal_ordinal(text, active) == ordinal
