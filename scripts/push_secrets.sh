#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required but was not found in PATH." >&2
  exit 1
fi

if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_REGION:-}" && -z "${AWS_DEFAULT_REGION:-}" ]]; then
  echo "Set AWS_PROFILE or AWS_REGION/AWS_DEFAULT_REGION before running this script." >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

get_env_value() {
  local key="$1"
  local line

  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    return 1
  fi

  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

put_secret() {
  local secret_id="$1"
  local env_key="$2"
  local value

  if ! value="$(get_env_value "$env_key")"; then
    echo "Skipping $secret_id because $env_key is missing from $ENV_FILE" >&2
    return 0
  fi

  if [[ -z "$value" ]]; then
    echo "Skipping $secret_id because $env_key is empty in $ENV_FILE" >&2
    return 0
  fi

  echo "Updating Secrets Manager value for $secret_id"
  aws secretsmanager put-secret-value \
    --secret-id "$secret_id" \
    --secret-string "$value" \
    >/dev/null
}

put_secret "memoryos/GEMINI_API_KEY" "GEMINI_API_KEY"
put_secret "memoryos/QDRANT_API_KEY" "QDRANT_API_KEY"
put_secret "memoryos/QDRANT_URL" "QDRANT_URL"
put_secret "memoryos/CLERK_SECRET_KEY" "CLERK_SECRET_KEY"
put_secret "memoryos/CLERK_WEBHOOK_SECRET" "CLERK_WEBHOOK_SECRET"
put_secret "memoryos/SENTRY_DSN" "SENTRY_DSN"
put_secret "memoryos/DATABASE_URL" "DATABASE_URL"

echo "Secret push complete."
