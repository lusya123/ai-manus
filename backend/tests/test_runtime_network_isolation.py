from __future__ import annotations

from types import SimpleNamespace

import docker
import pytest

from app.infrastructure.external import runtime_network as runtime_network_module
from app.infrastructure.external.runtime_network import (
    RUNTIME_GATEWAY_ALIAS,
    RUNTIME_NETWORK_KIND,
    assert_legacy_network_has_at_most_one_runtime,
    connect_runtime_to_private_network,
    audit_legacy_runtime_networks,
    container_ip_on_network,
    ensure_private_runtime_network,
    ensure_runtime_egress_network,
    gc_orphaned_runtime_networks,
    remove_private_runtime_network,
    reattach_backend_to_private_runtime_networks,
    resolve_owned_runtime_control_ip,
    runtime_egress_network_name,
    runtime_container_identity_labels,
    runtime_network_name,
)


class FakeContainer:
    def __init__(
        self,
        container_id: str,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        running: bool = True,
    ) -> None:
        self.id = container_id
        self.name = name
        self.attrs = {
            "Config": {"Labels": dict(labels or {})},
            "State": {"Running": running},
            "NetworkSettings": {"Networks": {}},
        }

    def reload(self) -> None:
        return None


class FakeContainers:
    def __init__(self) -> None:
        self.items: dict[str, FakeContainer] = {}

    def add(self, container: FakeContainer) -> None:
        self.items[container.id] = container
        self.items[container.name] = container

    def get(self, reference: str) -> FakeContainer:
        try:
            return self.items[reference]
        except KeyError as exc:
            raise docker.errors.NotFound("container missing") from exc


class FakeNetwork:
    def __init__(
        self,
        owner: "FakeNetworks",
        name: str,
        labels: dict[str, str] | None = None,
        *,
        internal: bool = False,
        options: dict[str, str] | None = None,
    ) -> None:
        self.owner = owner
        self.name = name
        self.attrs = {
            "Labels": dict(labels or {}),
            "Containers": {},
            "Driver": "bridge",
            "Internal": internal,
            "Options": dict(options or {}),
        }
        self.removed = False

    def reload(self) -> None:
        return None

    def connect(self, container: FakeContainer, aliases=None) -> None:
        self.attrs["Containers"][container.id] = {"Name": container.name}
        container.attrs["NetworkSettings"]["Networks"][self.name] = {
            "Aliases": list(aliases or []),
            "IPAddress": f"172.31.0.{len(self.attrs['Containers']) + 1}",
        }

    def disconnect(self, container: FakeContainer, force: bool = False) -> None:
        self.attrs["Containers"].pop(container.id, None)
        container.attrs["NetworkSettings"]["Networks"].pop(self.name, None)

    def remove(self) -> None:
        if self.attrs["Containers"]:
            raise RuntimeError("active endpoints")
        self.removed = True
        self.owner.items.pop(self.name, None)


class FakeNetworks:
    def __init__(self) -> None:
        self.items: dict[str, FakeNetwork] = {}
        self.create_kwargs: list[dict] = []

    def get(self, name: str) -> FakeNetwork:
        try:
            return self.items[name]
        except KeyError as exc:
            raise docker.errors.NotFound("network missing") from exc

    def create(self, name: str, **kwargs) -> FakeNetwork:
        self.create_kwargs.append(dict(kwargs))
        network = FakeNetwork(
            self,
            name,
            kwargs.get("labels"),
            internal=bool(kwargs.get("internal")),
            options=kwargs.get("options"),
        )
        self.items[name] = network
        return network

    def list(self, filters=None) -> list[FakeNetwork]:
        if not filters:
            return list(self.items.values())
        raw_labels = filters.get("label") or []
        requested = [raw_labels] if isinstance(raw_labels, str) else raw_labels
        return [
            network
            for network in self.items.values()
            if all(
                network.attrs["Labels"].get(key) == value
                for key, value in (
                    label.split("=", 1) for label in requested
                )
            )
        ]


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.networks = FakeNetworks()

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _forwarding_disabled(monkeypatch) -> None:
    """Keep network-isolation unit tests independent of the host kernel."""

    monkeypatch.setattr(
        runtime_network_module,
        "_read_runtime_forwarding_sysctl",
        lambda _path: "0\n",
    )


