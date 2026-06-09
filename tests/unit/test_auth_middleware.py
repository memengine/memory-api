from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import fakeredis.aioredis
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient
from jose import jwt
from jose.utils import base64url_encode

from api.middleware.auth import AuthMiddleware


class FakeExecuteResult:
    def __init__(self, items) -> None:
        self._items = list(items)

    def scalar_one_or_none(self):
        if not self._items:
            return None
        return self._items[0]


@dataclass
class FakeSession:
    clerk_org_to_tenant_id: dict[str, uuid.UUID]
    created_orgs: dict[str, uuid.UUID] | None = None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, query, params=None):
        sql_text = str(query).lower()
        if "insert into tenants" in sql_text:
            org_id = str((params or {}).get("clerk_org_id") or "")
            tenant_id = uuid.uuid4()
            self.clerk_org_to_tenant_id[org_id] = tenant_id
            if self.created_orgs is not None:
                self.created_orgs[org_id] = tenant_id
            return FakeExecuteResult([tenant_id])
        if "insert into tenant_budgets" in sql_text:
            return FakeExecuteResult([])

        org_id = None
        for criterion in getattr(query, "_where_criteria", ()):
            right = getattr(criterion, "right", None)
            value = getattr(right, "value", None)
            if value is not None and str(value).startswith("org_"):
                org_id = str(value)
                break

        tenant_id = self.clerk_org_to_tenant_id.get(org_id or "")
        return FakeExecuteResult([tenant_id] if tenant_id is not None else [])

    async def commit(self) -> None:
        return None

    async def run_sync(self, fn) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, clerk_org_to_tenant_id: dict[str, uuid.UUID] | None = None) -> None:
        self.created_orgs: dict[str, uuid.UUID] = {}
        self.session = FakeSession(
            clerk_org_to_tenant_id=clerk_org_to_tenant_id or {},
            created_orgs=self.created_orgs,
        )

    def __call__(self) -> FakeSession:
        return self.session


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
    http_client: httpx.AsyncClient,
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

    @app.get("/private")
    async def private(request: Request) -> dict[str, str | None]:
        return {
            "user_id": getattr(request.state, "user_id", None),
            "tenant_id": getattr(request.state, "tenant_id", None),
            "auth_scheme": getattr(request.state, "auth_scheme", None),
            "auth_method": getattr(request.state, "auth_method", None),
        }

    @app.get("/v1/tenant/private")
    async def tenant_private(request: Request) -> dict[str, str | None]:
        return {
            "user_id": getattr(request.state, "user_id", None),
            "tenant_id": getattr(request.state, "tenant_id", None),
            "auth_scheme": getattr(request.state, "auth_scheme", None),
            "auth_method": getattr(request.state, "auth_method", None),
        }

    return app


def build_http_client(jwks: dict) -> httpx.AsyncClient:
    async def jwks_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    return httpx.AsyncClient(transport=httpx.MockTransport(jwks_handler))


def build_token(
    private_key: str,
    *,
    issuer: str,
    subject: str = "user_clerk_123",
    org_id: str | None = None,
) -> str:
    claims = {
        "sub": subject,
        "iss": issuer,
        "exp": int(time.time()) + 3600,
    }
    if org_id is not None:
        claims["org_id"] = org_id

    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )


def test_jwt_with_valid_org_id_maps_to_tenant_and_sets_request_state() -> None:
    private_key, jwks = build_rsa_jwks()
    tenant_id = uuid.uuid4()
    org_id = "org_valid_123"
    token = build_token(
        private_key,
        issuer="https://clerk.example.test",
        org_id=org_id,
    )
    app = build_test_app(
        session_factory=FakeSessionFactory({org_id: tenant_id}),
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        http_client=build_http_client(jwks),
    )

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_clerk_123",
        "tenant_id": str(tenant_id),
        "auth_scheme": "bearer",
        "auth_method": "clerk_jwt",
    }


def test_jwt_with_unmapped_org_id_provisions_tenant_and_sets_request_state() -> None:
    private_key, jwks = build_rsa_jwks()
    org_id = "org_missing_123"
    token = build_token(
        private_key,
        issuer="https://clerk.example.test",
        org_id=org_id,
    )
    session_factory = FakeSessionFactory()
    app = build_test_app(
        session_factory=session_factory,
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        http_client=build_http_client(jwks),
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/tenant/private",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(session_factory.created_orgs[org_id])
    assert response.json()["auth_method"] == "clerk_jwt"


def test_jwt_without_org_id_returns_auth_003() -> None:
    private_key, jwks = build_rsa_jwks()
    token = build_token(
        private_key,
        issuer="https://clerk.example.test",
        org_id=None,
    )
    app = build_test_app(
        session_factory=FakeSessionFactory(),
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
        http_client=build_http_client(jwks),
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/tenant/private",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "org_required"
    assert response.json()["code"] == "AUTH_003"
