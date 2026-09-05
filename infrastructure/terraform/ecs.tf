resource "aws_security_group" "ecs" {
  name        = "${var.project_name}-ecs-sg"
  description = "Allow HTTP traffic from the ALB to ECS tasks"
  vpc_id      = aws_vpc.memoryos.id

  ingress {
    description     = "ALB to ECS"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-ecs-sg"
  })
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 30

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-logs"
  })
}

resource "aws_ecs_cluster" "memoryos" {
  name = "${var.project_name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-cluster"
  })
}

data "aws_iam_policy_document" "ecs_task_execution_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.project_name}-ecs-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume_role.json

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-ecs-execution-role"
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_secrets" {
  statement {
    actions = [
      "secretsmanager:GetSecretValue",
      "kms:Decrypt",
    ]

    resources = concat([
      aws_secretsmanager_secret.openai_api_key.arn,
      aws_secretsmanager_secret.secret_key.arn,
      aws_secretsmanager_secret.admin_secret.arn,
      aws_secretsmanager_secret.uui_session_secret.arn,
      aws_secretsmanager_secret.oauth_credential_encryption_key.arn,
      aws_secretsmanager_secret.qdrant_api_key.arn,
      aws_secretsmanager_secret.qdrant_url.arn,
      aws_secretsmanager_secret.clerk_secret_key.arn,
      aws_secretsmanager_secret.clerk_webhook_secret.arn,
      aws_secretsmanager_secret.smtp_pass.arn,
      aws_secretsmanager_secret.sentry_dsn.arn,
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.razorpay_key_id.arn,
      aws_secretsmanager_secret.razorpay_key_secret.arn,
      aws_secretsmanager_secret.razorpay_webhook_secret.arn,
      ],
      aws_secretsmanager_secret.redis_url[*].arn,
      aws_secretsmanager_secret.celery_broker_url[*].arn,
      aws_secretsmanager_secret.celery_result_backend[*].arn,
    )
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name   = "${var.project_name}-ecs-secrets-policy"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_secrets.json
}

resource "aws_iam_role" "ecs_task" {
  name               = "${var.project_name}-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume_role.json

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-ecs-task-role"
  })
}

# ECS uses the execution role to inject Secrets Manager values. The running API
# and Celery workers use this application task role to request per-record data
# keys directly from KMS.
data "aws_iam_policy_document" "ecs_task_memory_content_encryption" {
  statement {
    sid = "UseMemoryContentEnvelopeKey"

    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
    ]

    resources = [aws_kms_key.memory_content.arn]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "kms:EncryptionContextKeys"
      values   = ["memoryos_tenant_id"]
    }
  }

  statement {
    sid       = "DescribeMemoryContentEnvelopeKey"
    actions   = ["kms:DescribeKey"]
    resources = [aws_kms_key.memory_content.arn]
  }
}

resource "aws_iam_role_policy" "ecs_task_memory_content_encryption" {
  name   = "${var.project_name}-ecs-memory-content-encryption"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_memory_content_encryption.json
}

