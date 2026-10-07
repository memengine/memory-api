"""Candidate structure/binding tests. Mocks are not semantic quality evidence."""
import ast
from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from api.services.preference_intent_admission_service import PreferenceAdmissionUnavailable
from api.services.preference_intent_contract import PreferenceIntentReply, candidate_generation_schema_v2
from api.services.runtime_preference_worker import (
    INTENT_PROMPT, INTENT_PROMPT_V2, RuntimePreferenceWorker, classify_intents,
)
from tests.unit.test_runtime_preference_classifier import Model, record
from tests.unit.test_preference_intent_contract import intent, offered, reply


def test_generation_branches_are_valid_closed_and_preserve_canonical_schema():
    canonical = PreferenceIntentReply.model_json_schema()
    schema = candidate_generation_schema_v2()
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and "anyOf" not in schema
    assert schema["$defs"]["NonPreferenceFinding"] == canonical["$defs"]["NonPreferenceFinding"]
    assert schema["properties"] == canonical["properties"]
    expected = {"action", "property_key", "scope_ref", "value", "turn_index", "quote"}
    for branch in schema["$defs"]["PreferenceIntent"]["anyOf"]:
        assert branch["additionalProperties"] is False and set(branch["required"]) == expected
        assert set(branch["properties"]) == expected
        assert branch["properties"]["turn_index"] == canonical["$defs"]["PreferenceIntent"]["properties"]["turn_index"]
        assert branch["properties"]["quote"] == canonical["$defs"]["PreferenceIntent"]["properties"]["quote"]
    actions = {action for branch in schema["$defs"]["PreferenceIntent"]["anyOf"]
               for action in branch["properties"]["action"]["enum"]}
    assert actions == set(canonical["$defs"]["PreferenceIntent"]["properties"]["action"]["enum"])
    assert Draft202012Validator(schema).is_valid(reply([]))
    assert PreferenceIntentReply.model_json_schema() == canonical
    schema["$defs"]["PreferenceIntent"]["anyOf"][0]["properties"]["value"]["maxLength"] = 999
    assert candidate_generation_schema_v2() != schema  # Fresh objects, not shared mutable cache.


@pytest.mark.parametrize("action,value,identity,valid", [
    (action, value, "known", valid) for action in ("assert", "set_current")
    for value, valid in (("Python", True), (None, False))
] + [(action, value, "known", valid) for action in ("withhold", "reaffirm", "abstain")
     for value, valid in ((None, True), ("Python", False))]
  + [(action, value, "known", True) for action in ("tentative", "contextual") for value in (None, "Python")]
  + [("abstain", None, "unknown", True), ("abstain", None, "half-known", False),
     ("reaffirm", None, "unknown", False), ("set_current", "Python", "unknown", False)])
def test_provider_schema_encodes_action_value_and_identity_constraints(action, value, identity, valid):
    finding = intent(action=action, value=value)
    if identity != "known":
        finding["property_key"] = None
        finding["scope_ref"] = "global" if identity == "half-known" else None
    validator = Draft202012Validator(candidate_generation_schema_v2())
    assert validator.is_valid(reply([finding])) is valid


@pytest.mark.parametrize("extra", [{"authority": 100}, {"target_memory_id": "forged"}])
def test_provider_schema_has_no_authority_or_target_override(extra):
    validator = Draft202012Validator(candidate_generation_schema_v2())
    assert not validator.is_valid(reply([intent(**extra)]))
    assert not validator.is_valid({**reply(), **extra})


@pytest.mark.parametrize("version", [1, 2])
def test_empty_recall_reply_uses_one_call_and_same_backend_parser(version):
    model = Model(json.dumps(reply([])))
    result = classify_intents(record(), [{"role": "user", "content": "What language is stored?"}],
        model, contract_version=version)
    assert not result.preferences and not result.nonpreferences and len(model.calls) == 1
    call = model.calls[0]
    assert call["system_prompt"] == (INTENT_PROMPT if version == 1 else INTENT_PROMPT_V2)
    assert call["response_format"].name == f"memory_preference_intent_v{version}"
    assert call["response_format"].strict and call["max_tokens"] == 8192
    assert call["response_format"].schema == (PreferenceIntentReply.model_json_schema()
        if version == 1 else candidate_generation_schema_v2())
    assert PreferenceIntentReply.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("raw", [
    "not json", json.dumps({**reply(), "authority": 100}),
    json.dumps(reply([intent(action="reaffirm", value="C++", quote="Which language is stored?")])),
    json.dumps(reply([intent(action="reaffirm", value=None, quote="Which language is stored?")])),
])
def test_invalid_v2_never_becomes_empty_no_change_or_a_repair_call(raw):
    model = Model(raw)
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_response_invalid"):
        classify_intents(record(), [{"role": "user", "content": "Which language is stored?"}],
            model, contract_version=2)
    assert len(model.calls) == 1


