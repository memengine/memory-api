# Deployment files

The root Compose file builds with `deploy/Dockerfile`. Compose test overrides
and the dedicated scale and contributor profiles live here. The Docker build
context remains the repository root, and containers still load migrations from
`/app/alembic.ini`.

The CodeBuild project must select `deploy/buildspec.ecr.yml` as its buildspec
path if it uses this file. That selection lives in AWS CodeBuild. Check it
before rolling out this layout to a CodeBuild project.

The local beta has a separate [Docker guide](../beta/README.md) and keeps its
dedicated Dockerfile and Compose configuration together.
