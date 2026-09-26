from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_deploy_script_avoids_noop_revisions_and_bounds_github_history() -> None:
    script = (ROOT / ".github/scripts/deploy-ecs.sh").read_text(encoding="utf-8")

    assert 'if [[ "$current_definition" == "$desired_definition" ]]' in script
    assert '"$api_current_task_definition" != "$api_new_task_definition" || "$FORCE_DEPLOY" == "true"' in script
    assert 'FORCE_DEPLOY="${FORCE_DEPLOY:-false}"' in script
    assert "update_service_task_definition" in script
    assert "TASK_DEFINITION_RETENTION" in script
    assert "list-tags-for-resource" in script
    assert '"$managed_by" != "github-deploy"' in script
    assert "deregister-task-definition" in script


def test_terraform_does_not_roll_back_github_task_revisions() -> None:
    ecs = (ROOT / "infrastructure/terraform/ecs.tf").read_text(encoding="utf-8")

    assert ecs.count("ignore_changes = [task_definition]") == 3


def test_application_rollout_inventory_includes_celery_beat() -> None:
    outputs = (ROOT / "infrastructure/terraform/outputs.tf").read_text(encoding="utf-8")

    output_block = outputs.split('output "ecs_worker_service_names"', maxsplit=1)[1].split(
        "\n}\n", maxsplit=1
    )[0]
    assert "aws_ecs_service.celery_worker" in output_block
    assert "aws_ecs_service.celery_beat.name" in output_block


def test_live_staging_alias_is_not_automatically_deployed_on_push() -> None:
    workflow = (ROOT / ".github/workflows/deploy-staging-ecs.yml").read_text(
        encoding="utf-8"
    )

    assert "\n  push:" not in workflow
    assert "force_deploy:" in workflow
