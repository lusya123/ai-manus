from pathlib import Path
import json
import re
import textwrap

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_NETWORK = "manus-network"
DATA_NETWORK = "manus-data-network"


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
    assert compose["networks"][RUNTIME_NETWORK]["name"] == runtime_network_name
    assert set(services["backend"]["networks"]) == {
        RUNTIME_NETWORK,
        DATA_NETWORK,
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
    assert backend_environment["SANDBOX_NETWORK"] == runtime_network_name
    assert backend_environment["CLAW_NETWORK"] == runtime_network_name
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
        if step.get("uses") == "appleboy/ssh-action@v1.0.3"
    )
    deploy_script = ssh_step["with"]["script"]
    override = deploy_script.split('cat > "$incoming_override" <<EOF', 1)[1]
    override = override.split("\nEOF", 1)[0]
    compose_override = yaml.safe_load(textwrap.dedent(override))
    worker = compose_override["services"]["worker"]

    assert set(worker["networks"]) == {RUNTIME_NETWORK, DATA_NETWORK}
    assert worker["restart"] == "unless-stopped"
    assert worker["command"] == ["./start_worker.sh"]
    worker_environment = _environment_map(worker)
    assert worker_environment["SANDBOX_NETWORK"] == RUNTIME_NETWORK
    assert worker_environment["CLAW_NETWORK"] == RUNTIME_NETWORK

    health_command = worker["healthcheck"]["test"][1].replace("\\$", "$")
    assert "/app/.venv/bin/celery" in health_command
    assert "inspect ping" in health_command
    assert 'celery@$$(hostname)' in health_command


def test_fork_deployment_transfers_and_rolls_back_both_compose_files():
    workflow_text = (
        PROJECT_ROOT / ".github/workflows/docker-build-and-push.yml"
    ).read_text()
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["deploy-fork"]["steps"]

    assert any(step.get("uses") == "actions/checkout@v4" for step in steps)
    prepare = next(step for step in steps if step.get("id") == "deploy")
    assert "base64 < docker-compose.yml" in prepare["run"]
    ssh_step = next(
        step for step in steps if step.get("uses") == "appleboy/ssh-action@v1.0.3"
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

    qemu_index = uses.index("docker/setup-qemu-action@v3")
    buildx_index = uses.index("docker/setup-buildx-action@v3")
    assert qemu_index < buildx_index
    build_step = next(
        step for step in steps if step.get("uses") == "docker/build-push-action@v5"
    )
    assert build_step["with"]["platforms"] == "linux/amd64,linux/arm64"


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

    assert production["volumes"]["mongodb_data"]["name"] == "manus-mongodb-data"
    assert development["volumes"]["mongodb_data"]["name"] == "manus-mongodb-data-dev"
    assert production["volumes"]["redis_data"]["name"] == "manus-redis-data"
    assert development["volumes"]["redis_data"]["name"] == "manus-redis-data-dev"


def test_helper_scripts_use_distinct_compose_projects():
    production_script = (PROJECT_ROOT / "run.sh").read_text()
    development_script = (PROJECT_ROOT / "dev.sh").read_text()

    assert 'PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-manus}"' in production_script
    assert 'PROJECT_NAME="${DEV_COMPOSE_PROJECT_NAME:-ai-manus-dev}"' in (
        development_script
    )
    assert '-p "$PROJECT_NAME" -f docker-compose.yml' in production_script
    assert '-p "$PROJECT_NAME" -f docker-compose-development.yml' in (
        development_script
    )


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
    worker_entrypoint = (PROJECT_ROOT / "backend/start_worker.sh").read_text()
    sandbox_supervisor = (PROJECT_ROOT / "sandbox/supervisord.conf").read_text()
    claw_entrypoint = (PROJECT_ROOT / "claw/entrypoint.sh").read_text()

    assert 'CMD ["/app/.venv/bin/uvicorn"' in backend_dockerfile
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
