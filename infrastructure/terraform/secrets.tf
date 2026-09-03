resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = var.openai_api_key_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-openai-api-key"
  })
}

resource "aws_secretsmanager_secret" "secret_key" {
  name                    = var.secret_key_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-secret-key"
  })
}

resource "aws_secretsmanager_secret" "admin_secret" {
  name                    = var.admin_secret_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-admin-secret"
  })
}

resource "aws_secretsmanager_secret" "uui_session_secret" {
  name                    = var.uui_session_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-uui-session-secret"
  })
}

resource "aws_secretsmanager_secret" "oauth_credential_encryption_key" {
  name                    = var.oauth_credential_encryption_key_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-oauth-credential-encryption-key"
  })
}
resource "aws_secretsmanager_secret" "qdrant_api_key" {
  name                    = var.qdrant_api_key_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-qdrant-api-key"
  })
}

resource "aws_secretsmanager_secret" "qdrant_url" {
  name                    = var.qdrant_url_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-qdrant-url"
  })
}

resource "aws_secretsmanager_secret" "clerk_secret_key" {
  name                    = var.clerk_secret_key_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-clerk-secret-key"
  })
}

resource "aws_secretsmanager_secret" "clerk_webhook_secret" {
  name                    = var.clerk_webhook_secret_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-clerk-webhook-secret"
  })
}

resource "aws_secretsmanager_secret" "smtp_pass" {
  name                    = var.smtp_pass_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-smtp-pass"
  })
}
resource "aws_secretsmanager_secret" "sentry_dsn" {
  name                    = var.sentry_dsn_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-sentry-dsn"
  })
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = var.database_url_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-database-url"
  })
}

# These values are created only for environments that explicitly require Redis
# AUTH. They contain the ElastiCache endpoint plus its authentication token and
# are injected into ECS as secrets rather than task-definition environment text.
resource "aws_secretsmanager_secret" "redis_url" {
  count                   = var.redis_auth_enabled ? 1 : 0
  name                    = var.redis_url_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-redis-url"
  })
}

resource "aws_secretsmanager_secret" "celery_broker_url" {
  count                   = var.redis_auth_enabled ? 1 : 0
  name                    = var.celery_broker_url_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-celery-broker-url"
  })
}

resource "aws_secretsmanager_secret" "celery_result_backend" {
  count                   = var.redis_auth_enabled ? 1 : 0
  name                    = var.celery_result_backend_secret_name
  recovery_window_in_days = 7

  lifecycle {
    ignore_changes  = [tags]
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-celery-result-backend"
  })
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  count         = var.redis_auth_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.redis_url[0].id
  secret_string = "rediss://:${var.redis_auth_token}@${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/0?ssl_cert_reqs=required"
}

resource "aws_secretsmanager_secret_version" "celery_broker_url" {
  count         = var.redis_auth_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.celery_broker_url[0].id
  secret_string = "rediss://:${var.redis_auth_token}@${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/0?ssl_cert_reqs=required"
}

resource "aws_secretsmanager_secret_version" "celery_result_backend" {
  count         = var.redis_auth_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.celery_result_backend[0].id
  secret_string = "rediss://:${var.redis_auth_token}@${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/1?ssl_cert_reqs=required"
}

# Terraform creates the normal secret containers only. Put their real values
# after apply, for example. When Redis AUTH is enabled, Terraform writes the
# three TLS Redis URL secrets from the sensitive input token instead.
# aws secretsmanager put-secret-value --secret-id memoryos/OPENAI_API_KEY --secret-string "sk-..."
# aws secretsmanager put-secret-value --secret-id memoryos/SECRET_KEY --secret-string "generate-a-long-random-value"
# aws secretsmanager put-secret-value --secret-id memoryos/ADMIN_SECRET --secret-string "generate-a-long-random-value"
# aws secretsmanager put-secret-value --secret-id memoryos/UUI_SESSION_SECRET --secret-string "generate-a-long-random-value"
# aws secretsmanager put-secret-value --secret-id memoryos/OAUTH_CREDENTIAL_ENCRYPTION_KEY --secret-string "base64-32-byte-fernet-key"
# aws secretsmanager put-secret-value --secret-id memoryos/DATABASE_URL --secret-string "postgresql+asyncpg://postgres:YOUR_PASSWORD@HOST:5432/postgres"
# aws secretsmanager put-secret-value --secret-id memoryos/QDRANT_URL --secret-string "https://YOUR_QDRANT_CLUSTER"
# aws secretsmanager put-secret-value --secret-id memoryos/QDRANT_API_KEY --secret-string "..."
# aws secretsmanager put-secret-value --secret-id memoryos/CLERK_SECRET_KEY --secret-string "..."
# aws secretsmanager put-secret-value --secret-id memoryos/CLERK_WEBHOOK_SECRET --secret-string "..."
# aws secretsmanager put-secret-value --secret-id memoryos/SMTP_PASS --secret-string "your-smtp-password"
# aws secretsmanager put-secret-value --secret-id memoryos/SENTRY_DSN --secret-string "..."
