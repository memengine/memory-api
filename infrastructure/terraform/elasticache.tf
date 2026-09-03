resource "aws_security_group" "redis" {
  name        = "${var.project_name}-redis-sg"
  description = "Allow Redis access from ECS tasks"
  vpc_id      = aws_vpc.memoryos.id

  ingress {
    description     = "Redis from ECS"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-redis-sg"
  })
}

resource "aws_elasticache_subnet_group" "memoryos" {
  name       = "${var.project_name}-redis-subnets"
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-redis-subnets"
  })
}

resource "aws_elasticache_replication_group" "memoryos" {
  replication_group_id       = "${var.project_name}-redis"
  description                = "Redis replication group for MemoryOS"
  engine                     = "redis"
  engine_version             = var.redis_engine_version
  node_type                  = var.redis_node_type
  port                       = 6379
  parameter_group_name       = "default.redis7"
  automatic_failover_enabled = false
  multi_az_enabled           = false
  num_cache_clusters         = 1
  subnet_group_name          = aws_elasticache_subnet_group.memoryos.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = var.redis_transit_encryption_enabled
  auth_token                 = var.redis_auth_enabled ? var.redis_auth_token : null
  snapshot_retention_limit   = var.redis_snapshot_retention_limit
  snapshot_window            = var.redis_snapshot_retention_limit > 0 ? var.redis_snapshot_window : null

  lifecycle {
    precondition {
      condition     = !var.redis_auth_enabled || (var.redis_auth_token != null && length(var.redis_auth_token) >= 16)
      error_message = "Redis authentication requires a URL-safe token of at least 16 characters. Set it only through the sensitive TF_VAR_redis_auth_token environment variable."
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-redis"
  })
}

