output "alb_dns_name" {
  description = "Public DNS name of the Application Load Balancer."
  value       = aws_lb.memoryos.dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL for MemoryOS images."
  value       = aws_ecr_repository.memoryos.repository_url
}

output "ecr_repository_name" {
  description = "ECR repository name to use as the ECR_REPOSITORY GitHub secret."
  value       = aws_ecr_repository.memoryos.name
}

output "ecs_cluster_name" {
  description = "ECS cluster name to use as the ECS_CLUSTER GitHub secret."
  value       = aws_ecs_cluster.memoryos.name
}

output "ecs_api_service_name" {
  description = "API ECS service name to use as the ECS_API_SERVICE GitHub secret."
  value       = aws_ecs_service.memoryos.name
}

output "ecs_worker_service_names" {
  description = "Comma-separated Celery worker ECS service names to use as the ECS_WORKER_SERVICES GitHub secret."
  value       = join(",", [for service in values(aws_ecs_service.celery_worker) : service.name])
}

output "ecs_private_subnet_ids" {
  description = "Comma-separated private subnet IDs to use as the ECS_PRIVATE_SUBNETS GitHub secret for one-off migration tasks."
  value       = join(",", [for subnet in aws_subnet.private : subnet.id])
}

output "ecs_security_group_id" {
  description = "ECS task security group ID to use as the ECS_SECURITY_GROUPS GitHub secret."
  value       = aws_security_group.ecs.id
}

output "redis_endpoint" {
  description = "ElastiCache Redis primary endpoint used by Celery."
  value       = aws_elasticache_replication_group.memoryos.primary_endpoint_address
}

output "database_url_secret_name" {
  description = "Secrets Manager name that must contain the Supabase DATABASE_URL."
  value       = aws_secretsmanager_secret.database_url.name
}

output "qdrant_url_secret_name" {
  description = "Secrets Manager name that must contain the Qdrant Cloud URL."
  value       = aws_secretsmanager_secret.qdrant_url.name
}

output "qdrant_api_key_secret_name" {
  description = "Secrets Manager name that must contain the Qdrant Cloud API key."
  value       = aws_secretsmanager_secret.qdrant_api_key.name
}

output "openai_api_key_secret_name" {
  description = "Secrets Manager name that must contain the OpenAI API key."
  value       = aws_secretsmanager_secret.openai_api_key.name
}
output "secret_key_secret_name" {
  description = "Secrets Manager name that must contain SECRET_KEY."
  value       = aws_secretsmanager_secret.secret_key.name
}

output "admin_secret_secret_name" {
  description = "Secrets Manager name that must contain ADMIN_SECRET."
  value       = aws_secretsmanager_secret.admin_secret.name
}

output "uui_session_secret_name" {
  description = "Secrets Manager name that must contain UUI_SESSION_SECRET."
  value       = aws_secretsmanager_secret.uui_session_secret.name
}

output "oauth_credential_encryption_key_secret_name" {
  description = "Secrets Manager name that must contain OAUTH_CREDENTIAL_ENCRYPTION_KEY."
  value       = aws_secretsmanager_secret.oauth_credential_encryption_key.name
}

output "smtp_pass_secret_name" {
  description = "Secrets Manager name that must contain SMTP_PASS."
  value       = aws_secretsmanager_secret.smtp_pass.name
}

