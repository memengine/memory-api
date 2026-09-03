# Postgres is intentionally not managed by this Terraform stack.
#
# Production uses Supabase-managed Postgres. ECS receives the Supabase
# connection string through the DATABASE_URL secret defined in secrets.tf.
# This keeps database backups, pooling, dashboard access, and upgrades with
# Supabase instead of creating an AWS RDS instance here.
