from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_NETWORK = "manus-network"
DATA_NETWORK = "manus-data-network"
CHECKOUT_ACTION = (
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
)
QEMU_ACTION = (
    "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130"
)
BUILDX_ACTION = (
    "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f"
)
BUILD_PUSH_ACTION = (
    "docker/build-push-action@ca052bb54ab0790a636c9b5f226502c73d547a25"
)
SSH_ACTION = (
    "appleboy/ssh-action@029f5b4aeeeb58fdfe1410a5d17f967dacf36262"
)


def _package_lock_resolved_urls(value) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        resolved = value.get("resolved")
        if isinstance(resolved, str):
            urls.append(resolved)
        for nested in value.values():
            urls.extend(_package_lock_resolved_urls(nested))
    elif isinstance(value, list):
        for nested in value:
            urls.extend(_package_lock_resolved_urls(nested))
    return urls


def _environment_map(service: dict) -> dict[str, str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}

    result: dict[str, str] = {}
    for item in environment:
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


@pytest.mark.parametrize(
    "compose_name",
    [
        "docker-compose.yml",
        "docker-compose-development.yml",
        "docker-compose-example.yml",
    ],
)
def test_redis_security_state_is_persistent_and_non_evicting(compose_name: str):
    compose = yaml.safe_load((PROJECT_ROOT / compose_name).read_text())
    redis = compose["services"]["redis"]
    command = redis["command"]
    command_text = " ".join(command) if isinstance(command, list) else command

    assert "--appendonly yes" in command_text
    assert "--appendfsync everysec" in command_text
    assert "--maxmemory-policy noeviction" in command_text
    assert any(volume.endswith(":/data") for volume in redis["volumes"])

    volume_name = next(
        volume.split(":", 1)[0]
        for volume in redis["volumes"]
        if volume.endswith(":/data")
    )
    assert volume_name in compose["volumes"]


@pytest.mark.parametrize(
    "compose_name",
    [
        "docker-compose.yml",
        "docker-compose-development.yml",
        "docker-compose-example.yml",
    ],
)
def test_user_controlled_runtimes_are_isolated_from_datastores(compose_name: str):
    compose = yaml.safe_load((PROJECT_ROOT / compose_name).read_text())
    services = compose["services"]
    runtime_network_name = (
        "manus-network-dev"
        if compose_name == "docker-compose-development.yml"
        else RUNTIME_NETWORK
    )

    assert compose["networks"][DATA_NETWORK]["internal"] is True
    expected_runtime_network = (
        "${CORE_RESOURCE_PREFIX:-manus}-network"
        if compose_name == "docker-compose.yml"
        else (
            "${DEV_RESOURCE_PREFIX:-manus}-network-dev"
            if compose_name == "docker-compose-development.yml"
            else runtime_network_name
        )
    )
    assert compose["networks"][RUNTIME_NETWORK]["name"] == expected_runtime_network
    assert set(services["backend"]["networks"]) == {
        RUNTIME_NETWORK,
        DATA_NETWORK,
    }
    assert services["backend"]["sysctls"] == {
        "net.ipv4.ip_forward": "0",
        "net.ipv4.conf.all.forwarding": "0",
        "net.ipv4.conf.default.forwarding": "0",
        "net.ipv6.conf.all.forwarding": "0",
        "net.ipv6.conf.default.forwarding": "0",
    }
    assert set(services["mongodb"]["networks"]) == {DATA_NETWORK}
    assert set(services["redis"]["networks"]) == {DATA_NETWORK}

    services_on_data_network = {
        name
        for name, service in services.items()
        if DATA_NETWORK in service.get("networks", [])
    }
    assert services_on_data_network == {"backend", "mongodb", "redis"}

    for runtime_name in ("sandbox", "claw"):
        assert set(services[runtime_name]["networks"]) == {RUNTIME_NETWORK}

    backend_environment = _environment_map(services["backend"])
    expected_configured_runtime_network = (
        "${SANDBOX_NETWORK:-manus-network}"
        if compose_name == "docker-compose.yml"
        else (
            "${DEV_SANDBOX_NETWORK:-manus-network-dev}"
            if compose_name == "docker-compose-development.yml"
            else runtime_network_name
        )
    )
    expected_configured_claw_network = (
        "${CLAW_NETWORK:-manus-network}"
        if compose_name == "docker-compose.yml"
        else (
            "${DEV_CLAW_NETWORK:-manus-network-dev}"
            if compose_name == "docker-compose-development.yml"
            else runtime_network_name
        )
    )
    assert (
        backend_environment["SANDBOX_NETWORK"]
        == expected_configured_runtime_network
    )
    assert backend_environment["CLAW_NETWORK"] == expected_configured_claw_network
    assert backend_environment["RUNTIME_NETWORK_ISOLATION"] == "true"
    expected_deployment = (
        "${RUNTIME_DEPLOYMENT_ID:-ai-manus-dev}"
        if compose_name == "docker-compose-development.yml"
        else "${RUNTIME_DEPLOYMENT_ID:-ai-manus}"
    )
    assert backend_environment["RUNTIME_DEPLOYMENT_ID"] == expected_deployment
    if compose_name != "docker-compose-development.yml":
        assert backend_environment["CLAW_PUBLISH_HOST_PORTS"] == "false"
    if compose_name == "docker-compose-development.yml":
        assert backend_environment["SANDBOX_PROVIDER"] == "docker"


