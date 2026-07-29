from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


def resolve_api_key(
    *,
    secret_name_env: str,
    direct_value_env: str,
    json_key_candidates: tuple[str, ...] = ("api_key", "key", "value", "secret"),
) -> str | None:
    """Resolve provider credentials from env or AWS Secrets Manager.

    Local development and ECS usually inject the real key directly into
    ``direct_value_env``. Some deployments expose a Secrets Manager name through
    ``secret_name_env`` instead. Missing credentials return ``None`` so imports
    and test collection do not fail before a provider is actually used.
    """

    direct_value = _clean(os.getenv(direct_value_env))
    if direct_value:
        return direct_value

    secret_name = _clean(os.getenv(secret_name_env))
    if not secret_name:
        return None

    secret_value = _read_aws_secret(secret_name)
    if not secret_value:
        return None

    return _clean(_parse_secret_string(secret_value, json_key_candidates))


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_secret_string(secret_value: str, keys: tuple[str, ...]) -> str | None:
    stripped = secret_value.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        return stripped

    try:
        payload: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped

    if not isinstance(payload, dict):
        return stripped

    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    return stripped


@lru_cache(maxsize=32)
def _read_aws_secret(secret_name: str) -> str | None:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except Exception as exc:  # pragma: no cover - depends on deployment image
        logger.debug("AWS secret resolution unavailable for %s: %s", secret_name, exc)
        return None

    try:
        client = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        )
        response = client.get_secret_value(SecretId=secret_name)
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Unable to resolve AWS secret %s: %s", secret_name, exc.__class__.__name__)
        return None

    secret_string = response.get("SecretString")
    return secret_string if isinstance(secret_string, str) else None
