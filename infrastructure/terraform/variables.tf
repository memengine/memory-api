variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Project name prefix for AWS resources."
  type        = string
  default     = "memoryos"
}

variable "environment" {
  description = "Deployment environment label used for tagging resources."
  type        = string
  default     = "staging"
}

variable "app_version" {
  description = "Application release label exposed to the ECS task. Use a Git SHA or release tag."
  type        = string
  default     = "bootstrap"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the public subnets."
  type        = list(string)
  default     = ["10.20.1.0/24", "10.20.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for the private subnets."
  type        = list(string)
  default     = ["10.20.11.0/24", "10.20.12.0/24"]
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for the HTTPS ALB listener."
  type        = string
}

variable "ecr_repository_name" {
  description = "Name of the ECR repository."
  type        = string
  default     = "memoryos-api"
}

variable "container_image_tag" {
  description = "Container image tag to deploy to ECS."
  type        = string
  default     = "latest"
}

variable "ecs_cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 512
}

variable "ecs_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 1024
}

variable "ecs_max_capacity" {
  description = "Maximum ECS service task count for auto scaling."
  type        = number
  default     = 6
}

variable "ecs_api_desired_count" {
  description = "Initial desired count for the API ECS service."
  type        = number
  default     = 2
}

variable "ecs_api_min_capacity" {
  description = "Minimum API ECS service count for auto scaling."
  type        = number
  default     = 2
}

variable "redis_node_type" {
  description = "ElastiCache node type for Celery broker/result backend."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.1"
}

variable "redis_transit_encryption_enabled" {
  description = "Whether ElastiCache requires TLS in transit. Enable with rediss:// URLs."
  type        = bool
  default     = false
}

variable "redis_transit_encryption_mode" {
  description = "ElastiCache TLS migration mode. Existing clusters must use preferred before required."
  type        = string
  default     = "required"

  validation {
    condition     = contains(["preferred", "required"], var.redis_transit_encryption_mode)
    error_message = "redis_transit_encryption_mode must be preferred or required."
  }
}

variable "redis_auth_enabled" {
  description = "Whether ElastiCache AUTH is required. When enabled, REDIS_URL and Celery URLs are injected as Secrets Manager values."
  type        = bool
  default     = false
}

variable "redis_auth_token_update_strategy" {
  description = "ElastiCache AUTH transition strategy. Existing passwordless clusters must use ROTATE before SET."
  type        = string
  default     = "SET"

  validation {
    condition     = contains(["ROTATE", "SET"], var.redis_auth_token_update_strategy)
    error_message = "redis_auth_token_update_strategy must be ROTATE or SET."
  }
}

variable "manage_redis_connection_secrets" {
  description = "Keep Redis and Celery connection secrets managed during a staged Redis security migration."
  type        = bool
  default     = false
}

variable "redis_auth_token" {
  description = "URL-safe ElastiCache AUTH token. Supply only as TF_VAR_redis_auth_token; never put it in a tfvars file."
  type        = string
  default     = null
  sensitive   = true
}

variable "redis_snapshot_retention_limit" {
  description = "Number of daily Redis snapshots to retain. Set to zero only for disposable local development."
  type        = number
  default     = 0

  validation {
    condition     = var.redis_snapshot_retention_limit >= 0 && var.redis_snapshot_retention_limit <= 35
    error_message = "Redis snapshot retention must be between 0 and 35 days."
  }
}

variable "redis_snapshot_window" {
  description = "UTC one-hour window for Redis snapshots when retention is enabled."
  type        = string
  default     = "05:00-06:00"
}

variable "redis_url_secret_name" {
  description = "Secrets Manager name for the authenticated REDIS_URL used when Redis AUTH is enabled."
  type        = string
  default     = "memoryos/REDIS_URL"
}

