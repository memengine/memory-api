from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    failure_threshold: int
    window_seconds: int
    recovery_timeout_seconds: int


CIRCUIT_CONFIGS: dict[str, CircuitConfig] = {
    "redis": CircuitConfig(failure_threshold=5, window_seconds=10, recovery_timeout_seconds=30),
    "gemini_embed": CircuitConfig(failure_threshold=10, window_seconds=60, recovery_timeout_seconds=120),
    "gemini_extract": CircuitConfig(failure_threshold=10, window_seconds=60, recovery_timeout_seconds=300),
    "qdrant": CircuitConfig(failure_threshold=3, window_seconds=5, recovery_timeout_seconds=60),
    "postgres": CircuitConfig(failure_threshold=5, window_seconds=10, recovery_timeout_seconds=20),
}