@pytest.mark.parametrize("role,kind", [("assistant", "assistant_output"), ("user", "tool_output"),
    ("user", "fetched_document"), ("tool", "tool_output")])
def test_v2_still_rejects_untrusted_role_or_source_evidence(role, kind):
    model = Model(json.dumps(reply()))
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_response_invalid"):
        classify_intents(record(), [{"role": role, "source_kind": kind, "content": intent()["quote"]}],
            model, contract_version=2)
    assert len(model.calls) == 1


def test_v2_keeps_mixed_update_while_not_fabricating_recall_findings():
    text = "Which language was stored? Use Python instead of C++, and my exam moved to October 18."
    raw = reply(nonpreferences=[{"content": "my exam moved to October 18", "category": "fact",
        "turn_index": 0, "quote": "my exam moved to October 18"}])
    parsed = classify_intents(record(), [{"role": "user", "content": text}], Model(json.dumps(raw)), contract_version=2)
    assert parsed.preferences[0].value == "Python" and len(parsed.nonpreferences) == 1


def test_question_shaped_durable_request_can_only_prepare_a_review():
    text = "Could you make Python my default for every programming example?"
    raw = reply([intent(quote=text)])
    assert Draft202012Validator(candidate_generation_schema_v2()).is_valid(raw)
    # V1 still rejects. V2 may only produce a review, never an active declaration.
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_response_invalid"):
        classify_intents(record(), [{"role": "user", "content": text}], Model(json.dumps(raw)))
    result = classify_intents(record(), [{"role": "user", "content": text}], Model(json.dumps(raw)), contract_version=2)
    from api.services.preference_intent_contract import bind_preference_intents
    data = record()
    batch = bind_preference_intents(result, snapshot=data.snapshot, messages=[{"role":"user","content":text}],
        source_conversation_id=data.source_conversation_id, review_first=True)
    assert len(batch.question_reviews) == 1 and not batch.declarations and not batch.choices


@pytest.mark.parametrize("role,kind", [("assistant", "assistant_output"), ("user", "tool_output"),
    ("user", "fetched_document"), ("tool", "tool_output")])
def test_question_review_does_not_repair_untrusted_evidence(role, kind):
    text = "Could you make Python my default for every programming example?"
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_response_invalid"):
        classify_intents(record(), [{"role":role,"source_kind":kind,"content":text}],
            Model(json.dumps(reply([intent(quote=text)]))), contract_version=2)


def test_question_review_cannot_forge_quote_or_known_identity():
    text = "Could you make Python my default for every programming example?"
    for raw, actual in [(reply([intent(quote=text)]), "Please show a C++ example."),
        (reply([intent(quote=text, scope_ref="invented")]), text)]:
        with pytest.raises(PreferenceAdmissionUnavailable, match="intent_response_invalid"):
            classify_intents(record(), [{"role":"user","content":actual}],
                Model(json.dumps(raw)), contract_version=2)


@pytest.mark.parametrize("text", [
    "Could you make Python my default for every programming example?",
    "क्या आगे के हर प्रोग्रामिंग उदाहरण के लिए मेरी डिफ़ॉल्ट भाषा Python कर सकते हो?",
    "Kya aage ke har programming example ke liye Python meri default language kar sakte ho?",
    "Use Python instead of C++ as my default for every programming example.",
    "अब से प्रोग्रामिंग उदाहरणों की मेरी सामान्य भाषा C++ की जगह Python रखो।",
    "Ab se programming examples ke liye C++ ki jagah Python meri usual language rakho.",
])
@pytest.mark.parametrize("action", ["assert", "set_current"])
@pytest.mark.parametrize("absent", [False, True])
def test_all_v2_value_writes_are_review_only_independent_of_language(text, action, absent, monkeypatch):
    from api.services.preference_intent_contract import bind_preference_intents
    # This heuristic must not choose whether V2 can activate a proposed value.
    monkeypatch.setattr("api.services.extraction_service.ExtractionService._is_question_only",
        lambda *_: pytest.fail("V2 value proposal consulted legacy question heuristic"))
    data = record()
    data.snapshot = offered(absent=absent)
    messages = [{"role":"user","source_kind":"client_assertion","content":text}]
    batch = bind_preference_intents(reply([intent(action=action, quote=text)]), snapshot=data.snapshot,
        messages=messages, source_conversation_id=data.source_conversation_id, review_first=True)
    assert not batch.declarations and not batch.choices and len(batch.question_reviews) == 1
    review = batch.question_reviews[0]
    assert review["proposed_value"] == "Python" and review["quote"] == text
    assert review["source_evidence"]["requires_preference_confirmation"] is True
    assert (review["target_memory_id"] is None) is absent


