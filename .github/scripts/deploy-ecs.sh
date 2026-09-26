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
TASK_DEFINITION_RETENTION="${TASK_DEFINITION_RETENTION:-10}"
FORCE_DEPLOY="${FORCE_DEPLOY:-false}"

if ! [[ "$TASK_DEFINITION_RETENTION" =~ ^[0-9]+$ ]] || (( TASK_DEFINITION_RETENTION < 2 )); then
  echo "TASK_DEFINITION_RETENTION must be an integer greater than or equal to 2." >&2
  exit 1
fi

if [[ "$FORCE_DEPLOY" != "true" && "$FORCE_DEPLOY" != "false" ]]; then
  echo "FORCE_DEPLOY must be true or false." >&2
  exit 1
fi

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

latest_task_definition_for_family() {
  local family="$1"

  aws ecs list-task-definitions \
    --family-prefix "$family" \
    --status ACTIVE \
    --sort DESC \
    --query 'taskDefinitionArns[0]' \
    --output text
}

prepare_task_definition_with_image() {
  local task_definition="$1"
  local rendered_file="$2"
  local family
  local latest_task_definition
  local current_definition
  local desired_definition

  family="$(aws ecs describe-task-definition \
    --task-definition "$task_definition" \
    --query 'taskDefinition.family' \
    --output text)"
  latest_task_definition="$(latest_task_definition_for_family "$family")"

  if [[ -z "$latest_task_definition" || "$latest_task_definition" == "None" ]]; then
    echo "No active task definition found for family ${family}." >&2
    exit 1
  fi

  aws ecs describe-task-definition \
    --task-definition "$latest_task_definition" \
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

  current_definition="$(jq -Sc '
    del(
      .taskDefinitionArn,
      .revision,
      .status,
      .requiresAttributes,
      .compatibilities,
      .registeredAt,
      .registeredBy
    )
  ' current-task-definition.json)"
  desired_definition="$(jq -Sc . "$rendered_file")"

  if [[ "$current_definition" == "$desired_definition" ]]; then
    echo "Reusing ${latest_task_definition}; image and application version are unchanged." >&2
    printf '%s' "$latest_task_definition"
    return 0
  fi

  echo "Registering a new ${family} revision for ${IMAGE_URI}." >&2
  aws ecs register-task-definition \
    --cli-input-json "file://${rendered_file}" \
    --tags key=ManagedBy,value=github-deploy key=Application,value=memoryos-api \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text
}

cleanup_task_definition_family() {
  local family="$1"
  local referenced_task_definitions="$2"
  local task_definitions
  local index
  local task_definition
  local managed_by

  read -ra task_definitions <<< "$(aws ecs list-task-definitions \
    --family-prefix "$family" \
    --status ACTIVE \
    --sort DESC \
    --query 'taskDefinitionArns' \
    --output text)"

  for index in "${!task_definitions[@]}"; do
    if (( index < TASK_DEFINITION_RETENTION )); then
      continue
    fi

    task_definition="${task_definitions[$index]}"
    if [[ " $referenced_task_definitions " == *" $task_definition "* ]]; then
      echo "Keeping referenced task definition ${task_definition}." >&2
      continue
    fi

    managed_by="$(aws ecs list-tags-for-resource \
      --resource-arn "$task_definition" \
      --query 'tags[?key==`ManagedBy`].value | [0]' \
      --output text)"
    if [[ "$managed_by" != "github-deploy" ]]; then
      echo "Keeping non-GitHub task definition ${task_definition}." >&2
      continue
    fi

    echo "Deregistering old unreferenced task definition ${task_definition}." >&2
    aws ecs deregister-task-definition \
      --task-definition "$task_definition" \
      --query 'taskDefinition.taskDefinitionArn' \
      --output text >/dev/null
  done
}

update_service_task_definition() {
  local service_name="$1"
  local current_task_definition="$2"
  local desired_task_definition="$3"
  local update_args=(
    ecs update-service
    --cluster "$ECS_CLUSTER"
    --service "$service_name"
    --task-definition "$desired_task_definition"
  )

  if [[ "$current_task_definition" == "$desired_task_definition" ]]; then
    update_args+=(--force-new-deployment)
  fi

  aws "${update_args[@]}" >/dev/null
}

run_migrations() {
  local task_definition="$1"
  local rendered_file="$2"
  local container_name
  local task_arn
  local exit_code
  local task_detail
  local stopped_reason
  local container_reason
  local container_status
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
    task_detail="$(aws ecs describe-tasks \
      --cluster "$ECS_CLUSTER" \
      --tasks "$task_arn" \
      --query 'tasks[0]' \
      --output json)"
    stopped_reason="$(jq -r '.stoppedReason // "unknown"' <<< "$task_detail")"
    container_reason="$(jq -r '.containers[0].reason // "unknown"' <<< "$task_detail")"
    container_status="$(jq -r '.containers[0].lastStatus // "unknown"' <<< "$task_detail")"

    echo "Migration task failed with exit code ${exit_code}. Task: ${task_arn}" >&2
    echo "Stopped reason: ${stopped_reason}" >&2
    echo "Container status: ${container_status}" >&2
    echo "Container reason: ${container_reason}" >&2
    print_task_logs "$task_arn" "$container_name" "$rendered_file" || true
    exit 1
  fi
}

