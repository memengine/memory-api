from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from benchmarks.public.longmemeval.adapter import (
    MemoryOSLongMemEvalAdapter,
    RetrievedEvidence,
    evidence_session_ids,
)
from benchmarks.public.longmemeval.context_chunk_selection import (
    expand_selected_chunks,
    select_chunk_hashes,
)
from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    assert_public_dataset_path,
    load_dataset,
    select_smoke_cases,
    select_stratified_smoke_cases,
)
from benchmarks.public.longmemeval.episodic_chunk_selection import (
    _select_cases,
    build_chunks,
)
from benchmarks.public.longmemeval.episodic_hybrid_retrieval import (
    bm25_scores,
    reciprocal_rank_fusion,
)
from benchmarks.public.longmemeval.episodic_query_planner import plan_query
from benchmarks.public.longmemeval.episodic_selection import (
    _evaluate_candidate,
    decompose_comparison_query,
    explicit_month_constraint,
    record_supports_month,
)
from benchmarks.public.longmemeval.episodic_shadow import (
    build_records,
    ranking_metrics,
)
from benchmarks.public.longmemeval.evaluate_saved_evidence import build_contexts
from benchmarks.public.longmemeval.qa import (
    ModelCall,
    OpenAIQAClient,
    answer_prompt,
    judge_prompt,
)
from benchmarks.public.longmemeval.runner import _load_checkpoint, _write_json_atomic


def _case(question_id: str = "q-1") -> dict[str, object]:
    return {
        "question_id": question_id,
        "question_type": "knowledge-update",
        "question": "What is the current value?",
        "answer": "new",
        "question_date": "2023/05/30 (Tue) 21:56",
        "haystack_session_ids": ["old-session", "new-session"],
        "haystack_dates": [
            "2023/05/01 (Mon) 10:00",
            "2023/05/20 (Sat) 10:00",
        ],
        "haystack_sessions": [
            [{"role": "user", "content": "The value is old."}],
            [{"role": "user", "content": "The value is now new.", "has_answer": True}],
        ],
        "answer_session_ids": ["new-session"],
    }


