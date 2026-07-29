#!/usr/bin/env bash
set -euo pipefail

required_env=(
  AWS_REGION
  ECR_REPOSITORY
  ECR_REGISTRY
  IMAGE_URI
  ECS_CLUSTER
  ECS_API_SERVICE
  ECS_PRIVATE_SUBNETS
  ECS_SECURITY_GROUPS
)

for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
done

ECS_WORKER_SERVICES="${ECS_WORKER_SERVICES:-}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"
APP_VERSION="${APP_VERSION:-${GITHUB_SHA:-unknown}}"

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

json_list_from_csv() {
  local csv="$1"
  jq -cn --arg csv "$csv" '$csv | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))'
}

task_definition_for_service() {
  local service_name="$1"
  aws ecs describe-services \
    --cluster "$ECS_CLUSTER" \
    --services "$service_name" \
    --query 'services[0].taskDefinition' \
    --output text
}

register_task_definition_with_image() {
  local task_definition="$1"
  local rendered_file="$2"

  aws ecs describe-task-definition \
    --task-definition "$task_definition" \
    --query 'taskDefinition' \
    --output json > current-task-definition.json

  jq \
    --arg image_uri "$IMAGE_URI" \
    --arg app_version "$APP_VERSION" \
    '
    del(
      .taskDefinitionArn,
      .revision,
      .status,
      .requiresAttributes,
      .compatibilities,
      .registeredAt,
      .registeredBy
    )
    | .containerDefinitions |= map(
        .image = $image_uri
        | .environment = (
            ((.environment // []) | map(select(.name != "APP_VERSION")))
            + [{"name":"APP_VERSION","value":$app_version}]
          )
      )
    ' current-task-definition.json > "$rendered_file"

  aws ecs register-task-definition \
    --cli-input-json "file://${rendered_file}" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text
}

run_migrations() {
  local task_definition="$1"
  local rendered_file="$2"
  local container_name
  local task_arn
  local exit_code
  local subnet_json
  local security_group_json
  local network_config

  container_name="$(jq -r '.containerDefinitions[0].name' "$rendered_file")"
  subnet_json="$(json_list_from_csv "$ECS_PRIVATE_SUBNETS")"
  security_group_json="$(json_list_from_csv "$ECS_SECURITY_GROUPS")"
  network_config="$(jq -cn \
    --argjson subnets "$subnet_json" \
    --argjson securityGroups "$security_group_json" \
    '{awsvpcConfiguration:{subnets:$subnets,securityGroups:$securityGroups,assignPublicIp:"DISABLED"}}'
  )"

  echo "Running Alembic migration task with ${task_definition}"
  task_arn="$(aws ecs run-task \
    --cluster "$ECS_CLUSTER" \
    --launch-type FARGATE \
    --task-definition "$task_definition" \
    --network-configuration "$network_config" \
    --overrides "$(jq -cn \
      --arg container "$container_name" \
      '{containerOverrides:[{name:$container,command:["alembic","-c","/app/alembic.ini","upgrade","head"]}]}'
    )" \
    --query 'tasks[0].taskArn' \
    --output text)"

  if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
    echo "Failed to start migration task." >&2
    exit 1
  fi

  aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$task_arn"

  exit_code="$(aws ecs describe-tasks \
    --cluster "$ECS_CLUSTER" \
    --tasks "$task_arn" \
    --query 'tasks[0].containers[0].exitCode' \
    --output text)"

  if [[ "$exit_code" != "0" ]]; then
    echo "Migration task failed with exit code ${exit_code}. Task: ${task_arn}" >&2
    exit 1
  fi
}

echo "Deploying ${IMAGE_URI} to ECS cluster ${ECS_CLUSTER}"

api_current_task_definition="$(task_definition_for_service "$ECS_API_SERVICE")"
api_new_task_definition="$(register_task_definition_with_image "$api_current_task_definition" api-task-definition-rendered.json)"

if [[ "$RUN_MIGRATIONS" == "true" ]]; then
  run_migrations "$api_new_task_definition" api-task-definition-rendered.json
else
  echo "Skipping migrations because RUN_MIGRATIONS=${RUN_MIGRATIONS}"
fi

aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_API_SERVICE" \
  --task-definition "$api_new_task_definition" \
  --force-new-deployment >/dev/null

services_to_wait=("$ECS_API_SERVICE")

if [[ -n "$ECS_WORKER_SERVICES" ]]; then
  IFS=',' read -ra worker_services <<< "$ECS_WORKER_SERVICES"
  for raw_service in "${worker_services[@]}"; do
    service_name="$(trim "$raw_service")"
    if [[ -z "$service_name" ]]; then
      continue
    fi

    echo "Updating worker service ${service_name}"
    worker_current_task_definition="$(task_definition_for_service "$service_name")"
    worker_new_task_definition="$(register_task_definition_with_image "$worker_current_task_definition" "worker-${service_name}-task-definition-rendered.json")"

    aws ecs update-service \
      --cluster "$ECS_CLUSTER" \
      --service "$service_name" \
      --task-definition "$worker_new_task_definition" \
      --force-new-deployment >/dev/null

    services_to_wait+=("$service_name")
  done
fi

echo "Waiting for services to become stable: ${services_to_wait[*]}"
aws ecs wait services-stable \
  --cluster "$ECS_CLUSTER" \
  --services "${services_to_wait[@]}"

echo "ECS deployment complete."