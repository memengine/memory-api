resource "aws_kms_key" "memory_content" {
  description             = "MemoryOS ${var.environment} envelope encryption for governed memory content"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = merge(local.common_tags, {
    Name    = "${var.project_name}-${var.environment}-memory-content"
    Purpose = "memory-content-envelope-encryption"
  })
}

resource "aws_kms_alias" "memory_content" {
  name          = "alias/${var.project_name}-${var.environment}-memory-content"
  target_key_id = aws_kms_key.memory_content.key_id
}