print_task_logs() {
  local task_arn="$1"
  local container_name="$2"
  local rendered_file="$3"
  local task_id
  local log_group
  local log_region
  local stream_prefix
  local log_stream

  task_id="${task_arn##*/}"
  log_group="$(jq -r '.containerDefinitions[0].logConfiguration.options["awslogs-group"] // empty' "$rendered_file")"
  log_region="$(jq -r '.containerDefinitions[0].logConfiguration.options["awslogs-region"] // empty' "$rendered_file")"
  stream_prefix="$(jq -r '.containerDefinitions[0].logConfiguration.options["awslogs-stream-prefix"] // empty' "$rendered_file")"

  if [[ -z "$log_group" || -z "$stream_prefix" ]]; then
    echo "No awslogs configuration found on task definition; cannot tail migration logs." >&2
    return 0
  fi

  log_region="${log_region:-$AWS_REGION}"
  log_stream="${stream_prefix}/${container_name}/${task_id}"
  echo "Last migration logs from ${log_group}:${log_stream}" >&2
  aws logs get-log-events \
    --region "$log_region" \
    --log-group-name "$log_group" \
    --log-stream-name "$log_stream" \
    --limit 80 \
    --query 'events[].message' \
    --output text >&2 || true
}

echo "Deploying ${IMAGE_URI} to ECS cluster ${ECS_CLUSTER}"

api_current_task_definition="$(task_definition_for_service "$ECS_API_SERVICE")"
api_new_task_definition="$(prepare_task_definition_with_image "$api_current_task_definition" api-task-definition-rendered.json)"

services_to_wait=()
services_for_reference_check=("$ECS_API_SERVICE")
families_to_cleanup=("$(aws ecs describe-task-definition --task-definition "$api_new_task_definition" --query 'taskDefinition.family' --output text)")

if [[ "$api_current_task_definition" != "$api_new_task_definition" || "$FORCE_DEPLOY" == "true" ]]; then
  if [[ "$RUN_MIGRATIONS" == "true" ]]; then
    run_migrations "$api_new_task_definition" api-task-definition-rendered.json
  else
    echo "Skipping migrations because RUN_MIGRATIONS=${RUN_MIGRATIONS}"
  fi

  update_service_task_definition \
    "$ECS_API_SERVICE" \
    "$api_current_task_definition" \
    "$api_new_task_definition"
  services_to_wait+=("$ECS_API_SERVICE")
else
  echo "Skipping unchanged API service ${ECS_API_SERVICE}."
fi

if [[ -n "$ECS_WORKER_SERVICES" ]]; then
  IFS=',' read -ra worker_services <<< "$ECS_WORKER_SERVICES"
  for raw_service in "${worker_services[@]}"; do
    service_name="$(trim "$raw_service")"
    if [[ -z "$service_name" ]]; then
      continue
    fi

    echo "Updating worker service ${service_name}"
    worker_current_task_definition="$(task_definition_for_service "$service_name")"
    worker_new_task_definition="$(prepare_task_definition_with_image "$worker_current_task_definition" "worker-${service_name}-task-definition-rendered.json")"
    worker_family="$(aws ecs describe-task-definition --task-definition "$worker_new_task_definition" --query 'taskDefinition.family' --output text)"
    families_to_cleanup+=("$worker_family")
    services_for_reference_check+=("$service_name")

    if [[ "$worker_current_task_definition" == "$worker_new_task_definition" && "$FORCE_DEPLOY" != "true" ]]; then
      echo "Skipping unchanged service ${service_name}."
      continue
    fi

    update_service_task_definition \
      "$service_name" \
      "$worker_current_task_definition" \
      "$worker_new_task_definition"
    services_to_wait+=("$service_name")
  done
fi

if (( ${#services_to_wait[@]} > 0 )); then
  echo "Waiting for services to become stable: ${services_to_wait[*]}"
  aws ecs wait services-stable \
    --cluster "$ECS_CLUSTER" \
    --services "${services_to_wait[@]}"
else
  echo "All services already use the requested image and application version."
fi

referenced_task_definitions="$(aws ecs describe-services \
  --cluster "$ECS_CLUSTER" \
  --services "${services_for_reference_check[@]}" \
  --query 'services[].deployments[].taskDefinition' \
  --output text)"

for family in "${families_to_cleanup[@]}"; do
  cleanup_task_definition_family "$family" "$referenced_task_definitions"
done

echo "ECS deployment complete."
