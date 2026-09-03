locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Service     = "memoryos-api"
  }

  public_subnet_map = {
    for idx, cidr in var.public_subnet_cidrs : tostring(idx) => cidr
  }

  private_subnet_map = {
    for idx, cidr in var.private_subnet_cidrs : tostring(idx) => cidr
  }
}