variable "aws_region" {
  description = "AWS region for the state bucket and its KMS key."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Project name used in KMS aliases and resource tags."
  type        = string
  default     = "memoryos"
}

variable "environment" {
  description = "Environment whose infrastructure state this bucket protects."
  type        = string
  default     = "staging"
}

variable "state_bucket_name" {
  description = "Globally unique S3 bucket name for Terraform state."
  type        = string
}
