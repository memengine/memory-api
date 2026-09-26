from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class EvidenceAuthority(IntEnum):
    """Server-owned authority ceilings for conversational evidence."""

    UNTRUSTED_CONTEXT = 10
    CLIENT_ASSERTION = 20
    LEGACY_CONVERSATION = 50
    APPLICATION_ATTESTED = 60
    MEMORYOS_ATTESTED = 100


@dataclass(frozen=True)
class EvidenceDecision:
    accepted: bool
    authority: EvidenceAuthority
    reason: str
    user_turn_indexes: tuple[int, ...] = ()
    proposal_turn_index: int | None = None
    proposal_id: str | None = None
    proposal_group_id: str | None = None
    proposal_ordinal: int | None = None
    review_required: bool = False


def authority_for_submission(
    *,
    mode: str,
    has_memoryos_envelope: bool = False,
    writer_capabilities: set[str] | None = None,
) -> EvidenceAuthority:
    """Resolve authority from server-observed facts, never caller metadata."""

    if has_memoryos_envelope:
        return EvidenceAuthority.MEMORYOS_ATTESTED
    if "user_evidence:attest" in (writer_capabilities or set()):
        return EvidenceAuthority.APPLICATION_ATTESTED
    if mode == "client_assertion":
        return EvidenceAuthority.CLIENT_ASSERTION
    # Preserve the current API authority while the signed evidence-envelope
    # endpoint is introduced. This is intentionally below attested evidence.
    return EvidenceAuthority.LEGACY_CONVERSATION


def validate_conversational_evidence(
    *,
    messages: list[dict[str, Any]],
    evidence_turns: Any,
    evidence_relation: Any,
    proposal_turn: Any,
    visible_turn_indexes: set[int] | None = None,
    proposal_confirmation_enabled: bool = False,
    active_proposals: list[dict[str, Any]] | None = None,
    structured_proposal_decision: bool = False,
) -> EvidenceDecision:
    """Verify model citations against typed roles, ordering, and proposal scope."""

    if not isinstance(evidence_turns, list) or not evidence_turns:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "missing_evidence")
    indexes = tuple(
        sorted(
            {
                value
                for value in evidence_turns
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < len(messages)
            }
        )
    )
    if len(indexes) != len(evidence_turns):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "invalid_evidence_turn")
    if visible_turn_indexes is not None and any(index not in visible_turn_indexes for index in indexes):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "evidence_not_visible_to_model")

    cited_user_indexes = tuple(
        index
        for index in indexes
        if _canonical_role(messages[index]) == "user"
        and _source_kind(messages[index]) in {"direct_user_input", "client_assertion"}
    )

    relation = str(evidence_relation or "direct_user_statement")
    if relation == "direct_user_statement":
        if not cited_user_indexes:
            return EvidenceDecision(
                False, EvidenceAuthority.CLIENT_ASSERTION, "no_user_evidence"
            )
        return EvidenceDecision(
            True,
            EvidenceAuthority.CLIENT_ASSERTION,
            "direct_user_statement",
            user_turn_indexes=cited_user_indexes,
        )
    if relation != "user_confirmed_assistant_proposal":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "unsupported_relation")
    if not proposal_confirmation_enabled:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "semantic_confirmation_not_enabled")
    if not isinstance(proposal_turn, int) or isinstance(proposal_turn, bool):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "missing_proposal_turn")
    if not 0 <= proposal_turn < len(messages):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "invalid_proposal_turn")
    if visible_turn_indexes is not None and proposal_turn not in visible_turn_indexes:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_visible_to_model")
    if _canonical_role(messages[proposal_turn]) != "assistant":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_assistant")
    if _source_kind(messages[proposal_turn]) != "assistant_output":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_assistant_output")
    if not bool(messages[proposal_turn].get("is_memory_proposal")):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_registered")
    eligible_user_indexes = tuple(
        index
        for index, message in enumerate(messages)
        if index > proposal_turn
        and _canonical_role(message) == "user"
        and _source_kind(message) in {"direct_user_input", "client_assertion"}
        and (visible_turn_indexes is None or index in visible_turn_indexes)
    )
    user_indexes = tuple(
        index for index in cited_user_indexes if index > proposal_turn
    ) or eligible_user_indexes[-1:]
    if not user_indexes:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "no_user_evidence",
        )
    if max(user_indexes) - proposal_turn > 12:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "proposal_outside_turn_window",
        )

    active = list(active_proposals or [])
    target = next(
        (
            proposal
            for proposal in active
            if int(proposal.get("turn_index", -1)) == proposal_turn
            and str(proposal.get("turn_id") or "")
            == str(messages[proposal_turn].get("turn_id") or "")
            and str(proposal.get("content_sha256") or "")
            == str(messages[proposal_turn].get("turn_content_sha256") or "")
        ),
        None,
    )
    if target is None:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_active")

    latest_user_text = str(messages[max(user_indexes)].get("content") or "")
    target_ordinal = int(target.get("ordinal", -1))
    if structured_proposal_decision:
        if structured_proposal_denied(
            latest_user_text,
            target_ordinal=target_ordinal,
            active_proposals=active,
        ):
            return EvidenceDecision(
                False,
                EvidenceAuthority.CLIENT_ASSERTION,
                "explicit_confirmation_denied",
                user_turn_indexes=user_indexes,
                proposal_turn_index=proposal_turn,
            )
        return EvidenceDecision(
            True,
            EvidenceAuthority.CLIENT_ASSERTION,
            "verified_structured_proposal_reference",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
            proposal_id=str(target.get("id") or "") or None,
            proposal_group_id=str(target.get("group_id") or "") or None,
            proposal_ordinal=(
                int(target["ordinal"])
                if target.get("ordinal") is not None
                else None
            ),
        )

    referenced_ordinal = _explicit_proposal_ordinal(latest_user_text, active)
    denied_ordinals = _explicitly_denied_proposal_ordinals(
        latest_user_text,
        active,
    )
    if has_explicit_proposal_denial(latest_user_text) and (
        not denied_ordinals
        or referenced_ordinal is None
        or target_ordinal in denied_ordinals
    ):
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "explicit_confirmation_denied",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
        )
    if referenced_ordinal is not None and int(target.get("ordinal", -1)) != referenced_ordinal:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "proposal_reference_mismatch",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
            review_required=True,
        )
    if referenced_ordinal is None and len(active) != 1:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "ambiguous_proposal_reference",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
            review_required=True,
        )

    return EvidenceDecision(
        True,
        EvidenceAuthority.CLIENT_ASSERTION,
        "verified_proposal_reference",
        user_turn_indexes=user_indexes,
        proposal_turn_index=proposal_turn,
        proposal_id=str(target.get("id") or "") or None,
        proposal_group_id=str(target.get("group_id") or "") or None,
        proposal_ordinal=int(target["ordinal"]) if target.get("ordinal") is not None else None,
    )