def test_runtime_network_names_are_stable_and_separate() -> None:
    first = runtime_network_name("sandbox", "sandbox-owner-a")
    second = runtime_network_name("sandbox", "sandbox-owner-b")
    claw = runtime_network_name("claw", "sandbox-owner-a")

    assert first == runtime_network_name("sandbox", "sandbox-owner-a")
    assert first != second
    assert first != claw
    assert first != runtime_egress_network_name("sandbox", "sandbox-owner-a")
    assert runtime_network_name(
        "sandbox", "sandbox-owner-a", "deployment-a"
    ) != runtime_network_name(
        "sandbox", "sandbox-owner-a", "deployment-b"
    )
    assert len(first) < 64


def test_partial_egress_only_runtime_is_repaired_on_lookup(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer("gateway-id", "backend")
    runtime = FakeContainer(
        "runtime-id",
        "sandbox-partial",
        labels={
            **runtime_container_identity_labels("sandbox", "sandbox-partial"),
            "ai-manus.session_id": "session-partial",
        },
    )
    client.containers.add(gateway)
    client.containers.add(runtime)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-partial",
        owner_id="session-partial",
    )
    client.networks.get(egress_name).connect(runtime)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )

    address = resolve_owned_runtime_control_ip(
        client,
        runtime,
        runtime_kind="sandbox",
        container_name="sandbox-partial",
        owner_id="session-partial",
    )

    control_name = runtime_network_name("sandbox", "sandbox-partial")
    assert address == runtime.attrs["NetworkSettings"]["Networks"][control_name][
        "IPAddress"
    ]
    assert set(client.networks.get(egress_name).attrs["Containers"]) == {
        runtime.id
    }
    assert set(client.networks.get(control_name).attrs["Containers"]) == {
        gateway.id,
        runtime.id,
    }


def test_gc_never_mutates_another_runtime_deployment(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    monkeypatch.setattr(runtime_network_module.docker, "from_env", lambda **kwargs: client)
    control_name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-other-deployment",
        owner_id="session-shared",
        deployment_id="deployment-b",
    )
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-other-deployment",
        owner_id="session-shared",
        deployment_id="deployment-b",
    )
    for name in (control_name, egress_name):
        client.networks.get(name).attrs["Labels"]["ai-manus.created_at"] = "1"
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        runtime_network_gc_grace_seconds=60,
        runtime_deployment_id="deployment-a",
        sandbox_provider="docker",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    assert gc_orphaned_runtime_networks(settings, now=1000) == 0
    assert control_name in client.networks.items
    assert egress_name in client.networks.items


def test_gateway_project_must_match_runtime_deployment(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer(
        "gateway-id",
        "backend",
        labels={"com.docker.compose.project": "deployment-b"},
    )
    client.containers.add(gateway)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )

    with pytest.raises(RuntimeError, match="must exactly match"):
        ensure_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-project-mismatch",
            owner_id="session-project-mismatch",
            deployment_id="deployment-a",
        )


def test_forwarding_guard_reads_every_required_ipv4_and_ipv6_sysctl(
    monkeypatch,
) -> None:
    observed: list[str] = []

    def read_sysctl(path: str) -> str:
        observed.append(path)
        return "0\n"

    monkeypatch.setattr(
        runtime_network_module,
        "_read_runtime_forwarding_sysctl",
        read_sysctl,
    )

    runtime_network_module.assert_runtime_process_forwarding_disabled()

    assert observed == [
        "/proc/sys/net/ipv4/ip_forward",
        "/proc/sys/net/ipv4/conf/all/forwarding",
        "/proc/sys/net/ipv4/conf/default/forwarding",
        "/proc/sys/net/ipv6/conf/all/forwarding",
        "/proc/sys/net/ipv6/conf/default/forwarding",
    ]


