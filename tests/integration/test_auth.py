from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import fakeredis.aioredis
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient
from jose import jwt
from jose.utils import base64url_encode

from api.db.models import ApiKey
from api.middleware.auth import AuthMiddleware
from api.utils.crypto import fingerprint_api_key
from api.utils.crypto import hash_api_key


class FakeExecuteResult:
    def __init__(self, items) -> None:
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        if not self._items:
            return None
        return self._items[0]


@dataclass
class FakeSession:
    api_keys: list[ApiKey]
    clerk_org_to_tenant_id: dict[str, uuid.UUID] | None = None
    execute_calls: int = 0
    commit_calls: int = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, query):
        self.execute_calls += 1
        query_text = str(query)
        if "FROM tenants" in query_text:
            org_id = None
            for criterion in getattr(query, "_where_criteria", ()):
                right = getattr(criterion, "right", None)
                value = getattr(right, "value", None)
                if value is not None:
                    org_id = str(value)
                    break
            tenant_id = (self.clerk_org_to_tenant_id or {}).get(org_id or "")
            return FakeExecuteResult([tenant_id] if tenant_id is not None else [])
        return FakeExecuteResult(self.api_keys)

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeSessionFactory:
    def __init__(
        self,
        api_keys: list[ApiKey],
        *,
        clerk_org_to_tenant_id: dict[str, uuid.UUID] | None = None,
    ) -> None:
        self.session = FakeSession(
            api_keys=api_keys,
            clerk_org_to_tenant_id=clerk_org_to_tenant_id,
        )

    def __call__(self) -> FakeSession:
        return self.session


def make_api_key(*, raw_key: str, user_id: uuid.UUID | None = None) -> ApiKey:
    return ApiKey(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        key_hash=hash_api_key(raw_key),
        name="SDK key",
        permissions=["read", "write"],
        rate_limit_per_minute=60,
        created_at=datetime.now(UTC),
        last_used_at=None,
        is_active=True,
    )


def build_rsa_jwks() -> tuple[str, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()

    def encode_number(value: int) -> str:
        byte_length = max(1, (value.bit_length() + 7) // 8)
        return base64url_encode(value.to_bytes(byte_length, "big")).decode("utf-8")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-id",
                "use": "sig",
                "alg": "RS256",
                "n": encode_number(public_numbers.n),
                "e": encode_number(public_numbers.e),
            }
        ]
    }
    return private_pem.decode("utf-8"), jwks


