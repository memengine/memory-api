import pytest

from api.services.evidence_policy import (
    _explicit_confirmation_denial,
    _explicit_proposal_ordinal,
    explicit_proposal_ordinal,
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


@pytest.mark.parametrize(
    ("text", "ordinal"),
    [
        ("Record only the third suggestion.", 3),
        ("Proposal number two gets my approval.", 2),
        ("Correction: first nahi, third entry save karo.", 3),
        ("ऊपर से तीसरा प्रस्ताव दर्ज कीजिए।", 3),
        ("इन पाँच में चौथे को चुनकर याद रखिए।", 4),
        ("पाँचवाँ विकल्प सहेजिए।", 5),
    ],
)
def test_server_owned_ordinal_parser_supports_larger_multilingual_lists(
    text: str,
    ordinal: int,
) -> None:
    active = [{"ordinal": value} for value in range(1, 6)]

    assert explicit_proposal_ordinal(text, active) == ordinal