def test_fork_deployment_worker_bridges_runtime_and_data_networks_safely():
    workflow_text = (
        PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml"
    ).read_text()
    workflow = yaml.safe_load(workflow_text)
    ssh_step = next(
        step
        for step in workflow["jobs"]["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    deploy_script = ssh_step["with"]["script"]
    override = deploy_script.split('cat > "$incoming_override" <<EOF', 1)[1]
    override = override.split("\nEOF", 1)[0]
    compose_override = yaml.safe_load(textwrap.dedent(override))
    worker = compose_override["services"]["worker"]

    assert set(worker["networks"]) == {RUNTIME_NETWORK, DATA_NETWORK}
    assert worker["restart"] == "unless-stopped"
    assert worker["command"] == ["./start_worker.sh"]
    assert set(worker["sysctls"]) == {
        "net.ipv4.ip_forward=0",
        "net.ipv4.conf.all.forwarding=0",
        "net.ipv4.conf.default.forwarding=0",
        "net.ipv6.conf.all.forwarding=0",
        "net.ipv6.conf.default.forwarding=0",
    }
    worker_environment = _environment_map(worker)
    assert worker_environment["SANDBOX_NETWORK"] == RUNTIME_NETWORK
    assert worker_environment["CLAW_NETWORK"] == RUNTIME_NETWORK
    assert worker_environment["RUNTIME_DEPLOYMENT_ID"] == "ai-manus"
    assert worker_environment["MONGODB_URI"] == "mongodb://mongodb:27017"
    assert worker_environment["REDIS_HOST"] == "redis"
    assert worker_environment["REDIS_PORT"] == "6379"

    health_command = worker["healthcheck"]["test"][1].replace("\\$", "$")
    assert "/app/.venv/bin/celery" in health_command
    assert "inspect ping" in health_command
    assert 'celery@$$(hostname)' in health_command


def test_fork_worker_internal_datastore_addresses_override_polluted_env(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose is required for Compose merge verification")
    compose_version = subprocess.run(
        [docker, "compose", "version"],
        capture_output=True,
        text=True,
    )
    if compose_version.returncode != 0:
        pytest.skip("docker compose plugin is required for merge verification")

    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    ssh_step = next(
        step
        for step in workflow["jobs"]["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    deploy_script = ssh_step["with"]["script"]
    override = deploy_script.split('cat > "$incoming_override" <<EOF', 1)[1]
    override = override.split("\nEOF", 1)[0]
    worker = yaml.safe_load(textwrap.dedent(override))["services"]["worker"]

    (tmp_path / ".env").write_text(
        "MONGODB_URI=mongodb://127.0.0.1:27017\n"
        "REDIS_HOST=127.0.0.1\n"
        "REDIS_PORT=6383\n"
    )
    (tmp_path / "base.yml").write_text(
        "services:\n"
        "  worker:\n"
        "    image: busybox:latest\n"
        "    env_file:\n"
        "      - .env\n"
    )
    (tmp_path / "override.yml").write_text(
        yaml.safe_dump(
            {"services": {"worker": {"environment": worker["environment"]}}},
            sort_keys=False,
        )
    )
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(tmp_path / ".env"),
            "-f",
            str(tmp_path / "base.yml"),
            "-f",
            str(tmp_path / "override.yml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = json.loads(result.stdout)["services"]["worker"]["environment"]
    assert environment["MONGODB_URI"] == "mongodb://mongodb:27017"
    assert environment["REDIS_HOST"] == "redis"
    assert environment["REDIS_PORT"] == "6379"


def test_fork_deployment_transfers_and_rolls_back_both_compose_files():
    workflow_text = (
        PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml"
    ).read_text()
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["deploy-fork"]["steps"]

    assert any(step.get("uses") == CHECKOUT_ACTION for step in steps)
    prepare = next(step for step in steps if step.get("id") == "deploy")
    assert "base64 < docker-compose.yml" in prepare["run"]
    ssh_step = next(
        step for step in steps if step.get("uses") == SSH_ACTION
    )
    assert "COMPOSE_B64" in ssh_step["env"]
    assert "COMPOSE_B64" in ssh_step["with"]["envs"].split(",")

    deploy_script = ssh_step["with"]["script"]
    assert 'previous_compose="$(mktemp)"' in deploy_script
    assert 'previous_override="$(mktemp)"' in deploy_script
    assert 'restore_file "$previous_compose" docker-compose.yml' in deploy_script
    assert (
        'restore_file "$previous_override" docker-compose.deploy.yml'
        in deploy_script
    )
    assert 'compose stop worker' in deploy_script
    assert 'docker inspect --format' in deploy_script
    assert 'compose config -q' in deploy_script


def test_multiarch_build_registers_emulation_before_buildx():
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    steps = workflow["jobs"]["build-and-push"]["steps"]
    uses = [step.get("uses") for step in steps]

    qemu_index = uses.index(QEMU_ACTION)
    buildx_index = uses.index(BUILDX_ACTION)
    assert qemu_index < buildx_index
    build_step = next(
        step for step in steps if step.get("uses") == BUILD_PUSH_ACTION
    )
    assert build_step["with"]["platforms"] == "linux/amd64,linux/arm64"


def test_server_datastore_maintenance_is_exact_gated_and_backed_up():
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    jobs = workflow["jobs"]
    preflight = jobs["server-maintenance-preflight"]
    maintenance = jobs["server-maintenance"]

    for job_name in ("frontend-smoke", "backend-unit-security", "sandbox-unit"):
        assert "inputs.server_maintenance == 'none'" in jobs[job_name]["if"]

    assert preflight["environment"] == "fork-production"
    authorization = next(
        step
        for step in preflight["steps"]
        if step.get("id") == "authorization"
    )
    assert authorization["env"]["RECOVERY_APPROVED_SHA"] == (
        "${{ vars.FORK_DATASTORE_RECOVERY_APPROVED_SHA }}"
    )
    assert authorization["env"]["SERVER_HOST_FINGERPRINT"] == (
        "${{ secrets.SERVER_HOST_FINGERPRINT }}"
    )
    assert '"$RECOVERY_APPROVED_SHA" != "$GITHUB_SHA"' in authorization["run"]
    assert "recover-datastores" in authorization["run"]
    assert "43.156.115.199" in authorization["run"]
    assert "cannot be combined with image publication or deployment" in (
        authorization["run"]
    )

    assert maintenance["needs"] == "server-maintenance-preflight"
    assert maintenance["environment"] == "fork-production"
    assert maintenance["concurrency"]["group"] == (
        "fork-production-${{ github.repository }}"
    )
    ssh_step = next(
        step for step in maintenance["steps"] if step.get("uses") == SSH_ACTION
    )
    assert ssh_step["with"]["fingerprint"] == (
        "${{ secrets.SERVER_HOST_FINGERPRINT }}"
    )
    script = ssh_step["with"]["script"]
    assert "docker ps -aq --no-trunc" in script
    assert "com.docker.compose.service=${service}" in script
    assert 'docker volume inspect --format \'{{.Mountpoint}}\'' in script
    assert "sudo -n tar" in script
    assert "sha256sum" in script
    assert 'docker start "$mongodb_id"' in script
    assert 'docker start "$redis_id"' in script
    assert 'docker stop --time 120 "$mongodb_id"' in script
    assert 'docker stop --time 120 "$redis_id"' in script
    assert "container_state_fingerprint" in script
    assert "container_running_fingerprint" in script
    assert 'docker ps -aq --no-trunc --filter "volume=${volume_name}"' in script
    assert "volume_has_exclusive_consumer" in script
    assert "volume gained an unexpected consumer during backup" in script
    assert "volume was no longer exclusive after final validation" in script
    assert "started or changed during its offline backup" in script
    assert "backup checksum manifest" in script
    assert 'sudo -n sync -f "$backup_dir"' in script
    assert "ROLLBACK_FAILED service=mongodb final_state_not_exited" in script
    assert "ROLLBACK_FAILED service=redis final_state_not_exited" in script
    assert "Redis final PING did not return PONG" in script
    assert "Redis final DBSIZE was not numeric" in script
    assert "for stability_attempt in $(seq 1 30); do" in script
    assert "runtime changed during the stability window" in script
    assert "restarted while recovering" in script
    assert 'timeout 5s docker exec "$mongodb_id"' in script
    assert 'timeout 5s docker exec "$redis_id"' in script
    assert 'timeout 30s docker start "$mongodb_id"' in script
    assert "recovery_elapsed_seconds" in script
    assert "consumed the recovery time budget" in script
    assert maintenance["timeout-minutes"] == 90
    assert ssh_step["with"]["command_timeout"] == "80m"
    post_start = script.split('timeout 30s docker start "$mongodb_id"', 1)[1]
    for line in post_start.splitlines():
        if any(
            command in line
            for command in (
                "docker exec ",
                "docker inspect ",
                "docker ps ",
                "docker start ",
                "docker stop ",
            )
        ):
            assert "timeout " in line
    assert script.index("mongodb_start_attempted=true") < script.index(
        'docker start "$mongodb_id"'
    )
    assert script.index("redis_start_attempted=true") < script.index(
        'docker start "$redis_id"'
    )
    health_loop = script.split("for attempt in $(seq 1 90); do", 1)[1].split(
        "done", 1
    )[0]
    assert health_loop.index("mongodb_healthy=false") < health_loop.index(
        'docker exec "$mongodb_id"'
    )
    assert health_loop.index("redis_healthy=false") < health_loop.index(
        'docker exec "$redis_id"'
    )
    assert 'echo "RECOVERY redis_dbsize=$(' not in script
    assert "MongoDB container identity changed after audit" in script
    assert "Redis container identity changed after audit" in script
    for forbidden in (
        "docker rm",
        "docker container rm",
        "docker compose up",
        "docker volume rm",
    ):
        assert forbidden not in script

    deploy_preflight = jobs["deploy-preflight"]
    deploy_secret_step = next(
        step for step in deploy_preflight["steps"] if step.get("id") == "secrets"
    )
    assert deploy_secret_step["env"]["SERVER_HOST_FINGERPRINT"] == (
        "${{ secrets.SERVER_HOST_FINGERPRINT }}"
    )
    deploy_ssh_step = next(
        step
        for step in jobs["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    assert deploy_ssh_step["with"]["fingerprint"] == (
        "${{ secrets.SERVER_HOST_FINGERPRINT }}"
    )


def test_workflow_actions_are_pinned_to_immutable_commits():
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )

    action_refs = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]

    assert action_refs
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", action_ref)
        for action_ref in action_refs
    )


def test_fork_deployment_uses_approved_sha_and_immutable_image_digests():
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    publish_preflight = workflow["jobs"]["publish-preflight"]
    preflight = workflow["jobs"]["deploy-preflight"]
    publish = workflow["jobs"]["publish-fork-images"]
    deploy = workflow["jobs"]["deploy-fork"]

    assert "environment" not in publish_preflight
    authorization_step = next(
        step
        for step in publish_preflight["steps"]
        if step.get("id") == "authorization"
    )
    assert authorization_step["env"]["EXPECTED_SHA"] == (
        "${{ inputs.expected_sha }}"
    )
    assert '"$EXPECTED_SHA" != "$GITHUB_SHA"' in authorization_step["run"]
    assert publish["needs"] == "publish-preflight"
    assert publish["if"] == (
        "needs.publish-preflight.outputs.enabled == 'true'"
    )
    assert publish["permissions"]["packages"] == "write"

    assert preflight["environment"] == "fork-production"
    assert preflight["needs"] == "publish-fork-images"
    # Environment-scoped vars are not available while GitHub evaluates a
    # job-level ``if``.  The job must enter the environment first and enforce
    # the explicit enable gate inside its preflight step.
    assert "vars.ENABLE_FORK_DEPLOY" not in preflight["if"]
    approval_step = next(
        step for step in preflight["steps"] if step.get("id") == "secrets"
    )
    assert approval_step["env"]["ENABLE_FORK_DEPLOY"] == (
        "${{ vars.ENABLE_FORK_DEPLOY }}"
    )
    assert '"$ENABLE_FORK_DEPLOY" != "true"' in approval_step["run"]
    assert approval_step["env"]["APPROVED_SHA"] == (
        "${{ vars.FORK_DEPLOY_APPROVED_SHA }}"
    )
    assert '"$APPROVED_SHA" != "$GITHUB_SHA"' in approval_step["run"]

    build_step = next(step for step in publish["steps"] if step.get("id") == "build")
    assert build_step["uses"] == BUILD_PUSH_ACTION
    digest_step = next(
        step
        for step in publish["steps"]
        if step.get("name") == "Record immutable fork image digest"
    )
    assert digest_step["env"]["IMAGE_DIGEST"] == (
        "${{ steps.build.outputs.digest }}"
    )
    assert "sha256:[0-9a-f]{64}" in digest_step["run"]

    assert deploy["needs"] == "deploy-preflight"
    assert "inputs.deploy_fork == true" in deploy["if"]
    assert "needs.deploy-preflight.outputs.enabled == 'true'" in deploy["if"]
    prepare_step = next(step for step in deploy["steps"] if step.get("id") == "deploy")
    assert "ai-manus-${component}@${digest}" in prepare_step["run"]
    ssh_step = next(step for step in deploy["steps"] if step.get("uses") == SSH_ACTION)
    deploy_script = ssh_step["with"]["script"]
    for image_variable in (
        "FRONTEND_IMAGE",
        "BACKEND_IMAGE",
        "SANDBOX_IMAGE",
        "CLAW_IMAGE",
        "MOCKSERVER_IMAGE",
    ):
        assert image_variable in ssh_step["env"]
        assert image_variable in ssh_step["with"]["envs"].split(",")
    assert "image: ${FRONTEND_IMAGE}" in deploy_script
    assert "image: ${BACKEND_IMAGE}" in deploy_script
    assert "image: ${SANDBOX_IMAGE}" in deploy_script
    assert "image: ${CLAW_IMAGE}" in deploy_script
    assert "image: ${MOCKSERVER_IMAGE}" in deploy_script
    assert 'hotpatch_image="$SANDBOX_IMAGE"' in deploy_script
    assert ".RepoDigests" in deploy_script
    assert "ai-manus-frontend:${IMAGE_TAG}" not in deploy_script
    assert "ai-manus-backend:${IMAGE_TAG}" not in deploy_script
    assert "ai-manus-sandbox:${IMAGE_TAG}" not in deploy_script


def test_fork_deployment_fails_closed_and_bounds_hotpatch_state():
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    ssh_step = next(
        step
        for step in workflow["jobs"]["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    deploy_script = ssh_step["with"]["script"]

    assert "require_docker() {" in deploy_script
    assert "timeout 5s docker info --format" in deploy_script
    assert "command -v timeout" in deploy_script
    assert 'exec 9>"$DEPLOY_PATH/.fork-deploy.lock"' in deploy_script
    assert "flock -n 9" in deploy_script
    docker_readiness_calls = [
        index
        for index in range(len(deploy_script))
        if deploy_script.startswith("\nrequire_docker\n", index)
    ]
    assert len(docker_readiness_calls) == 2
    assert docker_readiness_calls[0] < deploy_script.index(
        'previous_compose="$(mktemp)"'
    )
    assert (
        deploy_script.index(
            'docker compose -p ai-manus -f "$incoming_compose" '
            '-f "$incoming_override" config -q'
        )
        < docker_readiness_calls[1]
        < deploy_script.index("\nverify_stateful_pre_mutation\n")
        < deploy_script.index("changed=true")
    )

    retained_guard = (
        'docker container inspect "$HOTPATCH_TARGET" >/dev/null 2>&1 && '
        "\\\n  "
        '[ "${HOTPATCH_ENABLED:-false}" != true ]'
    )
    assert retained_guard in deploy_script
    assert deploy_script.index(retained_guard) < deploy_script.index("changed=true")
    assert "maximum_entries=200000" in deploy_script
    assert "timeout 30s find . -mindepth 1 -xdev" in deploy_script
    assert 'head -z -n "$maximum_fields"' in deploy_script
    assert "exceeded ${maximum_entries} entries" in deploy_script

    assert 'registry_docker_config="$(mktemp -d /tmp/ai-manus-registry.XXXXXX)"' in (
        deploy_script
    )
    assert 'export DOCKER_CONFIG="$registry_docker_config"' in deploy_script
    assert deploy_script.count("cleanup_registry_login") >= 3
    assert 'docker logout "$REGISTRY"' in deploy_script
    assert (
        'mongodb_id_before="$(restore_compose ps -q mongodb 2>/dev/null || true)"'
        not in deploy_script
    )
    assert (
        'redis_id_before="$(restore_compose ps -q redis 2>/dev/null || true)"'
        not in deploy_script
    )
    assert 'if ! mongodb_id_before="$(restore_compose ps -q mongodb)"; then' in (
        deploy_script
    )
    assert 'if ! redis_id_before="$(restore_compose ps -q redis)"; then' in (
        deploy_script
    )
    assert (
        "docker ps -aq \\\n    --filter label=com.docker.compose.project=ai-manus"
        in deploy_script
    )
    assert (
        "the existing Compose project cannot prove both MongoDB and Redis "
        "are running from the current configuration"
        in deploy_script
    )


@pytest.mark.parametrize(
    (
        "has_compose",
        "project_ids",
        "mongodb_id",
        "redis_id",
        "hotpatch_enabled",
        "docker_ready",
        "ps_fails",
        "expected_success",
        "expected_message",
    ),
    [
        (False, "", "", "", False, True, False, True, "project="),
        (
            False,
            "stale-container",
            "",
            "",
            False,
            True,
            False,
            False,
            "cannot prove both MongoDB and Redis",
        ),
        (True, "", "", "", False, True, False, True, "project="),
        (
            True,
            "backend-id\nmongodb-id\nredis-id",
            "mongodb-id",
            "redis-id",
            False,
            True,
            False,
            True,
            "mongodb=mongodb-id redis=redis-id",
        ),
        (
            True,
            "backend-id\nmongodb-id",
            "mongodb-id",
            "",
            False,
            True,
            False,
            False,
            "cannot prove both MongoDB and Redis",
        ),
        (
            True,
            "mongodb-id\nredis-id",
            "",
            "",
            False,
            True,
            False,
            False,
            "cannot prove both MongoDB and Redis",
        ),
        (
            True,
            "",
            "",
            "",
            True,
            True,
            False,
            False,
            "hotpatch requires existing MongoDB and Redis",
        ),
        (
            True,
            "",
            "",
            "",
            False,
            True,
            True,
            False,
            "Could not query the existing Compose project",
        ),
        (
            False,
            "",
            "",
            "",
            False,
            False,
            False,
            False,
            "Docker daemon remained unavailable",
        ),
    ],
)
def test_fork_deployment_datastore_preflight_distinguishes_bootstrap_from_partial_state(
    tmp_path,
    has_compose: bool,
    project_ids: str,
    mongodb_id: str,
    redis_id: str,
    hotpatch_enabled: bool,
    docker_ready: bool,
    ps_fails: bool,
    expected_success: bool,
    expected_message: str,
):
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    ssh_step = next(
        step
        for step in workflow["jobs"]["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    deploy_script = ssh_step["with"]["script"]
    preflight_script = deploy_script.split("\nverify_expected_container() {", 1)[0]
    preflight_script += (
        "\nprintf 'mongodb=%s redis=%s project=%s\\n' "
        '"$mongodb_id_before" "$redis_id_before" "$project_container_ids_before"\n'
    )

    deploy_path = tmp_path / "deploy"
    deploy_path.mkdir()
    preflight_script = preflight_script.replace(
        "/home/ubuntu/ai-manus",
        f'"{deploy_path}"',
    )
    if has_compose:
        (deploy_path / "docker-compose.yml").write_text(
            "services:\n"
            "  mongodb:\n"
            "    image: mongo:7.0\n"
            "  redis:\n"
            "    image: redis:7.0\n"
        )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  'compose version') exit 0 ;;\n"
        "  'info --format {{.ServerVersion}}')\n"
        "    if [ \"${DOCKER_READY}\" = 1 ]; then\n"
        "      printf '28.2.2\\n'\n"
        "      exit 0\n"
        "    fi\n"
        "    printf 'daemon unavailable\\n' >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "  'container inspect '* ) exit 1 ;;\n"
        "  'ps -aq --filter label=com.docker.compose.project=ai-manus')\n"
        "    if [ \"${PS_FAILS}\" = 1 ]; then\n"
        "      printf 'daemon query failed\\n' >&2\n"
        "      exit 1\n"
        "    fi\n"
        "    printf '%s\\n' \"${PROJECT_IDS}\"\n"
        "    ;;\n"
        "  *' config -q') exit 0 ;;\n"
        "  *' ps -q mongodb') printf '%s\\n' \"${MONGODB_ID}\" ;;\n"
        "  *' ps -q redis') printf '%s\\n' \"${REDIS_ID}\" ;;\n"
        "  *) printf 'unexpected docker command: %s\\n' \"$*\" >&2; exit 97 ;;\n"
        "esac\n"
    )
    fake_docker.chmod(0o755)
    fake_timeout = fake_bin / "timeout"
    fake_timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    fake_timeout.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n")
    fake_sleep.chmod(0o755)
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n")
    fake_flock.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_PATH": str(deploy_path),
        "HOTPATCH_ENABLED": "true" if hotpatch_enabled else "false",
        "HOTPATCH_TARGET": "ai-manus-sandbox-ab145dc2",
        "DEPLOY_REF": "refs/tags/deploy-test-20260721-artifact-fix-01",
        "PUBLIC_HOST": "43.156.115.199",
        "FRONTEND_HOST_PORT": "18081",
        "DOCKER_READY": "1" if docker_ready else "0",
        "PS_FAILS": "1" if ps_fails else "0",
        "PROJECT_IDS": project_ids,
        "MONGODB_ID": mongodb_id,
        "REDIS_ID": redis_id,
    }
    result = subprocess.run(
        ["/bin/sh"],
        input=preflight_script,
        cwd=deploy_path,
        env=env,
        text=True,
        capture_output=True,
    )

    combined_output = result.stdout + result.stderr
    assert (result.returncode == 0) is expected_success
    assert expected_message in combined_output


@pytest.mark.parametrize(
    (
        "had_previous_compose",
        "mongodb_id_before",
        "redis_id_before",
        "current_project_ids",
        "current_mongodb_id",
        "current_redis_id",
        "expected_success",
        "expected_message",
    ),
    [
        (False, "", "", "", "", "", True, ""),
        (
            False,
            "",
            "",
            "new-container",
            "",
            "",
            False,
            "appeared after bootstrap preflight",
        ),
        (True, "", "", "", "", "", True, ""),
        (
            True,
            "",
            "",
            "new-container",
            "",
            "",
            False,
            "appeared after bootstrap preflight",
        ),
        (
            True,
            "mongodb-id",
            "redis-id",
            "backend-id\nmongodb-id\nredis-id",
            "mongodb-id",
            "redis-id",
            True,
            "",
        ),
        (
            True,
            "mongodb-id",
            "redis-id",
            "backend-id\nmongodb-new\nredis-id",
            "mongodb-new",
            "redis-id",
            False,
            "changed after preflight",
        ),
    ],
)
def test_fork_deployment_revalidates_datastore_fingerprints_before_mutation(
    had_previous_compose: bool,
    mongodb_id_before: str,
    redis_id_before: str,
    current_project_ids: str,
    current_mongodb_id: str,
    current_redis_id: str,
    expected_success: bool,
    expected_message: str,
):
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    ssh_step = next(
        step
        for step in workflow["jobs"]["deploy-fork"]["steps"]
        if step.get("uses") == SSH_ACTION
    )
    deploy_script = ssh_step["with"]["script"]
    function_body = deploy_script.split(
        "verify_stateful_pre_mutation() {", 1
    )[1].split("\n}\n\nrestore_file() {", 1)[0]
    verification_script = (
        "set -eu\n"
        "compose_project_container_ids() {\n"
        "  printf '%s\\n' \"${CURRENT_PROJECT_IDS}\"\n"
        "}\n"
        "restore_compose() {\n"
        "  case \"$*\" in\n"
        "    'ps -q mongodb') printf '%s\\n' \"${CURRENT_MONGODB_ID}\" ;;\n"
        "    'ps -q redis') printf '%s\\n' \"${CURRENT_REDIS_ID}\" ;;\n"
        "    *) exit 97 ;;\n"
        "  esac\n"
        "}\n"
        f"had_previous_compose={'true' if had_previous_compose else 'false'}\n"
        f"mongodb_id_before={mongodb_id_before!r}\n"
        f"redis_id_before={redis_id_before!r}\n"
        "verify_stateful_pre_mutation() {"
        f"{function_body}\n"
        "}\n"
        "verify_stateful_pre_mutation\n"
    )
    result = subprocess.run(
        ["/bin/sh"],
        input=verification_script,
        env={
            **os.environ,
            "CURRENT_PROJECT_IDS": current_project_ids,
            "CURRENT_MONGODB_ID": current_mongodb_id,
            "CURRENT_REDIS_ID": current_redis_id,
        },
        text=True,
        capture_output=True,
    )

    combined_output = result.stdout + result.stderr
    assert (result.returncode == 0) is expected_success
    assert expected_message in combined_output


def test_public_compose_example_requires_host_injected_secrets():
    example = (PROJECT_ROOT / "docker-compose-example.yml").read_text()

    assert "API_KEY=${API_KEY:?required}" in example
    assert "JWT_SECRET_KEY=${JWT_SECRET_KEY:?required}" in example
    assert "CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS:?required}" in example
    assert "API_KEY=sk-" not in example


def test_development_data_volumes_cannot_reuse_production_storage():
    production = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    development = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose-development.yml").read_text()
    )

    assert production["volumes"]["mongodb_data"]["name"] == (
        "${CORE_RESOURCE_PREFIX:-manus}-mongodb-data"
    )
    assert development["volumes"]["mongodb_data"]["name"] == (
        "${DEV_RESOURCE_PREFIX:-manus}-mongodb-data-dev"
    )
    assert production["volumes"]["redis_data"]["name"] == (
        "${CORE_RESOURCE_PREFIX:-manus}-redis-data"
    )
    assert development["volumes"]["redis_data"]["name"] == (
        "${DEV_RESOURCE_PREFIX:-manus}-redis-data-dev"
    )


def test_helper_scripts_use_distinct_compose_projects():
    production_script = (PROJECT_ROOT / "run.sh").read_text()
    development_script = (PROJECT_ROOT / "dev.sh").read_text()

    assert 'PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-manus}"' in production_script
    assert 'PROJECT_NAME="${DEV_COMPOSE_PROJECT_NAME:-ai-manus-dev}"' in (
        development_script
    )
    assert '-p "$PROJECT_NAME" -f docker-compose.yml' in production_script
    assert 'RESOURCE_PREFIX="manus-$PROJECT_NAME"' in production_script
    assert 'CORE_RESOURCE_PREFIX="$RESOURCE_PREFIX"' in production_script
    assert '-p "$PROJECT_NAME" -f docker-compose-development.yml' in (
        development_script
    )
    assert 'RESOURCE_PREFIX="manus-$PROJECT_NAME"' in development_script
    assert 'DEV_RESOURCE_PREFIX="$RESOURCE_PREFIX"' in development_script
    assert 'DEV_SANDBOX_NETWORK=' in development_script
    assert 'DEV_CLAW_NETWORK=' in development_script


def test_local_docker_sandboxes_have_cpu_memory_and_process_limits():
    development = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose-development.yml").read_text()
    )
    fixed_sandbox = development["services"]["sandbox"]

    assert fixed_sandbox["mem_limit"] == "${SANDBOX_MEMORY_LIMIT:-2g}"
    assert fixed_sandbox["cpus"] == "${SANDBOX_CPU_LIMIT:-2.0}"
    assert fixed_sandbox["pids_limit"] == "${SANDBOX_PIDS_LIMIT:-512}"

    example = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose-example.yml").read_text()
    )
    backend_environment = _environment_map(example["services"]["backend"])
    assert backend_environment["SANDBOX_MEMORY_LIMIT"] == "2g"
    assert backend_environment["SANDBOX_CPU_LIMIT"] == "2.0"
    assert backend_environment["SANDBOX_PIDS_LIMIT"] == "512"


def test_production_dynamic_runtime_images_follow_the_compose_release():
    production = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    backend_environment = _environment_map(production["services"]["backend"])

    assert backend_environment["SANDBOX_IMAGE"] == (
        "${SANDBOX_IMAGE:-${IMAGE_REGISTRY:-simpleyyt}/"
        "manus-sandbox:${IMAGE_TAG:-latest}}"
    )
    assert backend_environment["CLAW_IMAGE"] == (
        "${CLAW_IMAGE:-${IMAGE_REGISTRY:-simpleyyt}/"
        "manus-claw:${IMAGE_TAG:-latest}}"
    )


def test_container_entrypoints_do_not_sync_dependencies_or_log_claw_token():
    backend_dockerfile = (PROJECT_ROOT / "backend/Dockerfile").read_text()
    sandbox_dockerfile = (PROJECT_ROOT / "sandbox/Dockerfile").read_text()
    mockserver_dockerfile = (PROJECT_ROOT / "mockserver/Dockerfile").read_text()
    worker_entrypoint = (PROJECT_ROOT / "backend/start_worker.sh").read_text()
    sandbox_supervisor = (PROJECT_ROOT / "sandbox/supervisord.conf").read_text()
    claw_entrypoint = (PROJECT_ROOT / "claw/entrypoint.sh").read_text()
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml").read_text()
    )
    development = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose-development.yml").read_text()
    )
    uv_version = workflow["env"]["UV_VERSION"]

    assert 'CMD ["/app/.venv/bin/uvicorn"' in backend_dockerfile
    assert f"pip install uv=={uv_version}" in backend_dockerfile
    assert f"pip3 install uv=={uv_version}" in sandbox_dockerfile
    assert "--reload" not in mockserver_dockerfile
    assert (
        development["services"]["mockserver"]["command"] == ["./dev.sh"]
    )
    assert 'exec "${SCRIPT_DIR}/.venv/bin/celery"' in worker_entrypoint
    assert "command=/app/.venv/bin/uvicorn" in sandbox_supervisor
    assert "uv run" not in backend_dockerfile.split("CMD ", 1)[1]
    assert "exec uv run" not in worker_entrypoint
    assert "command=uv run" not in sandbox_supervisor

    shell_commands = [
        line
        for line in claw_entrypoint.splitlines()
        if line.lstrip().startswith(("echo ", "printf "))
    ]
    assert all("OPENCLAW_GATEWAY_TOKEN" not in line for line in shell_commands)
    assert "umask 077" in claw_entrypoint
    assert claw_entrypoint.count('wait "${GATEWAY_PID}"') == 1