variable "celery_broker_url_secret_name" {
  description = "Secrets Manager name for the authenticated CELERY_BROKER_URL used when Redis AUTH is enabled."
  type        = string
  default     = "memoryos/CELERY_BROKER_URL"
}

variable "celery_result_backend_secret_name" {
  description = "Secrets Manager name for the authenticated CELERY_RESULT_BACKEND used when Redis AUTH is enabled."
  type        = string
  default     = "memoryos/CELERY_RESULT_BACKEND"
}

variable "clerk_jwt_issuer" {
  description = "CLERK_JWT_ISSUER used by backend JWT verification."
  type        = string
}

variable "openai_api_key_secret_name" {
  description = "Secrets Manager name for OPENAI_API_KEY."
  type        = string
  default     = "memoryos/OPENAI_API_KEY"
}

variable "secret_key_secret_name" {
  description = "Secrets Manager name for SECRET_KEY used by backend signing and app security."
  type        = string
  default     = "memoryos/SECRET_KEY"
}

variable "admin_secret_secret_name" {
  description = "Secrets Manager name for ADMIN_SECRET used by internal/admin endpoints."
  type        = string
  default     = "memoryos/ADMIN_SECRET"
}

variable "uui_session_secret_name" {
  description = "Secrets Manager name for UUI_SESSION_SECRET used by Memory Passport sessions."
  type        = string
  default     = "memoryos/UUI_SESSION_SECRET"
}

variable "oauth_credential_encryption_key_secret_name" {
  description = "Secrets Manager name for OAUTH_CREDENTIAL_ENCRYPTION_KEY used to encrypt OAuth connector credentials."
  type        = string
  default     = "memoryos/OAUTH_CREDENTIAL_ENCRYPTION_KEY"
}
variable "qdrant_api_key_secret_name" {
  description = "Secrets Manager name for QDRANT_API_KEY."
  type        = string
  default     = "memoryos/QDRANT_API_KEY"
}

variable "qdrant_url_secret_name" {
  description = "Secrets Manager name for QDRANT_URL."
  type        = string
  default     = "memoryos/QDRANT_URL"
}

variable "clerk_secret_key_secret_name" {
  description = "Secrets Manager name for CLERK_SECRET_KEY."
  type        = string
  default     = "memoryos/CLERK_SECRET_KEY"
}

variable "clerk_webhook_secret_secret_name" {
  description = "Secrets Manager name for CLERK_WEBHOOK_SECRET."
  type        = string
  default     = "memoryos/CLERK_WEBHOOK_SECRET"
}

variable "smtp_pass_secret_name" {
  description = "Secrets Manager name for SMTP_PASS used by consent email delivery."
  type        = string
  default     = "memoryos/SMTP_PASS"
}
variable "sentry_dsn_secret_name" {
  description = "Secrets Manager name for SENTRY_DSN."
  type        = string
  default     = "memoryos/SENTRY_DSN"
}

variable "database_url_secret_name" {
  description = "Secrets Manager name for Supabase DATABASE_URL."
  type        = string
  default     = "memoryos/DATABASE_URL"
}

variable "razorpay_key_id_secret_name" {
  description = "Secrets Manager name for RAZORPAY_KEY_ID."
  type        = string
  default     = "memoryos/RAZORPAY_KEY_ID"
}

variable "razorpay_key_secret_secret_name" {
  description = "Secrets Manager name for RAZORPAY_KEY_SECRET."
  type        = string
  default     = "memoryos/RAZORPAY_KEY_SECRET"
}

variable "razorpay_webhook_secret_secret_name" {
  description = "Secrets Manager name for RAZORPAY_WEBHOOK_SECRET."
  type        = string
  default     = "memoryos/RAZORPAY_WEBHOOK_SECRET"
}