locals {
  ecs_container_environment = concat([
    { name = "APP_ENV", value = var.app_env },
    { name = "APP_VERSION", value = var.app_version },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "CORS_ALLOWED_ORIGINS", value = var.cors_allowed_origins },
    { name = "CONSENT_APP_BASE_URL", value = var.consent_app_base_url },
    { name = "BILLING_UPGRADE_URL", value = var.billing_upgrade_url },
    { name = "RAZORPAY_PLAN_STARTER_MONTHLY_INR", value = var.razorpay_plan_starter_monthly_inr },
    { name = "RAZORPAY_PLAN_STARTER_ANNUAL_INR", value = var.razorpay_plan_starter_annual_inr },
    { name = "RAZORPAY_PLAN_STARTER_MONTHLY_USD", value = var.razorpay_plan_starter_monthly_usd },
    { name = "RAZORPAY_PLAN_STARTER_ANNUAL_USD", value = var.razorpay_plan_starter_annual_usd },
    { name = "RAZORPAY_PLAN_GROWTH_MONTHLY_INR", value = var.razorpay_plan_growth_monthly_inr },
    { name = "RAZORPAY_PLAN_GROWTH_ANNUAL_INR", value = var.razorpay_plan_growth_annual_inr },
    { name = "RAZORPAY_PLAN_GROWTH_MONTHLY_USD", value = var.razorpay_plan_growth_monthly_usd },
    { name = "RAZORPAY_PLAN_GROWTH_ANNUAL_USD", value = var.razorpay_plan_growth_annual_usd },
    { name = "RAZORPAY_PLAN_SCALE_MONTHLY_INR", value = var.razorpay_plan_scale_monthly_inr },
    { name = "RAZORPAY_PLAN_SCALE_ANNUAL_INR", value = var.razorpay_plan_scale_annual_inr },
    { name = "RAZORPAY_PLAN_SCALE_MONTHLY_USD", value = var.razorpay_plan_scale_monthly_usd },
    { name = "RAZORPAY_PLAN_SCALE_ANNUAL_USD", value = var.razorpay_plan_scale_annual_usd },
    { name = "DB_POOL_SIZE", value = tostring(var.db_pool_size) },
    { name = "DB_MAX_OVERFLOW", value = tostring(var.db_max_overflow) },
    { name = "DB_POOL_TIMEOUT_SECONDS", value = tostring(var.db_pool_timeout_seconds) },
    { name = "CELERY_LOG_LEVEL", value = var.celery_log_level },
    { name = "QDRANT_TIMEOUT_SECONDS", value = tostring(var.qdrant_timeout_seconds) },
    { name = "QDRANT_PREFER_GRPC", value = tostring(var.qdrant_prefer_grpc) },
    { name = "QDRANT_ASYNC_POOL_SIZE", value = tostring(var.qdrant_async_pool_size) },
    { name = "CLERK_JWT_ISSUER", value = var.clerk_jwt_issuer },
    { name = "LLM_PROVIDER_ORDER", value = var.llm_provider_order },
    { name = "OPENAI_MODEL", value = var.openai_model },
    { name = "OPENAI_TIMEOUT_SECONDS", value = tostring(var.openai_timeout_seconds) },
    { name = "EXTRACTION_MODEL", value = var.extraction_model },
    { name = "EMBEDDING_PROVIDER", value = var.embedding_provider },
    { name = "EMBEDDING_MODEL", value = var.embedding_model },
    { name = "EMBEDDING_MODEL_ID", value = var.embedding_model_id },
    { name = "EMBEDDING_DIMENSIONS", value = tostring(var.embedding_dimensions) },
    { name = "EXTRACTION_PAYLOAD_RETENTION_DAYS", value = tostring(var.extraction_payload_retention_days) },
    { name = "RETRIEVAL_L1_CACHE_TTL_SECONDS", value = tostring(var.retrieval_l1_cache_ttl_seconds) },
    { name = "RETRIEVAL_MODEL_ID_CACHE_TTL_SECONDS", value = tostring(var.retrieval_model_id_cache_ttl_seconds) },
    { name = "RETRIEVAL_MEMORY_COUNT_CACHE_TTL_SECONDS", value = tostring(var.retrieval_memory_count_cache_ttl_seconds) },
    { name = "RETRIEVAL_HOT_TIER_CACHE_TTL_SECONDS", value = tostring(var.retrieval_hot_tier_cache_ttl_seconds) },
    { name = "RETRIEVAL_CACHE_READ_TIMEOUT_SECONDS", value = tostring(var.retrieval_cache_read_timeout_seconds) },
    { name = "RETRIEVAL_REDIS_CACHE_READ_ENABLED", value = tostring(var.retrieval_redis_cache_read_enabled) },
    { name = "RETRIEVAL_REDIS_CACHE_WRITE_ENABLED", value = tostring(var.retrieval_redis_cache_write_enabled) },
    { name = "VECTOR_PAYLOAD_INCLUDE_CONTENT", value = tostring(var.vector_payload_include_content) },
    { name = "RETRIEVAL_OVERFETCH_MULTIPLIER", value = tostring(var.retrieval_overfetch_multiplier) },
    { name = "SMTP_HOST", value = var.smtp_host },
    { name = "SMTP_PORT", value = tostring(var.smtp_port) },
    { name = "SMTP_USER", value = var.smtp_user },
    { name = "SMTP_FROM", value = var.smtp_from },
    { name = "EMAIL_FROM", value = var.email_from },
    { name = "TENANT_RATE_LIMIT_PER_MINUTE", value = tostring(var.tenant_rate_limit_per_minute) },
    { name = "DATA_ENCRYPTION_PROVIDER", value = var.data_encryption_provider },
    { name = "DATA_ENCRYPTION_KMS_KEY_ID", value = aws_kms_key.memory_content.arn },
    { name = "DATA_ENCRYPTION_WRITE_MODE", value = var.data_encryption_write_mode },
    ], var.redis_auth_enabled ? [] : [
    { name = "REDIS_URL", value = "${var.redis_transit_encryption_enabled ? "rediss" : "redis"}://${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/0" },
    { name = "CELERY_BROKER_URL", value = "${var.redis_transit_encryption_enabled ? "rediss" : "redis"}://${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/0${var.redis_transit_encryption_enabled ? "?ssl_cert_reqs=required" : ""}" },
    { name = "CELERY_RESULT_BACKEND", value = "${var.redis_transit_encryption_enabled ? "rediss" : "redis"}://${aws_elasticache_replication_group.memoryos.primary_endpoint_address}:6379/1${var.redis_transit_encryption_enabled ? "?ssl_cert_reqs=required" : ""}" },
  ])
  ecs_container_secrets = concat([
    { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
    { name = "SECRET_KEY", valueFrom = aws_secretsmanager_secret.secret_key.arn },
    { name = "ADMIN_SECRET", valueFrom = aws_secretsmanager_secret.admin_secret.arn },
    { name = "UUI_SESSION_SECRET", valueFrom = aws_secretsmanager_secret.uui_session_secret.arn },
    { name = "OAUTH_CREDENTIAL_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.oauth_credential_encryption_key.arn },
    { name = "QDRANT_API_KEY", valueFrom = aws_secretsmanager_secret.qdrant_api_key.arn },
    { name = "QDRANT_URL", valueFrom = aws_secretsmanager_secret.qdrant_url.arn },
    { name = "CLERK_SECRET_KEY", valueFrom = aws_secretsmanager_secret.clerk_secret_key.arn },
    { name = "CLERK_WEBHOOK_SECRET", valueFrom = aws_secretsmanager_secret.clerk_webhook_secret.arn },
    { name = "SMTP_PASS", valueFrom = aws_secretsmanager_secret.smtp_pass.arn },
    { name = "SENTRY_DSN", valueFrom = aws_secretsmanager_secret.sentry_dsn.arn },
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "RAZORPAY_KEY_ID", valueFrom = aws_secretsmanager_secret.razorpay_key_id.arn },
    { name = "RAZORPAY_KEY_SECRET", valueFrom = aws_secretsmanager_secret.razorpay_key_secret.arn },
    { name = "RAZORPAY_WEBHOOK_SECRET", valueFrom = aws_secretsmanager_secret.razorpay_webhook_secret.arn },
    ], var.redis_auth_enabled ? [
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url[0].arn },
    { name = "CELERY_BROKER_URL", valueFrom = aws_secretsmanager_secret.celery_broker_url[0].arn },
    { name = "CELERY_RESULT_BACKEND", valueFrom = aws_secretsmanager_secret.celery_result_backend[0].arn },
  ] : [])

  celery_worker_configs = {
    scale = {
      queues        = "scale-extraction,enterprise-extraction"
      concurrency   = var.celery_scale_concurrency
      desired_count = var.celery_scale_desired_count
    }
    growth = {
      queues        = "growth-extraction"
      concurrency   = var.celery_growth_concurrency
      desired_count = var.celery_growth_desired_count
    }
    starter = {
      queues        = "starter-extraction,free-extraction"
      concurrency   = var.celery_starter_concurrency
      desired_count = var.celery_starter_desired_count
    }
    background = {
      queues        = "celery,reembedding,dead-letter"
      concurrency   = var.celery_background_concurrency
      desired_count = var.celery_background_desired_count
    }
  }
}