_CONFIRMATION_DENIAL_PATTERNS = (
    re.compile(r"[?？]\s*$"),
    re.compile(r"\b(?:no|nope|nah)\b", re.IGNORECASE),
    re.compile(r"\b(?:do\s+not|don't|dont)\s+(?:remember|save|store|keep)\b", re.IGNORECASE),
    re.compile(r"\b(?:not|never)\s+(?:agreeing|approve|remember|save|store|keep)\b", re.IGNORECASE),
    re.compile(r"\b(?:that|this|it)\s+is\s+not\s+my\s+(?:default|preference)\b", re.IGNORECASE),
    re.compile(r"\b(?:nahi|nahin)\b", re.IGNORECASE),
    re.compile(r"\bmat\s+(?:rakhna|rakho|banana|karo)\b", re.IGNORECASE),
    re.compile(r"\b(?:reject|decline|discard)(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s,;.!?।])(?:नहीं|मत|गलत)(?=$|[\s,;.!?।])"),
    re.compile(r"(?:अस्वीकार|खारिज)"),
    re.compile(r"न\s+(?:रख|याद|लागू|अपना)"),
)


def has_explicit_proposal_denial(user_text: str) -> bool:
    """Return true only for deterministic rejection or question signals."""

    normalized = " ".join(user_text.split())
    return any(pattern.search(normalized) for pattern in _CONFIRMATION_DENIAL_PATTERNS)


