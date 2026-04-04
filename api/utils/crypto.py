from __future__ import annotations

import hashlib

import bcrypt


def hash_api_key(raw_api_key: str) -> str:
    return bcrypt.hashpw(raw_api_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_api_key(raw_api_key: str, key_hash: str) -> bool:
    try:
        return bcrypt.checkpw(raw_api_key.encode("utf-8"), key_hash.encode("utf-8"))
    except ValueError:
        return False


def fingerprint_api_key(raw_api_key: str) -> str:
    return hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()


def api_key_prefix(raw_api_key: str) -> str:
    return raw_api_key[:8]