@pytest.mark.parametrize(
    "unsafe_path",
    runtime_network_module._RUNTIME_FORWARDING_SYSCTLS,
)
def test_forwarding_guard_rejects_any_enabled_sysctl(
    monkeypatch,
    unsafe_path: str,
) -> None:
    monkeypatch.setattr(
        runtime_network_module,
        "_read_runtime_forwarding_sysctl",
        lambda path: "1\n" if path == unsafe_path else "0\n",
    )

    with pytest.raises(RuntimeError, match="forwarding sysctl must be 0"):
        runtime_network_module.assert_runtime_process_forwarding_disabled()


def test_forwarding_guard_fails_closed_when_proc_sysctl_is_unreadable(
    monkeypatch,
) -> None:
    def unreadable(_path: str) -> str:
        raise PermissionError("procfs denied")

    monkeypatch.setattr(
        runtime_network_module,
        "_read_runtime_forwarding_sysctl",
        unreadable,
    )

    with pytest.raises(RuntimeError, match="Cannot verify"):
        runtime_network_module.assert_runtime_process_forwarding_disabled()


def test_private_network_checks_forwarding_before_docker_mutation(
    monkeypatch,
) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "assert_runtime_process_forwarding_disabled",
        lambda: (_ for _ in ()).throw(RuntimeError("forwarding enabled")),
    )

    with pytest.raises(RuntimeError, match="forwarding enabled"):
        ensure_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-forwarding",
            owner_id="session-forwarding",
        )

    assert client.networks.create_kwargs == []


def test_private_network_attaches_only_exact_gateway_alias(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer("gateway-id", "backend")
    client.containers.add(gateway)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )

    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )
    network = client.networks.get(name)

    assert network.attrs["Labels"]["ai-manus.kind"] == RUNTIME_NETWORK_KIND
    assert set(network.attrs["Containers"]) == {gateway.id}
    configured_subnet = client.networks.create_kwargs[0]["ipam"]["Config"][0][
        "Subnet"
    ]
    assert configured_subnet.startswith("10.")
    assert configured_subnet.endswith("/28")
    assert network.attrs["Internal"] is True
    assert gateway.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        RUNTIME_GATEWAY_ALIAS
    ]
    # Re-entry is idempotent and does not create a second endpoint.
    assert ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    ) == name
    assert set(network.attrs["Containers"]) == {gateway.id}


def test_runtime_has_separate_egress_and_internal_control_bridges(
    monkeypatch,
) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer("gateway-id", "backend")
    runtime = FakeContainer(
        "runtime-id",
        "sandbox-a",
        labels={
            **runtime_container_identity_labels("sandbox", "sandbox-a"),
            "ai-manus.session_id": "session-a",
        },
    )
    client.containers.add(gateway)
    client.containers.add(runtime)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )

    control_name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )
    egress = client.networks.get(egress_name)
    egress.connect(runtime)
    address = connect_runtime_to_private_network(
        client,
        runtime,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )

    control = client.networks.get(control_name)
    assert address.startswith("172.31.0.")
    assert control.attrs["Internal"] is True
    assert egress.attrs["Internal"] is False
    assert egress.attrs["Options"] == {
        "com.docker.network.bridge.enable_icc": "false"
    }
    assert set(control.attrs["Containers"]) == {gateway.id, runtime.id}
    assert set(egress.attrs["Containers"]) == {runtime.id}

    # Simulate Docker AutoRemove detaching the runtime. Exact cleanup must
    # remove both orphan bridges even though no container lookup is possible.
    control.disconnect(runtime)
    egress.disconnect(runtime)
    client.containers.items.pop(runtime.id)
    client.containers.items.pop(runtime.name)
    assert remove_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    ) is True
    assert control.removed is True
    assert egress.removed is True