resource "aws_ecs_task_definition" "memoryos" {
  family                   = "${var.project_name}-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.ecs_cpu)
  memory                   = tostring(var.ecs_memory)
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "memoryos-api"
      image     = "${aws_ecr_repository.memoryos.repository_url}:${var.container_image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]
      environment = local.ecs_container_environment
      secrets     = local.ecs_container_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-task"
  })
}

resource "aws_ecs_task_definition" "celery_worker" {
  for_each                 = local.celery_worker_configs
  family                   = "${var.project_name}-${each.key}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.ecs_cpu)
  memory                   = tostring(var.ecs_memory)
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "memoryos-celery-${each.key}"
      image     = "${aws_ecr_repository.memoryos.repository_url}:${var.container_image_tag}"
      essential = true
      command = [
        "celery",
        "-A",
        "api.celery_app.celery_app",
        "worker",
        "-Q",
        each.value.queues,
        "-c",
        tostring(each.value.concurrency),
        "--prefetch-multiplier",
        "1",
        "--loglevel=${var.celery_log_level}",
      ]
      environment = local.ecs_container_environment
      secrets     = local.ecs_container_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${each.key}-worker-task"
  })
}

resource "aws_ecs_service" "memoryos" {
  name            = "${var.project_name}-service"
  cluster         = aws_ecs_cluster.memoryos.id
  task_definition = aws_ecs_task_definition.memoryos.arn
  desired_count   = var.ecs_api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    assign_public_ip = false
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.ecs.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "memoryos-api"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  depends_on = [aws_lb_listener.https]

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-service"
  })
}

