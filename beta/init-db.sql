-- Fresh, dedicated local beta database only. Public local-only password.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE ROLE memoryos_beta_app LOGIN PASSWORD 'local_beta_only' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE memoryos_beta TO memoryos_beta_app;
GRANT USAGE, CREATE ON SCHEMA public TO memoryos_beta_app;
