from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from typing import Any, Literal, Self

import httpx

from benchmarks.public.longmemeval.contract import LongMemEvalCase

OFFICIAL_MODEL = "gpt-4o-2024-08-06"
OPENAI_INPUT_USD_PER_MILLION = 2.50
OPENAI_OUTPUT_USD_PER_MILLION = 10.00
UPSTREAM_EVALUATOR_URL = (
    "https://github.com/xiaowu0162/LongMemEval/blob/main/"
    "src/evaluation/evaluate_qa.py"
)


@dataclass(frozen=True, slots=True)
class ModelCall:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    estimated_cost_usd: float


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    correct: bool
    raw_label: str
    call: ModelCall


def answer_prompt(case: LongMemEvalCase, context: str) -> str:
    # The answerer never receives the reference answer or evidence labels.
    return (
        "Use only the memory context below to answer the user's question. "
        "If the context does not contain enough information, say that the "
        "question cannot be answered from the available memory. Give a concise "
        "answer without discussing retrieval.\n\n"
        f"Memory context:\n{context or '(empty)'}\n\n"
        f"Current date: {case.question_date}\n"
        f"Question: {case.question}\nAnswer:"
    )


def judge_prompt(case: LongMemEvalCase, hypothesis: str) -> str:
    abstention = case.question_id.endswith("_abs")
    if abstention:
        instruction = (
            "Return yes only when the response correctly recognizes that the "
            "question cannot be answered from the available information."
        )
    elif case.question_type == "single-session-preference":
        instruction = (
            "Return yes when the response satisfies the personalization rubric "
            "by correctly using relevant personal information; it need not cover "
            "every rubric point."
        )
    elif case.question_type == "temporal-reasoning":
        instruction = (
            "Return yes when the response is equivalent to the reference answer. "
            "Allow a one-unit error in a calculated duration."
        )
    elif case.question_type == "knowledge-update":
        instruction = (
            "Return yes when the response contains the updated reference value, "
            "even if it also mentions an older value."
        )
    else:
        instruction = (
            "Return yes only when the response contains the complete reference "
            "answer or all information needed to derive it."
        )
    return (
        f"{instruction} Otherwise return no. Answer yes or no only.\n\n"
        f"Question: {case.question}\n"
        f"Reference answer or rubric: {case.answer}\n"
        f"Model response: {hypothesis}\n"
        "Correct?"
    )


def prompt_sha256(kind: Literal["answer", "judge"]) -> str:
    function = answer_prompt if kind == "answer" else judge_prompt
    return hashlib.sha256(inspect.getsource(function).encode()).hexdigest()


class OpenAIQAClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 90.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for answer evaluation")
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    async def answer(self, case: LongMemEvalCase, context: str, *, model: str) -> ModelCall:
        return await self._complete(
            prompt=answer_prompt(case, context), model=model, max_tokens=500
        )

    async def judge(
        self, case: LongMemEvalCase, hypothesis: str, *, model: str
    ) -> JudgeOutcome:
        call = await self._complete(
            prompt=judge_prompt(case, hypothesis), model=model, max_tokens=10
        )
        normalized = call.text.strip().lower().rstrip(".!,:;")
        if normalized not in {"yes", "no"}:
            raise ValueError(f"judge returned a non-binary label: {call.text!r}")
        return JudgeOutcome(correct=normalized == "yes", raw_label=normalized, call=call)

    async def _complete(self, *, prompt: str, model: str, max_tokens: int) -> ModelCall:
        started = time.perf_counter()
        request = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        response: httpx.Response | None = None
        for attempt in range(5):
            try:
                response = await self.client.post("/chat/completions", json=request)
                if getattr(response, "status_code", 200) not in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    break
                if attempt == 4:
                    response.raise_for_status()
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else min(2**attempt, 30)
                await asyncio.sleep(min(max(delay, 0.25), 60.0))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 4:
                    raise
                await asyncio.sleep(min(2**attempt, 30))
        if response is None:
            raise RuntimeError("OpenAI completion did not produce a response")
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        return ModelCall(
            text=str(body["choices"][0]["message"]["content"]).strip(),
            model=str(body.get("model") or model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            estimated_cost_usd=round(
                input_tokens / 1_000_000 * OPENAI_INPUT_USD_PER_MILLION
                + output_tokens / 1_000_000 * OPENAI_OUTPUT_USD_PER_MILLION,
                8,
            ),
        )