def build_test_app(
    *,
    session_factory: FakeSessionFactory,
    redis_client,
    http_client: httpx.AsyncClient | None = None,
    clerk_issuer: str = "https://clerk.example.test",
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        AuthMiddleware,
        session_factory=session_factory,
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=clerk_issuer,
        clerk_jwks_url=f"{clerk_issuer}/.well-known/jwks.json",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/webhooks/test")
    async def webhook() -> dict[str, str]:
        return {"status": "accepted"}

    @app.get("/private")
    async def private(request: Request) -> dict[str, str | None]:
        return {
            "user_id": getattr(request.state, "user_id", None),
            "tenant_id": getattr(request.state, "tenant_id", None),
            "auth_scheme": getattr(request.state, "auth_scheme", None),
        }

    return app


def test_public_endpoints_skip_auth() -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/webhooks/test").status_code == 200


def test_bearer_token_is_verified_against_clerk_jwks() -> None:
    private_key, jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    token = jwt.encode(
        {
            "sub": "user_clerk_123",
            "iss": issuer,
            "exp": int(time.time()) + 3600,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/jwks.json"
        return httpx.Response(200, json=jwks)

    transport = httpx.MockTransport(jwks_handler)
    http_client = httpx.AsyncClient(transport=transport)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_clerk_123",
        "tenant_id": None,
        "auth_scheme": "bearer",
    }


def test_bearer_token_uses_tenant_id_claim_when_present() -> None:
    private_key, jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    tenant_id = str(uuid.uuid4())
    token = jwt.encode(
        {
            "sub": "user_clerk_456",
            "iss": issuer,
            "exp": int(time.time()) + 3600,
            "tenant_id": tenant_id,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    transport = httpx.MockTransport(jwks_handler)
    http_client = httpx.AsyncClient(transport=transport)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_clerk_456",
        "tenant_id": tenant_id,
        "auth_scheme": "bearer",
    }


def test_bearer_token_maps_clerk_org_id_to_tenant() -> None:
    private_key, jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    tenant_id = uuid.uuid4()
    org_id = "org_2abc123"
    token = jwt.encode(
        {
            "sub": "user_clerk_789",
            "iss": issuer,
            "exp": int(time.time()) + 3600,
            "org_id": org_id,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    transport = httpx.MockTransport(jwks_handler)
    http_client = httpx.AsyncClient(transport=transport)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(
            api_keys=[],
            clerk_org_to_tenant_id={org_id: tenant_id},
        ),
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_clerk_789",
        "tenant_id": str(tenant_id),
        "auth_scheme": "bearer",
    }


def test_bearer_token_prefers_active_clerk_org_over_tenant_claim() -> None:
    private_key, jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    tenant_from_org = uuid.uuid4()
    stale_tenant_claim = uuid.uuid4()
    org_id = "org_active_workspace"
    token = jwt.encode(
        {
            "sub": "user_clerk_999",
            "iss": issuer,
            "exp": int(time.time()) + 3600,
            "org_id": org_id,
            "tenant_id": str(stale_tenant_claim),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    app = build_test_app(
        session_factory=FakeSessionFactory(
            api_keys=[],
            clerk_org_to_tenant_id={org_id: tenant_from_org},
        ),
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(jwks_handler)),
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(tenant_from_org)


def test_invalid_jwt_is_rejected_with_401() -> None:
    private_key, jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    token = jwt.encode(
        {
            "sub": "user_clerk_123",
            "iss": "https://wrong-issuer.example.test",
            "exp": int(time.time()) + 3600,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    transport = httpx.MockTransport(jwks_handler)
    http_client = httpx.AsyncClient(transport=transport)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_001"


def test_tampered_jwt_with_wrong_signature_is_rejected() -> None:
    private_key, jwks = build_rsa_jwks()
    tampered_private_key, _tampered_jwks = build_rsa_jwks()
    issuer = "https://clerk.example.test"
    token = jwt.encode(
        {
            "sub": "user_clerk_123",
            "iss": issuer,
            "exp": int(time.time()) + 3600,
        },
        tampered_private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    transport = httpx.MockTransport(jwks_handler)
    http_client = httpx.AsyncClient(transport=transport)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
        http_client=http_client,
        clerk_issuer=issuer,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_001"


def test_api_key_authentication_uses_cache_after_first_lookup(monkeypatch) -> None:
    raw_api_key = "sdk_secret_key"
    api_key = make_api_key(raw_key=raw_api_key, user_id=uuid.uuid4())
    from api.middleware import auth as auth_module

    bcrypt_verifications = 0
    real_verify_api_key = auth_module.verify_api_key

    def counting_verify_api_key(candidate: str, key_hash: str) -> bool:
        nonlocal bcrypt_verifications
        bcrypt_verifications += 1
        return real_verify_api_key(candidate, key_hash)

    monkeypatch.setattr(auth_module, "verify_api_key", counting_verify_api_key)
    session_factory = FakeSessionFactory(api_keys=[api_key])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=session_factory,
        redis_client=redis_client,
    )

    with TestClient(app) as client:
        first_response = client.get("/private", headers={"Authorization": f"ApiKey {raw_api_key}"})
        second_response = client.get("/private", headers={"Authorization": f"ApiKey {raw_api_key}"})

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["user_id"] == str(api_key.user_id)
    assert second_response.json()["auth_scheme"] == "apikey"
    assert session_factory.session.execute_calls == 1
    assert bcrypt_verifications == 1


def test_legacy_api_key_cache_entry_is_revalidated_against_database() -> None:
    raw_api_key = "sdk_legacy_cache_key"
    api_key = make_api_key(raw_key=raw_api_key, user_id=uuid.uuid4())
    session_factory = FakeSessionFactory(api_keys=[api_key])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_key = f"apikey:{fingerprint_api_key(raw_api_key)[:16]}:tenant_auth"
    asyncio.run(
        redis_client.set(
            cache_key,
            json.dumps(
                {
                    "user_id": str(api_key.user_id),
                    "api_key_id": str(api_key.id),
                    "key_hash": api_key.key_hash,
                }
            ),
            ex=300,
        )
    )
    app = build_test_app(
        session_factory=session_factory,
        redis_client=redis_client,
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"ApiKey {raw_api_key}"})

    assert response.status_code == 200
    assert session_factory.session.execute_calls == 1


def test_missing_auth_header_is_rejected_with_401() -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
    )

    with TestClient(app) as client:
        response = client.get("/private")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_001"


def test_auth_failure_returns_standard_401_payload_and_logs_attempt_count(caplog) -> None:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(
        session_factory=FakeSessionFactory(api_keys=[]),
        redis_client=redis_client,
    )

    with TestClient(app) as client:
        response = client.get(
            "/private",
            headers={
                "Authorization": "ApiKey invalid-key",
                "x-request-id": "req-auth-123",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": "unauthorized",
        "code": "AUTH_001",
        "request_id": "req-auth-123",
    }

    log_payload = json.loads(caplog.records[-1].message)
    assert log_payload["event"] == "auth_failure"
    assert log_payload["code"] == "AUTH_001"
    assert log_payload["attempt_count"] == 1
    assert "ip_address" in log_payload
