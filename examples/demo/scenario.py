"""Offline fixture for the two-source walkthrough. Uses only the standard library."""

import json
import os
import re
from collections import Counter
from pathlib import Path

STATE = Path(__file__).with_name(".scenario.json")
QUESTION = "What subscription plan is this account on?"
FALLBACK = "I can't confirm your plan while the correction is under review."


def start():
    if os.getenv("MEMORYOS_DEMO_MOCK") != "1":
        raise SystemExit("Set MEMORYOS_DEMO_MOCK=1 to run the offline fixture.")
    print("[MemoryOS mock mode]")


def extract(statement, event_id):
    match = re.fullmatch(r"(Correction: )?This account has subscription plan (Growth|Scale)\.", statement)
    if not match:
        raise ValueError("The mock extractor accepts the two demo statements.")
    return {
        "content": f"Subscription plan: {match[2]}",
        "source_event_id": event_id,
        "correction": bool(match[1]),
    }


def embed(text):
    """Deterministic sparse token vector for the synthetic plan query."""
    return Counter(re.findall(r"[a-z]+", text.lower()))


def retrieve(state, question):
    query = embed(question)
    record = state["current"]
    score = sum(query[token] * count for token, count in embed(record["content"]).items())
    if state.get("pending"):
        return [], {"state": "unavailable", "scope": "affected_context"}
    return ([record] if score else []), None


def load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("Run billing_says_growth.py first.") from None
    except json.JSONDecodeError:
        raise SystemExit(
            f"{STATE.name} is not valid JSON. Rerun billing_says_growth.py to recreate it."
        ) from None


def save(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


def show(label, record):
    print(f"{label}: {record['content']}")
    print(f"source_event_id: {record['source_event_id']}")