resource "aws_ecs_service" "celery_worker" {
  for_each        = local.celery_worker_configs
  name            = "${var.project_name}-${each.key}-worker"
  cluster         = aws_ecs_cluster.memoryos.id
  task_definition = aws_ecs_task_definition.celery_worker[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    assign_public_ip = false
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.ecs.id]
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${each.key}-worker-service"
  })
}

resource "aws_appautoscaling_target" "ecs_service" {
  max_capacity       = var.ecs_max_capacity
  min_capacity       = var.ecs_api_min_capacity
  resource_id        = "service/${aws_ecs_cluster.memoryos.name}/${aws_ecs_service.memoryos.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "ecs_cpu_target" {
  name               = "${var.project_name}-cpu-target"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs_service.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs_service.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs_service.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    target_value       = 70
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_target" "scale_worker" {
  count              = var.enable_scale_worker_autoscaling ? 1 : 0
  max_capacity       = var.celery_scale_max_capacity
  min_capacity       = local.celery_worker_configs.scale.desired_count
  resource_id        = "service/${aws_ecs_cluster.memoryos.name}/${aws_ecs_service.celery_worker["scale"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "scale_worker_scale_out" {
  count              = var.enable_scale_worker_autoscaling ? 1 : 0
  name               = "${var.project_name}-scale-worker-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.scale_worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.scale_worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.scale_worker[0].service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 300
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 8
    }
  }
}

resource "aws_appautoscaling_policy" "scale_worker_scale_in" {
  count              = var.enable_scale_worker_autoscaling ? 1 : 0
  name               = "${var.project_name}-scale-worker-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.scale_worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.scale_worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.scale_worker[0].service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 600
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = local.celery_worker_configs.scale.desired_count
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "scale_queue_depth_high" {
  count               = var.enable_scale_worker_autoscaling ? 1 : 0
  alarm_name          = "${var.project_name}-scale-extraction-depth-high"
  alarm_description   = "Scale out Scale extraction workers when queue depth stays above 60."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  metric_name         = "QueueDepth"
  namespace           = "MemoryOS/Celery"
  period              = 60
  statistic           = "Maximum"
  threshold           = 60
  alarm_actions       = [aws_appautoscaling_policy.scale_worker_scale_out[0].arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = "scale-extraction"
  }
}

resource "aws_cloudwatch_metric_alarm" "all_extraction_queues_empty" {
  count               = var.enable_scale_worker_autoscaling ? 1 : 0
  alarm_name          = "${var.project_name}-all-extraction-queues-empty"
  alarm_description   = "Scale workers back to baseline when all extraction queues are idle."
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 10
  datapoints_to_alarm = 10
  threshold           = 0
  alarm_actions       = [aws_appautoscaling_policy.scale_worker_scale_in[0].arn]
  treat_missing_data  = "breaching"

  metric_query {
    id          = "scale"
    return_data = false
    metric {
      metric_name = "QueueDepth"
      namespace   = "MemoryOS/Celery"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        QueueName = "scale-extraction"
      }
    }
  }

  metric_query {
    id          = "enterprise"
    return_data = false
    metric {
      metric_name = "QueueDepth"
      namespace   = "MemoryOS/Celery"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        QueueName = "enterprise-extraction"
      }
    }
  }

  metric_query {
    id          = "growth"
    return_data = false
    metric {
      metric_name = "QueueDepth"
      namespace   = "MemoryOS/Celery"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        QueueName = "growth-extraction"
      }
    }
  }

  metric_query {
    id          = "starter"
    return_data = false
    metric {
      metric_name = "QueueDepth"
      namespace   = "MemoryOS/Celery"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        QueueName = "starter-extraction"
      }
    }
  }

  metric_query {
    id          = "free"
    return_data = false
    metric {
      metric_name = "QueueDepth"
      namespace   = "MemoryOS/Celery"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        QueueName = "free-extraction"
      }
    }
  }

  metric_query {
    id          = "total"
    expression  = "scale + enterprise + growth + starter + free"
    label       = "TotalQueueDepth"
    return_data = true
  }
}