@pytest.mark.parametrize(
    "lock_name",
    ["frontend/package-lock.json", "claw/manus-claw/package-lock.json"],
)
def test_npm_locks_use_the_official_registry(lock_name: str):
    lock = json.loads((PROJECT_ROOT / lock_name).read_text())
    resolved_urls = _package_lock_resolved_urls(lock)

    assert resolved_urls
    assert all(
        url.startswith("https://registry.npmjs.org/") for url in resolved_urls
    )


def test_sandbox_python_lock_uses_official_pypi_artifacts():
    lock = (PROJECT_ROOT / "sandbox/uv.lock").read_text()
    registry_urls = re.findall(r'source = \{ registry = "([^"]+)" \}', lock)
    artifact_urls = re.findall(r'\burl = "([^"]+)"', lock)

    assert registry_urls
    assert artifact_urls
    assert set(registry_urls) == {"https://pypi.org/simple"}
    assert all(
        url.startswith("https://files.pythonhosted.org/packages/")
        for url in artifact_urls
    )


@pytest.mark.parametrize(
    "documentation_name",
    ["README.md", "README_zh.md", "docs/quick_start.md", "docs/en/quick_start.md"],
)
def test_documented_compose_example_matches_the_real_example(
    documentation_name: str,
):
    truth = yaml.safe_load((PROJECT_ROOT / "docker-compose-example.yml").read_text())
    text = (PROJECT_ROOT / documentation_name).read_text()
    snippet = text.split("<!-- docker-compose-example.yml -->", 1)[1]
    snippet = snippet.split("<!-- /docker-compose-example.yml -->", 1)[0].strip()
    snippet = snippet.removeprefix("```yaml").removesuffix("```").strip()

    assert yaml.safe_load(snippet) == truth