def test_public_contract_loads_and_hashes_without_mutating_labels(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "longmemeval_s_cleaned.json"
    dataset.write_text(json.dumps([_case()]), encoding="utf-8")

    cases, digest = load_dataset(dataset)

    assert len(cases) == 1
    assert len(digest) == 64
    assert cases[0].haystack_sessions[1][0].has_answer is True


def test_episodic_shadow_preserves_session_provenance_and_roles() -> None:
    case = LongMemEvalCase.model_validate(_case())

    records = build_records([case])

    assert len(records) == 2
    assert records[1].session_id == "new-session"
    assert records[1].roles == ("user",)
    assert len(records[1].content_hash) == 64
    assert "has_answer" not in records[1].text


def test_episodic_ranking_metrics_distinguish_recall_and_filler() -> None:
    metrics = ranking_metrics(["noise", "new-session"], ["new-session"])

    assert metrics["precision_at_k"] == 0.5
    assert metrics["recall_at_k"] == 1.0
    assert metrics["mrr"] == 0.5
    assert metrics["irrelevant_filler_results"] == 1


def test_episodic_selection_decomposes_only_explicit_comparisons() -> None:
    question = "Which happened first, the solo Europe trip or the family road trip?"

    assert decompose_comparison_query(question) == [
        question,
        "the solo Europe trip",
        "the family road trip",
    ]
    assert decompose_comparison_query("Where are my shoes?") == ["Where are my shoes?"]


def test_episodic_selection_enforces_explicit_month_evidence() -> None:
    assert explicit_month_constraint("What did I visit in December?") == 12
    assert explicit_month_constraint("What did I visit previously?") is None
    assert record_supports_month("I visited on 12/8.", 12)
    assert record_supports_month("I visited in December.", 12)
    assert not record_supports_month("I visited on 2/8.", 12)


def test_abstention_cases_still_require_gold_evidence_retrieval() -> None:
    answerable = LongMemEvalCase.model_validate(_case("answerable"))
    abstention_payload = _case("cannot-answer_abs")
    abstention = LongMemEvalCase.model_validate(abstention_payload)
    scored = {
        "answerable": [{"session_id": "new-session", "score": 0.5}],
        "cannot-answer_abs": [{"session_id": "new-session", "score": 0.5}],
    }

    result = _evaluate_candidate([answerable, abstention], scored, 0.4)

    assert result["abstention_evidence_recall_at_k"] == 1.0
    assert result["abstention_empty_result_rate"] == 0.0


def test_hybrid_lexical_scoring_and_rank_fusion_are_deterministic() -> None:
    scores = bm25_scores(
        "previous occupation teacher",
        ["I previously worked as a teacher.", "I enjoy hiking on weekends."],
    )
    fused = reciprocal_rank_fusion(["semantic", "shared"], ["lexical", "shared"])

    assert scores[0] > scores[1]
    assert fused[0]["session_id"] == "shared"


def test_query_planner_uses_general_multi_evidence_patterns() -> None:
    actions = plan_query(
        "How many appliances did I purchase, repair, donate, or replace this year?"
    )
    comparison = plan_query("Who adopted a pet first, Maya or Jordan?")
    quantitative = plan_query("What was the total cost of my monitor and keyboard?")

    assert "appliances purchase" in actions
    assert "appliances replace" in actions
    assert "Maya adopted a pet" in comparison
    assert "Jordan adopted a pet" in comparison
    assert "my monitor" in quantitative
    assert "keyboard" in quantitative


def test_saved_evidence_context_uses_provenance_hash_not_gold_labels() -> None:
    case = LongMemEvalCase.model_validate(_case())
    chunks = build_chunks([case], role_aware=True)
    target = next(chunk for chunk in chunks if chunk.session_id == "new-session")
    artifact = {
        "scored_results": {
            "q-1": [
                {
                    "session_id": "new-session",
                    "content_hash": target.content_hash,
                }
            ]
        },
        "lexical_results": {"q-1": []},
        "fused_results": {
            "q-1": [
                {
                    "session_id": "new-session",
                    "semantic_rank": 1,
                    "lexical_rank": None,
                }
            ]
        },
    }

    context = build_contexts([case], artifact)["q-1"]

    assert "The value is now new." in context["text"]
    assert "has_answer" not in context["text"]
    assert context["provenance"][0]["content_hash"] == target.content_hash


def test_context_chunk_selection_policies_are_bounded_and_label_independent() -> None:
    fused = {"session_id": "s-1"}
    semantic = {"s-1": {"content_hash": "semantic"}}
    lexical = {"s-1": {"content_hash": "lexical"}}

    assert select_chunk_hashes(fused, semantic, lexical, "semantic_first") == [
        "semantic"
    ]
    assert select_chunk_hashes(fused, semantic, lexical, "lexical_first") == [
        "lexical"
    ]
    assert select_chunk_hashes(fused, semantic, lexical, "bounded_union") == [
        "semantic",
        "lexical",
    ]


def test_context_neighbor_expansion_is_session_scoped_and_token_bounded() -> None:
    case = LongMemEvalCase.model_validate(_case())
    chunks = build_chunks([case], role_aware=True)
    session_chunks = [chunk for chunk in chunks if chunk.session_id == "new-session"]
    selected = [
        {
            "session_id": "new-session",
            "content_hash": session_chunks[0].content_hash,
        }
    ]
    by_session = {("q-1", "new-session"): session_chunks}

    expanded, truncated = expand_selected_chunks(
        selected,
        by_session,
        "q-1",
        radius=1,
        token_cap=10_000,
        allocation="round_robin",
    )

    assert expanded[0].session_id == "new-session"
    assert len({chunk.content_hash for chunk in expanded}) == len(expanded)
    assert truncated is False


def test_episodic_chunks_preserve_turn_and_session_provenance() -> None:
    payload = _case()
    payload["haystack_sessions"][1] = [
        {"role": "user", "content": "first observation"},
        {"role": "assistant", "content": "second observation"},
    ]
    chunks = build_chunks([LongMemEvalCase.model_validate(payload)])
    new_session = [chunk for chunk in chunks if chunk.session_id == "new-session"]

    assert len(new_session) == 1
    assert new_session[0].turn_start == 0
    assert new_session[0].turn_end == 1
    assert new_session[0].roles == ("assistant", "user")
    assert len(new_session[0].content_hash) == 64


def test_role_aware_episodic_chunks_never_mix_speakers() -> None:
    payload = _case()
    payload["haystack_sessions"][1] = [
        {"role": "user", "content": "first observation"},
        {"role": "assistant", "content": "second observation"},
        {"role": "user", "content": "third observation"},
    ]

    chunks = build_chunks(
        [LongMemEvalCase.model_validate(payload)], role_aware=True
    )
    new_session = [chunk for chunk in chunks if chunk.session_id == "new-session"]

    assert len(new_session) == 3
    assert [chunk.roles for chunk in new_session] == [
        ("user",),
        ("assistant",),
        ("user",),
    ]


def test_generalization_manifest_rejects_pilot_overlap(tmp_path: Path) -> None:
    cases = [LongMemEvalCase.model_validate(_case("58bf7951"))]
    manifest = tmp_path / "sample.json"
    manifest.write_text(
        json.dumps(
            {
                "classification": "public-development-generalization",
                "dataset_sha256": "digest",
                "question_ids": ["58bf7951", *[f"q-{index}" for index in range(29)]],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="overlaps the six-case pilot"):
        _select_cases(cases, "digest", manifest)


def test_public_contract_accepts_upstream_blank_turn_drift() -> None:
    payload = _case()
    payload["haystack_sessions"][0].append({"role": "assistant", "content": ""})

    case = LongMemEvalCase.model_validate(payload)

    assert case.haystack_sessions[0][-1].content == ""


def test_public_runner_rejects_internal_and_holdout_paths(tmp_path: Path) -> None:
    internal = tmp_path / "benchmarks" / "internal" / "data.json"
    holdout = tmp_path / "external" / "holdout" / "data.json"

    with pytest.raises(ValueError, match="cannot load internal or holdout"):
        assert_public_dataset_path(internal)
    with pytest.raises(ValueError, match="cannot load internal or holdout"):
        assert_public_dataset_path(holdout)


def test_smoke_selection_depends_only_on_question_id() -> None:
    first = [LongMemEvalCase.model_validate(_case(f"q-{index}")) for index in range(20)]
    second_payloads = [_case(f"q-{index}") for index in range(20)]
    for payload in second_payloads:
        payload["answer"] = "changed gold label"
    second = [LongMemEvalCase.model_validate(payload) for payload in second_payloads]

    assert [c.question_id for c in select_smoke_cases(first, count=10)] == [
        c.question_id for c in select_smoke_cases(second, count=10)
    ]


def test_stratified_smoke_covers_every_type_and_one_abstention() -> None:
    question_types = [
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "temporal-reasoning",
        "knowledge-update",
        "multi-session",
    ]
    cases = []
    for index, question_type in enumerate(question_types):
        payload = _case(f"q-{index}")
        payload["question_type"] = question_type
        cases.append(LongMemEvalCase.model_validate(payload))
    abstention = _case("q-abstention_abs")
    abstention["question_type"] = "multi-session"
    abstention["answer_session_ids"] = []
    cases.append(LongMemEvalCase.model_validate(abstention))

    selected = select_stratified_smoke_cases(cases)

    assert {case.question_type for case in selected} == set(question_types)
    assert sum(case.question_id.endswith("_abs") for case in selected) == 1


def test_retrieval_metrics_use_preserved_public_session_provenance() -> None:
    case = LongMemEvalCase.model_validate(_case())
    evidence = [
        RetrievedEvidence(
            memory_id="memory-1",
            content="The value is now new.",
            relevance_score=0.9,
            source_event_id="opaque-event",
            provenance={"scope": {"benchmark_session_id": "new-session"}},
        )
    ]

    assert evidence_session_ids(case, evidence) == ["new-session"]


@pytest.mark.asyncio
async def test_adapter_uses_tenant_api_key_authentication_scheme() -> None:
    adapter = MemoryOSLongMemEvalAdapter(
        base_url="http://localhost:8000",
        api_key="local-benchmark-key",
        run_id="contract-test",
    )
    try:
        assert adapter.client.headers["Authorization"] == "ApiKey local-benchmark-key"
    finally:
        await adapter.client.aclose()


def test_first_public_smoke_payload_matches_memoryos_request_contract() -> None:
    from api.schemas.requests import MemoryAddRequest

    payload = _case()
    case = LongMemEvalCase.model_validate(payload)
    turns = [
        {"role": turn.role, "content": turn.content.strip()}
        for turn in case.haystack_sessions[0]
        if turn.content.strip()
    ]

    request = MemoryAddRequest.model_validate(
        {
            "external_user_id": "longmemeval-contract-q-1",
            "messages": turns,
            "metadata": {"public_benchmark": "longmemeval"},
            "source": {
                "event_id": "lme-contract-q-1-session-1",
                "service": "longmemeval",
                "observed_at": "2023-05-01T10:00:00Z",
                "scope": {"benchmark_session_id": "old-session"},
                "evidence": [
                    {
                        "source_type": "longmemeval-session",
                        "reference": "old-session",
                        "content_hash": "0" * 64,
                    }
                ],
            },
        }
    )

    assert request.source is not None
    assert request.source.service == "longmemeval"


@pytest.mark.asyncio
async def test_failed_job_diagnostic_preserves_worker_boundary() -> None:
    adapter = MemoryOSLongMemEvalAdapter(
        base_url="http://localhost:8000",
        api_key="local-benchmark-key",
        run_id="contract-test",
    )
    adapter.client.post = AsyncMock(
        return_value=type(
            "Response",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"job_id": "job-1"},
            },
        )()
    )
    adapter._wait_for_job = AsyncMock(
        return_value={
            "status": "failed",
            "attempts": 2,
            "error": "provider unavailable",
            "error_summary": "upstream failure",
            "queue_name": "starter-extraction",
        }
    )
    case = LongMemEvalCase.model_validate(_case())
    try:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await adapter.ingest_case(case)
    finally:
        await adapter.client.aclose()


@pytest.mark.asyncio
async def test_failed_job_state_waits_for_scheduled_retry() -> None:
    adapter = MemoryOSLongMemEvalAdapter(
        base_url="http://localhost:8000",
        api_key="local-benchmark-key",
        run_id="contract-test",
        poll_interval_seconds=0,
    )
    adapter.client.get = AsyncMock(
        side_effect=[
            type(
                "Response",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {"data": {"status": "failed", "attempts": 1}},
                },
            )(),
            type(
                "Response",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {
                        "data": {"status": "completed", "attempts": 1}
                    },
                },
            )(),
        ]
    )
    try:
        job = await adapter._wait_for_job("job-1")
        assert job["status"] == "completed"
        assert adapter.client.get.await_count == 2
    finally:
        await adapter.client.aclose()


