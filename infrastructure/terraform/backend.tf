terraform {
  # Configure this after bootstrap-state has created the private S3 bucket:
  # terraform init -migrate-state -backend-config=backend.hcl
  backend "s3" {}
}
