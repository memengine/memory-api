from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.public.locomo.adapter import (
    MemoryOSLoCoMoAdapter,
    RetrievedMemory,
    candidate_dialog_ids,
)
from benchmarks.public.locomo.contract import (
    LoCoMoSample,
    assert_public_dataset_path,
    dataset_diagnostics,
    load_dataset,
    parse_locomo_datetime,
)
from benchmarks.public.locomo.freeze_pilot import freeze
from benchmarks.public.locomo.runner import load_pilot, preflight


def _sample(sample_id: str, questions_per_category: int = 5) -> dict[str, object]:
    qa = []
    for category in range(1, 6):
        for index in range(questions_per_category):
            item: dict[str, object] = {
                "question": f"category {category} question {index}",
                "category": category,
                "evidence": ["D1:1"],
            }
            if category == 5:
                item["adversarial_answer"] = "unsupported premise"
            else:
                item["answer"] = f"answer {category}-{index}"
            qa.append(item)
    return {
        "sample_id": sample_id,
        "qa": qa,
        "conversation": {
            "speaker_a": "A",
            "speaker_b": "B",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "A", "dia_id": "D1:1", "text": "Remember this."},
                {"speaker": "B", "dia_id": "D1:2", "text": "I will."},
            ],
        },
    }


def test_contract_loads_hashes_and_reports_evidence_diagnostics(tmp_path: Path) -> None:
    dataset = tmp_path / "locomo10.json"
    payload = _sample("conv-1")
    payload["qa"][0]["evidence"] = ["D1:1; D1:99"]
    dataset.write_text(json.dumps([payload]), encoding="utf-8")

    samples, digest = load_dataset(dataset)
    diagnostics = dataset_diagnostics(samples)

    assert len(digest) == 64
    assert diagnostics["sample_count"] == 1
    assert diagnostics["question_count"] == 25
    assert diagnostics["unresolved_evidence_count"] == 1
    assert diagnostics["unresolved_evidence"][0]["evidence_dialog_id"] == "D1:99"


def test_contract_preserves_speakers_sessions_and_dialog_ids() -> None:
    payload = _sample("conv-1")
    payload["conversation"]["session_1"][0]["img_url"] = [
        "https://example.test/image.jpg"
    ]
    sample = LoCoMoSample.model_validate(payload)

    sessions = sample.conversation.sessions()

    assert sessions[0][0] == 1
    assert [turn.speaker for turn in sessions[0][2]] == ["A", "B"]
    assert sample.conversation.dialog_ids() == {"D1:1", "D1:2"}
    assert sample.conversation.sessions()[0][2][0].img_url == [
        "https://example.test/image.jpg"
    ]
    assert sample.question_id(0) == "conv-1:qa-0"


def test_contract_rejects_unknown_speaker_and_missing_adversarial_answer() -> None:
    unknown_speaker = _sample("conv-1")
    unknown_speaker["conversation"]["session_1"][0]["speaker"] = "C"
    with pytest.raises(ValueError, match="unknown speaker"):
        LoCoMoSample.model_validate(unknown_speaker)

    missing_answer = _sample("conv-2")
    del missing_answer["qa"][-1]["adversarial_answer"]
    with pytest.raises(ValueError, match="category 5 requires"):
        LoCoMoSample.model_validate(missing_answer)


def test_public_contract_rejects_internal_and_holdout_paths(tmp_path: Path) -> None:
    internal = tmp_path / "benchmarks" / "internal" / "locomo.json"
    holdout = tmp_path / "external" / "holdout" / "locomo.json"

    with pytest.raises(ValueError, match="cannot load internal or holdout"):
        assert_public_dataset_path(internal)
    with pytest.raises(ValueError, match="cannot load internal or holdout"):
        assert_public_dataset_path(holdout)


def test_frozen_pilot_is_balanced_and_independent_of_answers_and_evidence(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    payloads = [_sample(f"conv-{index}") for index in range(4)]
    first.write_text(json.dumps(payloads), encoding="utf-8")

    changed = json.loads(json.dumps(payloads))
    for sample in changed:
        for question in sample["qa"]:
            question["evidence"] = []
            if question["category"] == 5:
                question["adversarial_answer"] = "changed"
            else:
                question["answer"] = "changed"
    second.write_text(json.dumps(changed), encoding="utf-8")

    first_manifest = freeze(first)
    second_manifest = freeze(second)

    assert first_manifest["sample_ids"] == second_manifest["sample_ids"]
    assert first_manifest["questions"] == second_manifest["questions"]
    assert first_manifest["question_count"] == 25
    assert first_manifest["category_counts"] == {
        "1": 5,
        "2": 5,
        "3": 5,
        "4": 5,
        "5": 5,
    }


def test_locomo_timestamp_is_stable_utc() -> None:
    parsed = parse_locomo_datetime("1:56 pm on 8 May, 2023")

    assert parsed.isoformat() == "2023-05-08T13:56:00+00:00"


@pytest.mark.asyncio
async def test_adapter_payload_preserves_speaker_time_and_candidate_dialog_provenance() -> (
    None
):
    sample = LoCoMoSample.model_validate(_sample("conv-1"))
    adapter = MemoryOSLoCoMoAdapter(
        base_url="https://memory.test",
        api_key="test-key",
        run_id="run-1",
    )
    try:
        payload, event_id = adapter.session_payload(sample, session_number=1)
    finally:
        await adapter.client.aclose()

    assert event_id.startswith("locomo-")
    assert payload["messages"][0] == {
        "role": "user",
        "content": "[A; dialog D1:1] Remember this.",
    }
    assert payload["messages"][1]["role"] == "assistant"
    assert payload["source"]["observed_at"] == "2023-05-08T13:56:00+00:00"
    assert payload["source"]["scope"]["benchmark_dialog_ids"] == ["D1:1", "D1:2"]
    assert "answer" not in json.dumps(payload)
    assert "category" not in json.dumps(payload)


def test_candidate_dialog_ids_are_deduplicated_from_retrieval_provenance() -> None:
    memories = [
        RetrievedMemory(
            "1", "one", 0.9, None, {"scope": {"benchmark_dialog_ids": ["D1:1", "D1:2"]}}
        ),
        RetrievedMemory(
            "2", "two", 0.8, None, {"scope": {"benchmark_dialog_ids": ["D1:2", "D2:1"]}}
        ),
    ]

    assert candidate_dialog_ids(memories) == ["D1:1", "D1:2", "D2:1"]


def test_runner_verifies_frozen_manifest_and_reports_provider_ceiling(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "locomo10.json"
    dataset.write_text(
        json.dumps([_sample("conv-1"), _sample("conv-2")]), encoding="utf-8"
    )
    manifest = freeze(dataset)
    manifest_path = tmp_path / "pilot.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    sample, qa_index, _loaded, digest = load_pilot(dataset, manifest_path)
    report = preflight(sample, qa_index, digest)

    assert report["maximum_extraction_jobs"] == 1
    assert report["answer_model_calls"] == 0
    assert report["judge_calls"] == 0
    assert report["live_provider_calls_required_for_ingestion"] is True


def test_runner_rejects_manifest_for_different_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "locomo10.json"
    dataset.write_text(
        json.dumps([_sample("conv-1"), _sample("conv-2")]), encoding="utf-8"
    )
    manifest = freeze(dataset)
    manifest["dataset_sha256"] = "0" * 64
    manifest_path = tmp_path / "pilot.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        load_pilot(dataset, manifest_path)