@pytest.mark.asyncio
async def test_retrieval_retries_only_explicit_embedding_unavailable_response(
    monkeypatch,
) -> None:
    adapter = MemoryOSLongMemEvalAdapter(
        base_url="http://localhost:8000",
        api_key="local-benchmark-key",
        run_id="contract-test",
    )
    request = httpx.Request("POST", "http://localhost:8000/v1/memories/retrieve")
    unavailable = httpx.Response(
        503,
        request=request,
        json={"code": "EMB_503", "error": "embedding_unavailable"},
    )
    success = httpx.Response(
        200,
        request=request,
        json={
            "data": [],
            "system_prompt_addition": "",
            "context_token_count": 0,
        },
    )
    adapter.client.post = AsyncMock(side_effect=[unavailable, success])
    sleep = AsyncMock()
    monkeypatch.setattr("benchmarks.public.longmemeval.adapter.asyncio.sleep", sleep)
    try:
        await adapter.retrieve(
            LongMemEvalCase.model_validate(_case()), limit=10, context_max_tokens=2000
        )
        assert adapter.client.post.await_count == 2
        sleep.assert_awaited_once_with(1)
    finally:
        await adapter.client.aclose()


def test_checkpoint_is_atomic_and_rejects_different_run(tmp_path: Path) -> None:
    path = tmp_path / "run.checkpoint.json"
    checkpoint = {
        "schema_version": "longmemeval-checkpoint-v1",
        "run_id": "run-1",
        "dataset_sha256": "a" * 64,
        "cases": {"q-1": {"sessions": {"s-1": {"job_id": "job-1"}}}},
    }
    _write_json_atomic(path, checkpoint)

    assert _load_checkpoint(path, run_id="run-1", dataset_sha256="a" * 64) == checkpoint
    with pytest.raises(ValueError, match="run_id"):
        _load_checkpoint(path, run_id="run-2", dataset_sha256="a" * 64)


