output "state_bucket_name" {
  description = "Use this value in the main stack backend configuration."
  value       = aws_s3_bucket.terraform_state.bucket
}

output "state_kms_key_arn" {
  description = "Use this value as kms_key_id in the main stack backend configuration."
  value       = aws_kms_key.terraform_state.arn
}