def test_ttl_autoremove_orphan_bridges_are_reclaimed(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    control_name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-expired",
        owner_id="session-expired",
    )
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-expired",
        owner_id="session-expired",
    )
    runtime = FakeContainer(
        "runtime-expired-id",
        "sandbox-expired",
        labels={
            **runtime_container_identity_labels(
                "sandbox", "sandbox-expired"
            ),
            "ai-manus.session_id": "session-expired",
        },
    )
    client.containers.add(runtime)
    client.networks.get(egress_name).connect(runtime)
    connect_runtime_to_private_network(
        client,
        runtime,
        runtime_kind="sandbox",
        container_name="sandbox-expired",
        owner_id="session-expired",
    )
    # Docker AutoRemove detaches and deletes the TTL-expired container while
    # the durable chat/session remains.
    client.networks.get(control_name).disconnect(runtime)
    client.networks.get(egress_name).disconnect(runtime)
    client.containers.items.pop(runtime.id)
    client.containers.items.pop(runtime.name)
    for name in (control_name, egress_name):
        client.networks.get(name).attrs["Labels"][
            "ai-manus.created_at"
        ] = "100.0"
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        runtime_network_gc_grace_seconds=60,
        sandbox_provider="docker",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    assert gc_orphaned_runtime_networks(settings, now=161.0) == 1
    assert control_name not in client.networks.items
    assert egress_name not in client.networks.items


def test_orphan_gc_rechecks_late_container_before_network_mutation(
    monkeypatch,
) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    control_name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-racing",
        owner_id="session-racing",
    )
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-racing",
        owner_id="session-racing",
    )
    for name in (control_name, egress_name):
        client.networks.get(name).attrs["Labels"][
            "ai-manus.created_at"
        ] = "100.0"
    late_runtime = FakeContainer(
        "late-runtime-id",
        "sandbox-racing",
        labels={
            "ai-manus.kind": "sandbox",
            "ai-manus.session_id": "session-racing",
        },
    )
    original_get = client.containers.get
    named_lookups = 0

    def race_get(reference: str):
        nonlocal named_lookups
        if reference == "sandbox-racing":
            named_lookups += 1
            if named_lookups == 1:
                raise docker.errors.NotFound("not visible yet")
            return late_runtime
        return original_get(reference)

    monkeypatch.setattr(client.containers, "get", race_get)
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        runtime_network_gc_grace_seconds=60,
        sandbox_provider="docker",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    assert gc_orphaned_runtime_networks(settings, now=161.0) == 0
    assert control_name in client.networks.items
    assert egress_name in client.networks.items


def test_private_network_rejects_foreign_labels(monkeypatch) -> None:
    client = FakeDockerClient()
    name = runtime_network_name("sandbox", "sandbox-a")
    client.networks.items[name] = FakeNetwork(
        client.networks,
        name,
        {
            "ai-manus.kind": RUNTIME_NETWORK_KIND,
            "ai-manus.runtime_kind": "sandbox",
            "ai-manus.runtime_container": "sandbox-other",
            "ai-manus.owner_digest": "foreign",
        },
    )
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)

    with pytest.raises(PermissionError, match="another runtime"):
        ensure_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-a",
            owner_id="session-a",
        )


def test_ipam_collision_probes_next_small_subnet(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    original_create = client.networks.create
    attempted_subnets: list[str] = []

    def collide_once(name: str, **kwargs):
        attempted_subnets.append(kwargs["ipam"]["Config"][0]["Subnet"])
        if len(attempted_subnets) == 1:
            raise docker.errors.APIError("Pool overlaps with another network")
        return original_create(name, **kwargs)

    monkeypatch.setattr(client.networks, "create", collide_once)

    ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-collision",
        owner_id="session-collision",
        address_pool="10.250.0.0/24",
        subnet_prefix=28,
    )

    assert len(attempted_subnets) == 2
    assert attempted_subnets[0] != attempted_subnets[1]
    assert all(subnet.endswith("/28") for subnet in attempted_subnets)


def test_container_ip_is_selected_only_from_owned_network() -> None:
    container = FakeContainer("runtime-id", "sandbox-a")
    container.attrs["NetworkSettings"]["Networks"] = {
        "shared": {"IPAddress": "172.20.0.5"},
        "private": {"IPAddress": "172.31.0.5"},
    }

    assert container_ip_on_network(container, "private") == "172.31.0.5"
    with pytest.raises(RuntimeError, match="private-network"):
        container_ip_on_network(container, "missing")