def test_answer_prompt_never_contains_gold_answer_or_evidence_labels() -> None:
    case = LongMemEvalCase.model_validate(_case())

    prompt = answer_prompt(case, "The current value is available in memory.")

    assert "Reference answer" not in prompt
    assert "has_answer" not in prompt
    assert "new-session" not in prompt


def test_preview_judge_preserves_task_specific_contracts() -> None:
    temporal = _case()
    temporal["question_type"] = "temporal-reasoning"
    update = LongMemEvalCase.model_validate(_case())

    assert "one-unit error" in judge_prompt(
        LongMemEvalCase.model_validate(temporal), "18 days"
    )
    assert "updated reference value" in judge_prompt(update, "new")


@pytest.mark.asyncio
async def test_preview_judge_accepts_binary_label_with_punctuation() -> None:
    client = OpenAIQAClient(api_key="test-key")
    client._complete = AsyncMock(
        return_value=ModelCall(
            text="No.",
            model="judge-model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
            estimated_cost_usd=0.0,
        )
    )
    try:
        outcome = await client.judge(
            LongMemEvalCase.model_validate(_case()), "wrong", model="judge-model"
        )
        assert outcome.correct is False
        assert outcome.raw_label == "no"
    finally:
        await client.client.aclose()


@pytest.mark.asyncio
async def test_answer_client_retries_connection_establishment_failures() -> None:
    client = OpenAIQAClient(api_key="test-key")
    response = type(
        "Response",
        (),
        {
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "model": "answer-model",
                "choices": [{"message": {"content": "new"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        },
    )()
    client.client.post = AsyncMock(
        side_effect=[httpx.ConnectError("temporary"), response]
    )
    try:
        call = await client.answer(
            LongMemEvalCase.model_validate(_case()), "context", model="answer-model"
        )
        assert call.text == "new"
        assert client.client.post.await_count == 2
    finally:
        await client.client.aclose()


@pytest.mark.asyncio
async def test_answer_client_honors_retryable_provider_response(monkeypatch) -> None:
    client = OpenAIQAClient(api_key="test-key")
    retry = httpx.Response(
        429,
        headers={"retry-after": "0.25"},
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )
    success = httpx.Response(
        200,
        json={
            "model": "answer-model",
            "choices": [{"message": {"content": "new"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )
    client.client.post = AsyncMock(side_effect=[retry, success])
    sleep = AsyncMock()
    monkeypatch.setattr("benchmarks.public.longmemeval.qa.asyncio.sleep", sleep)
    try:
        call = await client.answer(
            LongMemEvalCase.model_validate(_case()), "context", model="answer-model"
        )
        assert call.text == "new"
        sleep.assert_awaited_once_with(0.25)
    finally:
        await client.client.aclose()
