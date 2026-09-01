from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from typing import Any


PROVIDER_NAME = "benchmark-deterministic"


def benchmark_provider_enabled() -> bool:
    requested = os.getenv("MEMORYOS_BENCHMARK_PROVIDER", "").strip() == "deterministic"
    if not requested:
        return False
    if os.getenv("MEMORYOS_SCALE_DEDICATED") != "1":
        raise RuntimeError("Deterministic benchmark provider requires MEMORYOS_SCALE_DEDICATED=1.")
    if os.getenv("APP_ENV", "").strip().lower() != "benchmark":
        raise RuntimeError("Deterministic benchmark provider requires APP_ENV=benchmark.")
    return True


def deterministic_embedding(text: str, dimensions: int) -> list[float]:
    if not benchmark_provider_enabled():
        raise RuntimeError("Deterministic embedding requested outside the benchmark environment.")
    latency_ms = max(0, int(os.getenv("BENCHMARK_EMBED_LATENCY_MS", "5")))
    if latency_ms:
        time.sleep(latency_ms / 1000)
    vector = [0.0] * dimensions
    tokens = re.findall(r"[a-z0-9]+", text.lower()) or ["empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def deterministic_completion(system_prompt: str, user_message: str) -> dict[str, Any]:
    if not benchmark_provider_enabled():
        raise RuntimeError("Deterministic completion requested outside the benchmark environment.")
    if "pass 1 in a two-pass" in system_prompt.lower():
        content = {"entities": [], "relationships": [], "notes": "benchmark deterministic pass"}
    elif "conflict resolution engine" in system_prompt.lower():
        normalized = user_message.lower()
        action = "UPDATE" if "correction" in normalized or "replacing" in normalized else "KEEP_BOTH"
        content = {"action": action, "reasoning": "Deterministic benchmark conflict decision."}
    else:
        content = _extraction_payload(user_message)
    raw = json.dumps(content, separators=(",", ":"))
    input_tokens = max(1, (len(system_prompt) + len(user_message)) // 4)
    output_tokens = max(1, len(raw) // 4)
    return {
        "content": raw,
        "provider_used": PROVIDER_NAME,
        "model_used": "benchmark-fixture-v1",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _extraction_payload(user_message: str) -> dict[str, Any]:
    conversation = user_message.split("\n\nExisting memories for this user", 1)[0]
    turns = re.findall(r"\[user\]:\s*(.+)", conversation, flags=re.IGNORECASE)
    statement = (turns[-1] if turns else conversation.splitlines()[-1] if conversation.splitlines() else "").strip()
    statement = re.sub(r"\s+Observation\s+\d+\.?$", "", statement, flags=re.IGNORECASE)
    if not statement:
        return {"memories": [], "nothing_to_extract": True, "extraction_notes": "empty"}
    lowered = statement.lower()
    if "might " in lowered or "not sure" in lowered or "before making" in lowered:
        confidence = 0.55
    else:
        confidence = 0.92
    if "prefer" in lowered:
        category, importance = "preference", 4.0
    elif "review" in lowered or "every friday" in lowered or "procedure" in lowered:
        category, importance = "procedure", 5.0
    elif "goal" in lowered or "plan to" in lowered:
        category, importance = "goal", 6.0
    elif "manager" in lowered or "relationship" in lowered:
        category, importance = "relationship", 6.0
    elif "expert" in lowered or "experience" in lowered:
        category, importance = "expertise", 7.0
    else:
        category, importance = "fact", 5.0
    content = re.sub(r"^Correction:\s*", "", statement, flags=re.IGNORECASE).strip()
    return {
        "memories": [{
            "content": content,
            "category": category,
            "importance_score": importance,
            "confidence": confidence,
            "reasoning": "Deterministic benchmark extraction fixture.",
        }],
        "nothing_to_extract": False,
        "extraction_notes": "benchmark deterministic fixture",
    }


__all__ = [
    "PROVIDER_NAME",
    "benchmark_provider_enabled",
    "deterministic_completion",
    "deterministic_embedding",
]