def test_private_network_cleanup_disconnects_only_tagged_gateways(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer("gateway-id", "backend")
    client.containers.add(gateway)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )
    name = ensure_private_runtime_network(
        client,
        runtime_kind="claw",
        container_name="claw-a",
        owner_id="claw-owner",
    )
    network = client.networks.get(name)

    assert remove_private_runtime_network(
        client,
        runtime_kind="claw",
        container_name="claw-a",
        owner_id="claw-owner",
    ) is True
    assert network.removed is True
    assert name not in gateway.attrs["NetworkSettings"]["Networks"]


def test_worker_and_api_gateway_get_distinct_trusted_aliases(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer(
        "gateway-id",
        "backend",
        labels={"com.docker.compose.service": "backend"},
    )
    worker = FakeContainer("worker-id", "worker")
    client.containers.add(gateway)
    client.containers.add(worker)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "worker",
    )

    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-worker-created",
        owner_id="session-worker",
    )

    assert gateway.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        RUNTIME_GATEWAY_ALIAS
    ]
    assert worker.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        runtime_network_module.RUNTIME_CLIENT_ALIAS
    ]


def test_worker_uses_gateway_loaded_only_through_settings(monkeypatch) -> None:
    client = FakeDockerClient()
    gateway = FakeContainer(
        "gateway-id",
        "backend",
        labels={"com.docker.compose.service": "backend"},
    )
    worker = FakeContainer("worker-id", "worker")
    client.containers.add(gateway)
    client.containers.add(worker)
    monkeypatch.delenv("RUNTIME_GATEWAY_CONTAINER", raising=False)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(runtime_gateway_container="backend"),
    )
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "worker",
    )

    name = ensure_private_runtime_network(
        client,
        runtime_kind="claw",
        container_name="claw-settings-gateway",
        owner_id="claw-owner",
    )

    assert gateway.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        RUNTIME_GATEWAY_ALIAS
    ]
    assert worker.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        runtime_network_module.RUNTIME_CLIENT_ALIAS
    ]


def test_worker_rejects_non_backend_gateway(monkeypatch) -> None:
    client = FakeDockerClient()
    wrong_gateway = FakeContainer(
        "wrong-id",
        "frontend",
        labels={"com.docker.compose.service": "frontend"},
    )
    worker = FakeContainer("worker-id", "worker")
    client.containers.add(wrong_gateway)
    client.containers.add(worker)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "_gateway_reference",
        lambda: "frontend",
    )
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "worker",
    )

    with pytest.raises(PermissionError, match="not an AI Manus backend"):
        ensure_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-wrong-gateway",
            owner_id="session-worker",
        )


def test_recreated_backend_reattaches_to_live_private_runtime(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-live",
        owner_id="session-live",
    )
    egress_name = ensure_runtime_egress_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-live",
        owner_id="session-live",
    )
    runtime = FakeContainer(
        "runtime-id",
        "sandbox-live",
        labels={
            **runtime_container_identity_labels("sandbox", "sandbox-live"),
            "ai-manus.session_id": "session-live",
        },
    )
    backend = FakeContainer(
        "new-backend-id",
        "backend-new",
        labels={"com.docker.compose.service": "backend"},
    )
    client.containers.add(runtime)
    client.containers.add(backend)
    client.networks.get(name).connect(runtime)
    client.networks.get(egress_name).connect(runtime)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "_gateway_reference",
        lambda: "backend-new",
    )
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend-new",
    )

    reattach_backend_to_private_runtime_networks(client)

    assert backend.attrs["NetworkSettings"]["Networks"][name]["Aliases"] == [
        RUNTIME_GATEWAY_ALIAS
    ]
    assert egress_name not in backend.attrs["NetworkSettings"]["Networks"]