_UNAMBIGUOUS_MEMORY_DENIAL_PATTERNS = (
    re.compile(
        r"\b(?:do\s+not|don't|dont|never)\s+"
        r"(?:add|keep|record|remember|save|store)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:add|keep|record|remember|save|store)\b(?:\s+\w+){0,3}\s+"
        r"(?:nahi|nahin|not|never)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:nahi|nahin)\b(?:\s+\w+){0,3}\s+"
        r"(?:add|keep|record|remember|save|store)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bmat\s+(?:add|keep|record|remember|save|store|rakhna|rakho|banana|karo)\b", re.IGNORECASE),
    re.compile(r"\b(?:reject|decline|discard)(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:that|this|it)\s+is\s+not\s+my\s+(?:default|preference)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:no|nope|nah|nahi|nahin)\s*[.!]*\s*$", re.IGNORECASE),
    re.compile(r"(?:मत|नहीं)(?:\s+\S+){0,3}\s+(?:जोड़|रख|याद|सहेज|दर्ज)"),
    re.compile(r"(?:जोड़|रख|याद|सहेज|दर्ज)\S*(?:\s+\S+){0,3}\s+(?:मत|नहीं)"),
    re.compile(r"(?:अस्वीकार|खारिज)"),
)


def has_unambiguous_memory_denial(user_text: str) -> bool:
    """Return true for explicit memory rejection, not incidental negation."""

    normalized = " ".join(user_text.split())
    return any(
        pattern.search(normalized)
        for pattern in _UNAMBIGUOUS_MEMORY_DENIAL_PATTERNS
    )


def structured_proposal_denied(
    user_text: str,
    *,
    target_ordinal: int,
    active_proposals: list[dict[str, Any]],
) -> bool:
    """Reject only a whole-memory denial or denial scoped to the selected item."""

    denied_ordinals = structured_denied_proposal_ordinals(
        user_text,
        active_proposals,
    )
    return (
        target_ordinal in denied_ordinals
        or has_unambiguous_memory_denial(user_text)
    )


# Backward-compatible internal alias. Keep existing imports stable while the
# descriptive public helper name is used by new extraction paths.
_explicit_confirmation_denial = has_explicit_proposal_denial


_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

_SELECTION_ORDINAL_ALIASES = {
    **_ORDINAL_WORDS,
    "pehla": 1,
    "pahla": 1,
    "pehli": 1,
    "pehle": 1,
    "doosra": 2,
    "dusra": 2,
    "doosri": 2,
    "dusri": 2,
    "doosre": 2,
    "dusre": 2,
    "teesra": 3,
    "tisra": 3,
    "teesri": 3,
    "tisri": 3,
    "teesre": 3,
    "tisre": 3,
    "chautha": 4,
    "chauthi": 4,
    "chauthe": 4,
    "paanchva": 5,
    "panchva": 5,
    "paanchvi": 5,
    "panchvi": 5,
    "paanchve": 5,
    "panchve": 5,
    "chhatha": 6,
    "chhathi": 6,
    "chhathe": 6,
    "saatva": 7,
    "saatvi": 7,
    "saatve": 7,
    "aathva": 8,
    "aathvi": 8,
    "aathve": 8,
    "nauva": 9,
    "nauvi": 9,
    "nauve": 9,
    "dasva": 10,
    "dasvi": 10,
    "dasve": 10,
    "पहले": 1,
    "पहला": 1,
    "पहली": 1,
    "प्रथम": 1,
    "दूसरे": 2,
    "दूसरा": 2,
    "दूसरी": 2,
    "द्वितीय": 2,
    "तीसरे": 3,
    "तीसरा": 3,
    "तीसरी": 3,
    "तृतीय": 3,
    "चौथे": 4,
    "चौथा": 4,
    "चौथी": 4,
    "चतुर्थ": 4,
    "पाँचवें": 5,
    "पांचवें": 5,
    "पाँचवाँ": 5,
    "पांचवां": 5,
    "पाँचवीं": 5,
    "पांचवीं": 5,
    "छठे": 6,
    "छठा": 6,
    "छठी": 6,
    "सातवें": 7,
    "सातवाँ": 7,
    "सातवां": 7,
    "सातवीं": 7,
    "आठवें": 8,
    "आठवाँ": 8,
    "आठवां": 8,
    "आठवीं": 8,
    "नौवें": 9,
    "नौवाँ": 9,
    "नौवां": 9,
    "नौवीं": 9,
    "दसवें": 10,
    "दसवाँ": 10,
    "दसवां": 10,
    "दसवीं": 10,
}

_CARDINAL_ORDINAL_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_CARDINAL_SELECTION_MARKERS = {
    "choice",
    "entry",
    "number",
    "option",
    "proposal",
    "suggestion",
}


