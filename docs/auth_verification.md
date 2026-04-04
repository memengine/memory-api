# Auth Verification

## Scope

Verified the dual auth middleware in `api/middleware/auth.py` for:

- Clerk JWT auth via JWKS
- API key auth via bcrypt hash comparison
- standard 401 behavior
- public endpoint bypass
- hash-only API key handling

## Checklist Results

- Valid Clerk JWT: PASS
  - Verified by `tests/integration/test_auth.py::test_bearer_token_is_verified_against_clerk_jwks`
  - Request passed and `request.state.user_id` resolved to `user_clerk_123`

- Invalid JWT: PASS
  - Verified by `tests/integration/test_auth.py::test_invalid_jwt_is_rejected_with_401`
  - Token with wrong issuer was rejected with `401`

- Valid API key: PASS
  - Verified against a real temporary API key row created in PostgreSQL
  - Request returned `200`
  - Returned user id matched the seeded database user id

- Invalid API key: PASS
  - Verified by integration test and real DB-backed script
  - Rejected with `401`

- Tampered JWT (wrong signature): PASS
  - Verified by `tests/integration/test_auth.py::test_tampered_jwt_with_wrong_signature_is_rejected`
  - Rejected with `401`

- No auth header: PASS
  - Verified by `tests/integration/test_auth.py::test_missing_auth_header_is_rejected_with_401`
  - Rejected with `401`

- `grep 'api_key' api/middleware/auth.py`: PASS
  - Confirmed API keys are compared through `verify_api_key(raw_api_key, key_hash)`
  - Cache key uses `fingerprint_api_key(raw_api_key)`
  - Structured logs do not include the raw API key or JWT

## Commands Run

```powershell
python -m pytest tests/integration/test_auth.py
rg "api_key" api/middleware/auth.py -n
```

Real DB-backed API key verification used a temporary user and API key inserted into PostgreSQL, then cleaned both up after the request check.

## Key Outputs

### Integration test suite

```text
7 passed
```

### Real DB-backed API key verification

```json
{
  "valid_status": 200,
  "valid_body": {
    "user_id": "42c859d7-41d5-4c1b-9638-6a4066d7a473",
    "auth_scheme": "apikey"
  },
  "invalid_status": 401,
  "invalid_body": {
    "error": "unauthorized",
    "code": "AUTH_001",
    "request_id": "3a72544a-51ca-4948-9bba-2dd424ea4b08"
  },
  "expected_user_id": "42c859d7-41d5-4c1b-9638-6a4066d7a473"
}
```

## Notes

- Public endpoints remained excluded from auth:
  - `GET /health`
  - `GET /docs`
  - `GET /redoc`
  - `GET /openapi.json`
  - `POST /v1/webhooks/*`

- API key auth currently verifies by scanning active API key hashes from the database and comparing with bcrypt. This is correct for security, but it may become expensive at high scale. A future optimization would be storing a non-secret lookup fingerprint in the database for candidate narrowing, while still keeping bcrypt as the final verifier.