def test_gateway_reattach_rejects_foreign_runtime_label(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-live",
        owner_id="session-live",
    )
    foreign = FakeContainer(
        "foreign-id",
        "sandbox-live",
        labels={"ai-manus.kind": "claw", "ai-manus.session_id": "session-live"},
    )
    backend = FakeContainer(
        "backend-id",
        "backend",
        labels={"com.docker.compose.service": "backend"},
    )
    client.containers.add(foreign)
    client.containers.add(backend)
    client.networks.get(name).connect(foreign)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )

    with pytest.raises(PermissionError, match="foreign"):
        reattach_backend_to_private_runtime_networks(client)


def test_self_gateway_without_compose_label_is_allowed_when_no_runtime_networks(
    monkeypatch,
) -> None:
    client = FakeDockerClient()
    backend = FakeContainer("backend-id", "standalone-backend")
    client.containers.add(backend)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "_gateway_reference",
        lambda: "standalone-backend",
    )
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "standalone-backend",
    )

    reattach_backend_to_private_runtime_networks(client)


def test_private_network_cleanup_refuses_live_runtime_endpoint(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )
    runtime = FakeContainer(
        "runtime-id",
        "sandbox-a",
        labels={"ai-manus.kind": "sandbox"},
    )
    client.containers.add(runtime)
    client.networks.get(name).connect(runtime)

    with pytest.raises(
        RuntimeError,
        match="named container exists|live runtime",
    ):
        remove_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-a",
            owner_id="session-a",
        )


def test_private_network_cleanup_refuses_unknown_endpoint(monkeypatch) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    name = ensure_private_runtime_network(
        client,
        runtime_kind="sandbox",
        container_name="sandbox-a",
        owner_id="session-a",
    )
    unknown = FakeContainer("unknown-id", "operator-container")
    client.containers.add(unknown)
    client.networks.get(name).connect(unknown, aliases=["not-the-gateway"])

    with pytest.raises(RuntimeError, match="unrecognized"):
        remove_private_runtime_network(
            client,
            runtime_kind="sandbox",
            container_name="sandbox-a",
            owner_id="session-a",
        )


def test_legacy_audit_allows_one_running_runtime_and_ignores_stopped() -> None:
    client = FakeDockerClient()
    network = FakeNetwork(client.networks, "manus-network", {})
    client.networks.items[network.name] = network
    running = FakeContainer(
        "sandbox-live-id",
        "sandbox-live",
        labels={"ai-manus.kind": "sandbox"},
    )
    stopped_placeholder = FakeContainer(
        "sandbox-image-id",
        "sandbox-image-pull",
        running=False,
    )
    client.containers.add(running)
    client.containers.add(stopped_placeholder)
    network.connect(running)
    network.connect(stopped_placeholder)

    assert_legacy_network_has_at_most_one_runtime(
        client,
        network_names=("manus-network",),
        sandbox_name_prefix="sandbox",
        claw_name_prefix="claw",
    )


def test_legacy_audit_fails_closed_for_two_user_runtimes() -> None:
    client = FakeDockerClient()
    network = FakeNetwork(client.networks, "manus-network", {})
    client.networks.items[network.name] = network
    for identifier, kind in (("sandbox-id", "sandbox"), ("claw-id", "claw")):
        runtime = FakeContainer(
            identifier,
            identifier,
            labels={"ai-manus.kind": kind},
        )
        client.containers.add(runtime)
        network.connect(runtime)

    with pytest.raises(RuntimeError, match="Multiple legacy"):
        assert_legacy_network_has_at_most_one_runtime(
            client,
            network_names=("manus-network",),
            sandbox_name_prefix="sandbox",
            claw_name_prefix="claw",
        )


def test_legacy_audit_detects_env_only_claw_runtime() -> None:
    client = FakeDockerClient()
    network = FakeNetwork(client.networks, "manus-network", {})
    client.networks.items[network.name] = network
    sandbox = FakeContainer(
        "sandbox-id",
        "sandbox-live",
        labels={"ai-manus.kind": "sandbox"},
    )
    legacy_claw = FakeContainer(
        "legacy-claw-id",
        "unknown-sidecar",
    )
    legacy_claw.attrs["Config"]["Env"] = [
        "CLAW_TTL_SECONDS=3600",
        "MANUS_API_BASE_URL=http://backend:8000",
        "MANUS_API_KEY=runtime-secret",
    ]
    client.containers.add(sandbox)
    client.containers.add(legacy_claw)
    network.connect(sandbox)
    network.connect(legacy_claw)

    with pytest.raises(RuntimeError, match="Multiple legacy"):
        assert_legacy_network_has_at_most_one_runtime(
            client,
            network_names=("manus-network",),
            sandbox_name_prefix="sandbox",
            claw_name_prefix="claw",
        )