def normalized_selection_tokens(value: Any) -> tuple[str, ...]:
    """Return case-folded Unicode words without splitting combining marks."""

    tokens: list[str] = []
    current: list[str] = []
    for character in unicodedata.normalize("NFKC", str(value or "")).casefold():
        if unicodedata.category(character)[0] in {"L", "M", "N"}:
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def proposal_selection_ordinal(
    selection_evidence: str,
    active_proposals: list[dict[str, Any]],
) -> int | None:
    """Resolve an ordinal from a short, verbatim user selection span.

    Unlike the legacy sentence matcher, this accepts a bare ordinal because
    the caller separately verifies that the span is quoted from the user turn.
    Multiple or unknown ordinals fail closed.
    """

    normalized = " ".join(selection_evidence.casefold().split())
    if not normalized or len(normalized) > 80:
        return None
    tokens = normalized_selection_tokens(normalized)
    if not tokens or len(tokens) > 4:
        return None
    ordinals = [
        int(item["ordinal"])
        for item in active_proposals
        if item.get("ordinal") is not None
    ]
    last_ordinal = max(ordinals) if ordinals else None
    aliases: dict[str, int | None] = {
        **_SELECTION_ORDINAL_ALIASES,
        "former": 1,
        "last": last_ordinal,
        "latter": last_ordinal,
        "अंतिम": last_ordinal,
    }
    matches = {
        int(aliases[token])
        for token in tokens
        if token in aliases and aliases[token] is not None
    }
    matches.update(
        int(token)
        for token in tokens
        if token.isdigit() and 1 <= int(token) <= 99
    )
    matches.update(
        _CARDINAL_ORDINAL_WORDS[token]
        for index, token in enumerate(tokens)
        if token in _CARDINAL_ORDINAL_WORDS
        and index > 0
        and tokens[index - 1] in _CARDINAL_SELECTION_MARKERS
    )
    return next(iter(matches)) if len(matches) == 1 else None


def structured_denied_proposal_ordinals(
    user_text: str,
    active_proposals: list[dict[str, Any]],
) -> set[int]:
    """Return only proposal ordinals negated near their textual reference."""

    normalized = " ".join(user_text.lower().split())
    if not normalized:
        return set()
    ordinals = [
        int(item["ordinal"])
        for item in active_proposals
        if item.get("ordinal") is not None
    ]
    last_ordinal = max(ordinals) if ordinals else None
    mentions: list[tuple[int, int, int]] = []
    words = {
        **_SELECTION_ORDINAL_ALIASES,
        "former": 1,
        **({"last": last_ordinal, "latter": last_ordinal} if last_ordinal else {}),
    }
    for word, ordinal in words.items():
        if ordinal is None:
            continue
        mentions.extend(
            (match.start(), match.end(), int(ordinal))
            for match in re.finditer(rf"\b{re.escape(word)}\b", normalized)
        )
    hindi_words = {
        word: ordinal
        for word, ordinal in _SELECTION_ORDINAL_ALIASES.items()
        if any(ord(character) > 127 for character in word)
    }
    if last_ordinal:
        hindi_words["अंतिम"] = last_ordinal
    for word, ordinal in hindi_words.items():
        mentions.extend(
            (match.start(), match.end(), int(ordinal))
            for match in re.finditer(re.escape(word), normalized)
        )
    for match in re.finditer(
        r"(?:\b(?:option|proposal|choice)(?:\s+number)?|\bnumber|"
        r"(?:विकल्प|प्रस्ताव)(?:\s+संख्या)?)\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        normalized,
    ):
        mentions.append((match.start(), match.end(), int(match.group(1))))

    denied: set[int] = set()
    before_denial = re.compile(
        r"(?:\bnot|\bnever|\bdon't|\bdont|\bnahi|\bnahin|नहीं|मत)"
        r"(?:\s+\w+){0,6}\s*$",
        re.IGNORECASE,
    )
    after_denial = re.compile(
        r"^(?:\s+\w+){0,6}\s*(?:\bnot\b|\bnever\b|\bdon't\b|\bdont\b|"
        r"\bnahi\b|\bnahin\b|नहीं|मत)",
        re.IGNORECASE,
    )
    for start, end, ordinal in mentions:
        before = normalized[max(0, start - 48) : start]
        after = normalized[end : min(len(normalized), end + 48)]
        if before_denial.search(before) or after_denial.search(after):
            denied.add(ordinal)
    return denied