variable "razorpay_plan_starter_monthly_inr" {
  description = "Razorpay Starter monthly INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_starter_annual_inr" {
  description = "Razorpay Starter annual INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_starter_monthly_usd" {
  description = "Razorpay Starter monthly USD plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_starter_annual_usd" {
  description = "Razorpay Starter annual USD plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_growth_monthly_inr" {
  description = "Razorpay Growth monthly INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_growth_annual_inr" {
  description = "Razorpay Growth annual INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_growth_monthly_usd" {
  description = "Razorpay Growth monthly USD plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_growth_annual_usd" {
  description = "Razorpay Growth annual USD plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_scale_monthly_inr" {
  description = "Razorpay Scale monthly INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_scale_annual_inr" {
  description = "Razorpay Scale annual INR plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_scale_monthly_usd" {
  description = "Razorpay Scale monthly USD plan identifier."
  type        = string
  default     = ""
}

variable "razorpay_plan_scale_annual_usd" {
  description = "Razorpay Scale annual USD plan identifier."
  type        = string
  default     = ""
}
variable "app_env" {
  description = "Runtime APP_ENV value injected into ECS tasks."
  type        = string
  default     = "production"
}

variable "cors_allowed_origins" {
  description = "Comma-separated browser origins allowed by the backend CORS middleware."
  type        = string
  default     = "https://app.memoryo.dev,https://consent.memoryo.dev,https://operator.memoryo.dev,https://memoryo.dev"
}

variable "consent_app_base_url" {
  description = "Public base URL for the Memory Passport consent app."
  type        = string
  default     = "https://consent.memoryo.dev"
}

variable "billing_upgrade_url" {
  description = "Public URL users are sent to for plan upgrades."
  type        = string
  default     = "https://memoryo.dev/pricing"
}

variable "db_pool_size" {
  description = "Async database pool size for API and worker tasks."
  type        = number
  default     = 20
}

variable "db_max_overflow" {
  description = "Maximum temporary overflow connections above the database pool size."
  type        = number
  default     = 30
}

variable "db_pool_timeout_seconds" {
  description = "Seconds to wait for a database connection before timing out."
  type        = number
  default     = 30
}

variable "celery_scale_concurrency" {
  description = "Celery concurrency for scale and enterprise extraction queues."
  type        = number
  default     = 4
}

variable "celery_scale_desired_count" {
  description = "Initial ECS task count for the scale and enterprise worker queues."
  type        = number
  default     = 4
}

variable "celery_growth_concurrency" {
  description = "Celery concurrency for the growth extraction queue."
  type        = number
  default     = 3
}

variable "celery_growth_desired_count" {
  description = "Initial ECS task count for the growth extraction worker queue."
  type        = number
  default     = 3
}

variable "celery_starter_concurrency" {
  description = "Celery concurrency for starter and free extraction queues."
  type        = number
  default     = 3
}

variable "celery_starter_desired_count" {
  description = "Initial ECS task count for the starter and free worker queues."
  type        = number
  default     = 3
}

variable "celery_background_concurrency" {
  description = "Celery concurrency for background maintenance queues."
  type        = number
  default     = 2
}

variable "celery_background_desired_count" {
  description = "Initial ECS task count for background maintenance queues."
  type        = number
  default     = 2
}

variable "celery_scale_max_capacity" {
  description = "Maximum ECS task count for the scale worker auto-scaling target."
  type        = number
  default     = 8
}

variable "enable_scale_worker_autoscaling" {
  description = "Whether queue-depth alarms may start scale-worker tasks."
  type        = bool
  default     = true
}

variable "celery_log_level" {
  description = "Celery worker log level."
  type        = string
  default     = "info"
}

variable "qdrant_timeout_seconds" {
  description = "Qdrant client request timeout in seconds."
  type        = number
  default     = 10
}

variable "qdrant_prefer_grpc" {
  description = "Whether Qdrant clients should prefer gRPC."
  type        = bool
  default     = false
}

variable "qdrant_async_pool_size" {
  description = "Async Qdrant connection pool size."
  type        = number
  default     = 100
}

variable "llm_provider_order" {
  description = "Ordered LLM provider preference. Production currently uses OpenAI first."
  type        = string
  default     = "openai"
}

variable "openai_model" {
  description = "Default OpenAI model for general LLM calls."
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_timeout_seconds" {
  description = "OpenAI request timeout in seconds."
  type        = number
  default     = 30
}

variable "extraction_model" {
  description = "Model used by the extraction pipeline."
  type        = string
  default     = "gpt-4o-mini"
}

variable "embedding_provider" {
  description = "Embedding provider used by the memory retriever."
  type        = string
  default     = "openai"
}

variable "embedding_model" {
  description = "Embedding model name."
  type        = string
  default     = "text-embedding-3-small"
}

variable "embedding_model_id" {
  description = "Stable embedding model identifier stored with vectors."
  type        = string
  default     = "openai-text-embedding-3-small-v1"
}

variable "embedding_dimensions" {
  description = "Embedding vector dimension for the configured embedding model."
  type        = number
  default     = 1536
}

variable "extraction_payload_retention_days" {
  description = "Days to retain extraction payloads for provenance/debugging."
  type        = number
  default     = 30
}

variable "retrieval_l1_cache_ttl_seconds" {
  description = "L1 retrieval cache TTL in seconds."
  type        = number
  default     = 5.0
}

variable "retrieval_model_id_cache_ttl_seconds" {
  description = "Embedding model ID cache TTL in seconds."
  type        = number
  default     = 60.0
}

variable "retrieval_memory_count_cache_ttl_seconds" {
  description = "Memory count cache TTL in seconds."
  type        = number
  default     = 30.0
}

variable "retrieval_hot_tier_cache_ttl_seconds" {
  description = "Hot-tier retrieval cache TTL in seconds."
  type        = number
  default     = 2.0
}

variable "retrieval_cache_read_timeout_seconds" {
  description = "Redis retrieval cache read timeout in seconds."
  type        = number
  default     = 0.05
}

variable "retrieval_redis_cache_read_enabled" {
  description = "Whether retrieval should read from Redis cache."
  type        = bool
  default     = true
}

variable "retrieval_redis_cache_write_enabled" {
  description = "Whether retrieval may write full results to Redis. Disable in staging to avoid new plaintext cache replicas."
  type        = bool
  default     = true
}

variable "vector_payload_include_content" {
  description = "Whether normal-memory Qdrant and vector-outbox payloads include plaintext content. Disable only after retrieval fallback validation."
  type        = bool
  default     = true
}

variable "retrieval_overfetch_multiplier" {
  description = "Retriever overfetch multiplier before final ranking."
  type        = number
  default     = 3
}

variable "smtp_host" {
  description = "SMTP host for consent login emails. Leave blank to disable SMTP delivery."
  type        = string
  default     = ""
}

variable "smtp_port" {
  description = "SMTP port for consent login emails."
  type        = number
  default     = 587
}

variable "smtp_user" {
  description = "SMTP username for consent login emails."
  type        = string
  default     = ""
}

variable "smtp_from" {
  description = "SMTP From header value."
  type        = string
  default     = ""
}

variable "email_from" {
  description = "Application email sender address."
  type        = string
  default     = ""
}

variable "tenant_rate_limit_per_minute" {
  description = "Default tenant API rate limit per minute."
  type        = number
  default     = 120
}

variable "data_encryption_provider" {
  description = "Envelope-encryption provider used by MemoryOS. Keep disabled until a staged rollout is approved."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "aws-kms"], var.data_encryption_provider)
    error_message = "data_encryption_provider must be disabled or aws-kms."
  }
}

variable "data_encryption_write_mode" {
  description = "Envelope-encryption write mode. dual-write retains plaintext during the staged migration."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "dual-write"], var.data_encryption_write_mode)
    error_message = "data_encryption_write_mode must be disabled or dual-write."
  }
}