def test_agentbay_without_dynamic_claw_never_touches_docker(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_network_module,
        "assert_runtime_process_forwarding_disabled",
        lambda: (_ for _ in ()).throw(
            AssertionError("AgentBay-only startup must not inspect procfs")
        ),
    )
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AgentBay-only startup must not touch Docker")
        ),
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="agentbay",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    audit_legacy_runtime_networks(settings)


def test_fixed_only_runtimes_never_check_forwarding_or_touch_docker(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_network_module,
        "assert_runtime_process_forwarding_disabled",
        lambda: (_ for _ in ()).throw(
            AssertionError("fixed-only startup must not inspect procfs")
        ),
    )
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("fixed-only startup must not touch Docker")
        ),
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address="http://sandbox:8080",
        claw_enabled=True,
        claw_address="http://claw:18788",
    )

    audit_legacy_runtime_networks(settings)


def test_dynamic_docker_audit_checks_forwarding_before_docker(
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        runtime_network_module,
        "assert_runtime_process_forwarding_disabled",
        lambda: (_ for _ in ()).throw(RuntimeError("forwarding enabled")),
    )
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe topology must fail before Docker")
        ),
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    with pytest.raises(RuntimeError, match="forwarding enabled"):
        audit_legacy_runtime_networks(settings)


def test_native_host_dynamic_runtime_is_rejected_before_docker(monkeypatch) -> None:
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: False)
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid host topology must fail before Docker")
        ),
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address=None,
        claw_enabled=False,
        claw_address=None,
    )

    with pytest.raises(RuntimeError, match="backend to run in Docker"):
        audit_legacy_runtime_networks(settings)


def test_default_bridge_audit_detects_historical_none_prefix(monkeypatch) -> None:
    client = FakeDockerClient()
    bridge = FakeNetwork(client.networks, "bridge", {})
    client.networks.items[bridge.name] = bridge
    for suffix in ("one", "two"):
        runtime = FakeContainer(
            f"runtime-{suffix}",
            f"None-{suffix}",
        )
        client.containers.add(runtime)
        bridge.connect(runtime)
    backend = FakeContainer("backend-id", "backend")
    client.containers.add(backend)
    bridge.connect(backend)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address=None,
        sandbox_network=None,
        sandbox_name_prefix=None,
        claw_enabled=False,
        claw_address=None,
        claw_network=None,
        claw_name_prefix="manus-claw",
    )

    with pytest.raises(RuntimeError, match="Multiple legacy"):
        audit_legacy_runtime_networks(settings)


def test_global_legacy_audit_finds_old_network_and_old_prefix(monkeypatch) -> None:
    client = FakeDockerClient()
    old_network = FakeNetwork(
        client.networks,
        "retired-runtime-network",
        {"com.docker.compose.project": "ai-manus-prod"},
    )
    client.networks.items[old_network.name] = old_network
    for suffix in ("one", "two"):
        runtime = FakeContainer(
            f"retired-runtime-{suffix}",
            f"retired-prefix-{suffix}",
        )
        runtime.attrs["Config"]["Image"] = "registry/old-manus-sandbox:v1"
        client.containers.add(runtime)
        old_network.connect(runtime)
    current_network = FakeNetwork(
        client.networks,
        "new-runtime-network",
        {"com.docker.compose.project": "ai-manus-prod"},
    )
    client.networks.items[current_network.name] = current_network
    backend = FakeContainer(
        "backend-id",
        "backend",
        labels={"com.docker.compose.project": "ai-manus-prod"},
    )
    client.containers.add(backend)
    current_network.connect(backend)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address=None,
        sandbox_network="new-runtime-network",
        sandbox_name_prefix="new-prefix",
        claw_enabled=False,
        claw_address=None,
        claw_network=None,
        claw_name_prefix="new-claw-prefix",
    )

    with pytest.raises(RuntimeError, match="retired-runtime-network"):
        audit_legacy_runtime_networks(settings)