def _explicitly_denied_proposal_ordinals(
    user_text: str,
    active_proposals: list[dict[str, Any]],
) -> set[int]:
    """Legacy proposal-denial matcher retained for compatibility."""

    normalized = " ".join(user_text.lower().split())
    if not normalized:
        return set()
    ordinals = [
        int(item["ordinal"])
        for item in active_proposals
        if item.get("ordinal") is not None
    ]
    last_ordinal = max(ordinals) if ordinals else None
    mentions: list[tuple[int, int, int]] = []
    words = {
        **_ORDINAL_WORDS,
        "former": 1,
        **({"last": last_ordinal, "latter": last_ordinal} if last_ordinal else {}),
        "pehla": 1,
        "pahla": 1,
        "pehli": 1,
        "pehle": 1,
        "doosra": 2,
        "dusra": 2,
        "doosri": 2,
        "dusri": 2,
        "doosre": 2,
        "dusre": 2,
    }
    for word, ordinal in words.items():
        if ordinal is None:
            continue
        mentions.extend(
            (match.start(), match.end(), int(ordinal))
            for match in re.finditer(rf"\b{re.escape(word)}\b", normalized)
        )
    hindi_words = {
        "पहले": 1,
        "पहला": 1,
        "पहली": 1,
        "दूसरे": 2,
        "दूसरा": 2,
        "दूसरी": 2,
        **({"अंतिम": last_ordinal} if last_ordinal else {}),
    }
    for word, ordinal in hindi_words.items():
        mentions.extend(
            (match.start(), match.end(), int(ordinal))
            for match in re.finditer(re.escape(word), normalized)
        )
    for match in re.finditer(
        r"(?:\b(?:option|proposal|choice)(?:\s+number)?|\bnumber|"
        r"(?:विकल्प|प्रस्ताव)(?:\s+संख्या)?)\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        normalized,
    ):
        mentions.append((match.start(), match.end(), int(match.group(1))))

    denied: set[int] = set()
    before_denial = re.compile(
        r"(?:\bnot|\bnever|\bdon't|\bdont|\bnahi|\bnahin|नहीं|मत)"
        r"(?:\s+\w+){0,3}\s*$",
        re.IGNORECASE,
    )
    after_denial = re.compile(
        r"^(?:\s+\w+){0,3}\s*(?:\bnot\b|\bnever\b|\bdon't\b|\bdont\b|"
        r"\bnahi\b|\bnahin\b|नहीं|मत)",
        re.IGNORECASE,
    )
    for start, end, ordinal in mentions:
        before = normalized[max(0, start - 48) : start]
        after = normalized[end : min(len(normalized), end + 48)]
        if before_denial.search(before) or after_denial.search(after):
            denied.add(ordinal)
    return denied


def explicit_proposal_ordinal(
    user_text: str,
    active_proposals: list[dict[str, Any]],
) -> int | None:
    # Resolve only explicit list references; semantic binding stays with the extractor.
    normalized = " ".join(user_text.lower().split())
    if not normalized:
        return None
    denied_ordinals = structured_denied_proposal_ordinals(
        normalized,
        active_proposals,
    )
    reference_nouns = (
        "choice|entry|one|option|proposal|suggestion|wala|wale|wali"
    )
    for word, ordinal in _SELECTION_ORDINAL_ALIASES.items():
        if any(ord(character) > 127 for character in word):
            continue
        pattern = (
            rf"\b(?:the\s+{re.escape(word)}|"
            rf"{re.escape(word)}\s+(?:{reference_nouns}))\b"
        )
        if ordinal not in denied_ordinals and re.search(pattern, normalized):
            return ordinal
    hindi_reference_nouns = "को|वाला|वाले|वाली|प्रस्ताव|विकल्प|प्रविष्टि|सुझाव"
    for word, ordinal in _SELECTION_ORDINAL_ALIASES.items():
        if not any(ord(character) > 127 for character in word):
            continue
        if ordinal not in denied_ordinals and re.search(
            rf"{re.escape(word)}\s+(?:{hindi_reference_nouns})",
            normalized,
        ):
            return ordinal
    if 1 not in denied_ordinals and re.search(r"सबसे\s+पहले", normalized):
        return 1
    cardinal_pattern = re.search(
        r"\b(?:choice|entry|number|option|proposal|suggestion)\s+"
        r"(one|two|three|four|five|six|seven|eight|nine|ten)\b",
        normalized,
    )
    if cardinal_pattern:
        ordinal = _CARDINAL_ORDINAL_WORDS[cardinal_pattern.group(1)]
        if ordinal not in denied_ordinals:
            return ordinal
    numeric = re.search(
        r"(?:\b(?:option|proposal|choice)(?:\s+number)?|\bnumber|"
        r"(?:विकल्प|प्रस्ताव)(?:\s+संख्या)?)\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        normalized,
    )
    if numeric:
        ordinal = int(numeric.group(1))
        if ordinal not in denied_ordinals:
            return ordinal
    if re.search(
        r"(?:\b(?:the\s+(?:last|latter)|last\s+(?:one|option|proposal|choice|wala))\b|अंतिम\s+(?:वाला|वाले|वाली))",
        normalized,
    ):
        ordinals = [
            int(item["ordinal"])
            for item in active_proposals
            if item.get("ordinal") is not None
        ]
        ordinal = max(ordinals) if ordinals else None
        if ordinal not in denied_ordinals:
            return ordinal
    if 1 not in denied_ordinals and re.search(
        r"\b(?:the\s+former|former\s+(?:one|option|proposal|choice|suggestion))\b",
        normalized,
    ):
        return 1
    return None