@pytest.mark.parametrize("action", ["assert", "set_current"])
def test_v1_value_write_still_uses_original_declaration_path(action):
    from api.services.preference_intent_contract import bind_preference_intents
    data = record()
    batch = bind_preference_intents(reply([intent(action=action)]), snapshot=data.snapshot,
        messages=[{"role":"user","content":intent()["quote"]}], source_conversation_id=data.source_conversation_id)
    assert len(batch.declarations) == 1 and not batch.question_reviews


def test_frozen_development_inventory_is_representable_without_value_activation():
    from api.services.preference_intent_contract import bind_preference_intents
    path = Path(__file__).resolve().parents[2] / "benchmarks/internal/datasets/governed_memory/development/recall_operations_v2_v1.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    mixed = {
        "en": ("Make Python my usual language for programming examples", "my exam moved to October 18"),
        "hi": ("प्रोग्रामिंग उदाहरणों की मेरी सामान्य भाषा Python रखो", "मेरी परीक्षा अब 18 अक्टूबर को है।"),
        "hinglish": ("Programming examples ki meri usual language Python rakho", "mera exam ab October 18 ko hai."),
    }
    # Controlled diagnostic expectations, not evidence of provider understanding.
    for case in cases:
        prefs, facts = [], []
        if case["family"] not in {"recall", "one_off"}:
            action, quote = case["expected_actions"][0], case["text"]
            if case["family"] == "mixed":
                quote, fact = mixed[case["language"]]
                facts = [{"category":"fact","content":fact,"quote":fact,"turn_index":0}]
            prefs = [intent(action=action, quote=quote, value="Python" if action == "set_current" else None)]
        data = record()
        messages = [{"role":"user","source_kind":"client_assertion","content":case["text"]}]
        model = Model(json.dumps(reply(prefs, facts), ensure_ascii=False))
        parsed = classify_intents(data, messages, model, contract_version=2)
        batch = bind_preference_intents(parsed, snapshot=data.snapshot, messages=messages,
            source_conversation_id=data.source_conversation_id, review_first=True)
        assert not batch.declarations, case["id"]
        assert len(batch.question_reviews) == int(case["family"] in {"correction","mixed","question_shaped_update"}), case["id"]
        assert len(batch.choices) == int(case["family"] in {"reaffirmation","uncertainty"}), case["id"]
        assert len(batch.nonpreferences) == case["expected_nonpreferences"], case["id"]
        assert len(model.calls) == 1


@pytest.mark.parametrize("version", [True, False, 0, 3, "2", None])
def test_unknown_contract_fails_before_provider(version):
    model = Model(json.dumps(reply([])))
    with pytest.raises(ValueError, match="unsupported intent classifier contract"):
        classify_intents(record(), [], model, contract_version=version)
    assert model.calls == []


def test_normal_worker_and_implicit_classifier_continue_using_v1():
    assert inspect.signature(classify_intents).parameters["contract_version"].default == 1
    tree = ast.parse(inspect.getsource(RuntimePreferenceWorker))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "classify_intents"]
    assert len(calls) == 1
    assert inspect.signature(RuntimePreferenceWorker).parameters["contract_version"].default == 1
    source = deepcopy(reply())
    classify_intents(record(), [{"role": "user", "content": intent()["quote"]}], Model(json.dumps(source)))
    assert source == reply()


def test_development_inventory_is_frozen_multilingual_and_not_claimed_as_gold():
    path = Path(__file__).resolve().parents[2] / "benchmarks/internal/datasets/governed_memory/development/recall_operations_v2_v1.json"
    text = path.read_text(encoding="utf-8")  # Normalize checkout CRLF to LF for the frozen digest.
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == "39914b6bb61805bd89e59f6bac8b87df2d7b28c7c99141ab8ee6105f10c77f1f"
    data = json.loads(text)
    assert data["split"] == "development" and "not independent" in data["label_source"]
    cases = data["cases"]
    assert len(cases) == len({c["id"] for c in cases}) == 21
    assert Counter(c["language"] for c in cases) == {"en": 7, "hi": 7, "hinglish": 7}
    assert Counter(c["family"] for c in cases) == {family: 3 for family in (
        "recall", "reaffirmation", "correction", "uncertainty", "mixed", "one_off", "question_shaped_update")}
    assert all(c["text"].strip() and "\ufffd" not in c["text"] for c in cases)
    assert all(not c["empty_reply_allowed"] for c in cases if c["family"] == "question_shaped_update")