def test_legacy_audit_does_not_count_backend_claw_config_as_runtime(
    monkeypatch,
) -> None:
    client = FakeDockerClient()
    private_network = FakeNetwork(
        client.networks,
        "manus-sandbox-private",
    )
    client.networks.items[private_network.name] = private_network
    backend = FakeContainer(
        "backend-id",
        "ai-manus-backend-1",
        labels={
            "com.docker.compose.project": "ai-manus",
            "com.docker.compose.service": "backend",
        },
    )
    backend.attrs["Config"]["Env"] = [
        "CLAW_TTL_SECONDS=3600",
        "MANUS_API_BASE_URL=http://backend:8000",
    ]
    sandbox = FakeContainer(
        "sandbox-id",
        "ai-manus-sandbox-private",
        labels={"ai-manus.kind": "sandbox"},
    )
    client.containers.add(backend)
    client.containers.add(sandbox)
    private_network.connect(backend)
    private_network.connect(sandbox)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend-id")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend-id",
    )
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        runtime_deployment_id="ai-manus",
        sandbox_provider="docker",
        sandbox_address=None,
        sandbox_network="manus-network",
        sandbox_name_prefix="ai-manus-sandbox",
        claw_enabled=False,
        claw_address=None,
        claw_network=None,
        claw_name_prefix="ai-manus-claw",
    )

    audit_legacy_runtime_networks(settings)


def test_global_legacy_audit_ignores_another_compose_project(monkeypatch) -> None:
    client = FakeDockerClient()
    prod_network = FakeNetwork(
        client.networks,
        "manus-network",
        {"com.docker.compose.project": "ai-manus"},
    )
    dev_network = FakeNetwork(
        client.networks,
        "manus-network-dev",
        {"com.docker.compose.project": "ai-manus-dev"},
    )
    client.networks.items[prod_network.name] = prod_network
    client.networks.items[dev_network.name] = dev_network
    backend = FakeContainer(
        "backend-id",
        "backend",
        labels={"com.docker.compose.project": "ai-manus"},
    )
    client.containers.add(backend)
    prod_network.connect(backend)
    for kind in ("sandbox", "claw"):
        runtime = FakeContainer(
            f"dev-{kind}-id",
            f"ai-manus-{kind}-dev",
            labels={"ai-manus.kind": kind},
        )
        client.containers.add(runtime)
        dev_network.connect(runtime)
    monkeypatch.setattr(runtime_network_module, "_running_in_container", lambda: True)
    monkeypatch.setattr(runtime_network_module, "_gateway_reference", lambda: "backend")
    monkeypatch.setattr(
        runtime_network_module,
        "_current_container_reference",
        lambda: "backend",
    )
    monkeypatch.setattr(
        runtime_network_module.docker,
        "from_env",
        lambda **kwargs: client,
    )
    settings = SimpleNamespace(
        runtime_network_isolation=True,
        sandbox_provider="docker",
        sandbox_address=None,
        sandbox_network="manus-network",
        sandbox_name_prefix="sandbox",
        claw_enabled=False,
        claw_address=None,
        claw_network=None,
        claw_name_prefix="manus-claw",
    )

    audit_legacy_runtime_networks(settings)


def test_claw_private_backend_url_and_host_ports_are_disabled() -> None:
    from app.infrastructure.external.claw.docker_claw_runtime import (
        DockerClawRuntime,
    )

    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = SimpleNamespace(
        runtime_network_isolation=True,
        manus_api_base_url="http://backend:8000",
        backend_sandbox_url=None,
        backend_internal_url=None,
        backend_public_url=None,
        claw_publish_host_ports=True,
    )

    assert runtime._container_reachable_backend_url() == (
        f"http://{RUNTIME_GATEWAY_ALIAS}:8000"
    )
    assert runtime._publish_host_ports is False