def _explicit_proposal_ordinal(
    user_text: str,
    active_proposals: list[dict[str, Any]],
) -> int | None:
    # Resolve only explicit list references; semantic binding stays with the extractor.
    normalized = " ".join(user_text.lower().split())
    if not normalized:
        return None
    denied_ordinals = _explicitly_denied_proposal_ordinals(
        normalized,
        active_proposals,
    )
    for word, ordinal in _ORDINAL_WORDS.items():
        if ordinal not in denied_ordinals and re.search(
            rf"\b(?:the\s+{word}|{word}\s+(?:one|option|proposal|choice))\b",
            normalized,
        ):
            return ordinal
    multilingual_ordinals = (
        (
            (
                r"\b(?:pehla|pahla|pehli|pehle)\s+"
                r"(?:wala|one|option|proposal|choice|suggestion)\b"
            ),
            1,
        ),
        (
            (
                r"\b(?:doosra|dusra|doosri|dusri|doosre|dusre)\s+"
                r"(?:wala|one|option|proposal|choice|suggestion)\b"
            ),
            2,
        ),
        (r"(?:पहले|पहला|पहली)\s+(?:वाला|वाले|वाली|प्रस्ताव|विकल्प)", 1),
        (r"(?:दूसरे|दूसरा|दूसरी)\s+(?:वाला|वाले|वाली|प्रस्ताव|विकल्प)", 2),
    )
    for pattern, ordinal in multilingual_ordinals:
        if ordinal not in denied_ordinals and re.search(pattern, normalized):
            return ordinal
    numeric = re.search(
        r"(?:\b(?:option|proposal|choice)(?:\s+number)?|\bnumber|"
        r"(?:विकल्प|प्रस्ताव)(?:\s+संख्या)?)\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        normalized,
    )
    if numeric:
        ordinal = int(numeric.group(1))
        if ordinal not in denied_ordinals:
            return ordinal
    if re.search(
        r"(?:\b(?:the\s+(?:last|latter)|last\s+(?:one|option|proposal|choice|wala))\b|अंतिम\s+(?:वाला|वाले|वाली))",
        normalized,
    ):
        ordinals = [
            int(item["ordinal"])
            for item in active_proposals
            if item.get("ordinal") is not None
        ]
        ordinal = max(ordinals) if ordinals else None
        if ordinal not in denied_ordinals:
            return ordinal
    if 1 not in denied_ordinals and re.search(
        r"\b(?:the\s+former|former\s+(?:one|option|proposal|choice|suggestion))\b",
        normalized,
    ):
        return 1
    return None


def _canonical_role(message: dict[str, Any]) -> str:
    return str(message.get("role") or "").strip().lower()


def _source_kind(message: dict[str, Any]) -> str:
    explicit = str(message.get("source_kind") or "").strip().lower()
    if explicit:
        return explicit
    return "direct_user_input" if _canonical_role(message) == "user" else "assistant_output"


__all__ = [
    "EvidenceAuthority",
    "EvidenceDecision",
    "authority_for_submission",
    "explicit_proposal_ordinal",
    "has_explicit_proposal_denial",
    "has_unambiguous_memory_denial",
    "normalized_selection_tokens",
    "proposal_selection_ordinal",
    "structured_proposal_denied",
    "validate_conversational_evidence",
]
