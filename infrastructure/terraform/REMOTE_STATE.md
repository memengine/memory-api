# Remote Terraform state migration

The existing MemoryOS Terraform state is local. Migrate it to the bootstrap
bucket before applying changes to the main infrastructure stack.

## 1. Bootstrap the state bucket

From `infrastructure/terraform/bootstrap-state`, run:

```powershell
terraform init
terraform plan -var 'environment=production' -var 'state_bucket_name=memoryos-terraform-state-<unique-suffix>'
terraform apply -var 'environment=production' -var 'state_bucket_name=memoryos-terraform-state-<unique-suffix>'
```

The bootstrap creates only a private S3 bucket, a rotating KMS key, S3 versioning,
public-access blocks, and a policy that requires TLS. It has `prevent_destroy`.

## 2. Preserve a local recovery copy

From `infrastructure/terraform`, before migrating:

```powershell
Copy-Item terraform.tfstate terraform.tfstate.pre-remote.backup
```

The backup is ignored by Git. Do not upload it, email it, or commit it.

## 3. Configure and migrate

```powershell
Copy-Item backend.hcl.example backend.hcl
```

Replace the bucket and KMS key placeholders with bootstrap outputs. The existing
`memoryos` stack uses the `production` key shown in the example. A newly created
staging stack must use a separate key such as `memoryos/staging/terraform.tfstate`.
Then run:

```powershell
terraform init -migrate-state -backend-config=backend.hcl
terraform state list
```

Terraform will ask before copying local state to S3. Answer `yes` only after
confirming the bucket name and intended state key.

## Operating rule

After migration, run `terraform plan` and `terraform apply` only from this
configured main directory. Do not edit or delete Terraform-managed AWS resources
manually. S3 versioning provides state recovery, while the native S3 lockfile
prevents concurrent applies.
