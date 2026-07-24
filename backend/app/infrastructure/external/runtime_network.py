"""Per-runtime Docker network isolation.

User-controlled sandbox and Claw containers must never share a bridge with
one another.  Each managed runtime gets a deterministic private bridge and
the backend/worker container that needs to reach it is attached as the only
gateway endpoint.  Network names and labels are deterministic so a timed-out
Docker SDK call can be recovered and cleaned without guessing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import time
from typing import Any, Iterable

import docker


RUNTIME_NETWORK_KIND = "ai-manus-runtime-network"
RUNTIME_GATEWAY_ALIAS = "manus-runtime-gateway"
RUNTIME_CLIENT_ALIAS = "manus-runtime-client"
RUNTIME_NETWORK_ROLE_CONTROL = "control"
RUNTIME_NETWORK_ROLE_EGRESS = "egress"
_LABEL_KIND = "ai-manus.kind"
_LABEL_RUNTIME_KIND = "ai-manus.runtime_kind"
_LABEL_RUNTIME_CONTAINER = "ai-manus.runtime_container"
_LABEL_OWNER_DIGEST = "ai-manus.owner_digest"
_LABEL_NETWORK_ROLE = "ai-manus.network_role"
_LABEL_CREATED_AT = "ai-manus.created_at"
_LABEL_DEPLOYMENT_DIGEST = "ai-manus.deployment_digest"
_LABEL_GENERATION_DIGEST = "ai-manus.generation_digest"
_RUNTIME_FORWARDING_SYSCTLS = (
    "/proc/sys/net/ipv4/ip_forward",
    "/proc/sys/net/ipv4/conf/all/forwarding",
    "/proc/sys/net/ipv4/conf/default/forwarding",
    "/proc/sys/net/ipv6/conf/all/forwarding",
    "/proc/sys/net/ipv6/conf/default/forwarding",
)

logger = logging.getLogger(__name__)


def _runtime_deployment_id(deployment_id: str | None = None) -> str:
    if deployment_id is None:
        from app.core.config import get_settings

        deployment_id = getattr(
            get_settings(),
            "runtime_deployment_id",
            "ai-manus",
        )
    normalized = str(deployment_id or "").strip()
    if not normalized or len(normalized) > 128:
        raise RuntimeError(
            "RUNTIME_DEPLOYMENT_ID must contain 1 to 128 characters"
        )
    return normalized


def runtime_deployment_digest(deployment_id: str | None = None) -> str:
    """Return a non-secret stable scope for one Docker deployment."""

    return hashlib.sha256(
        _runtime_deployment_id(deployment_id).encode("utf-8")
    ).hexdigest()


def runtime_generation_digest(container_name: str) -> str:
    """Fence one immutable runtime incarnation by its durable exact name."""

    return hashlib.sha256(container_name.encode("utf-8")).hexdigest()


def runtime_container_identity_labels(
    runtime_kind: str,
    container_name: str,
    deployment_id: str | None = None,
) -> dict[str, str]:
    """Labels shared by every v2 managed runtime and its two bridges."""

    return {
        _LABEL_KIND: runtime_kind,
        _LABEL_DEPLOYMENT_DIGEST: runtime_deployment_digest(deployment_id),
        _LABEL_GENERATION_DIGEST: runtime_generation_digest(container_name),
    }


def verify_owned_runtime_container(
    container: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str,
    deployment_id: str | None = None,
    allow_legacy: bool = False,
) -> bool:
    """Verify full owner plus v2 deployment/generation fencing labels.

    ``allow_legacy`` accepts only a completely unscoped v1 container.  A
    partially-labelled object is never silently adopted.
    """

    container.reload()
    labels = dict(((container.attrs or {}).get("Config") or {}).get("Labels") or {})
    owner_label = (
        "ai-manus.session_id"
        if runtime_kind == "sandbox"
        else "ai-manus.claw_id"
    )
    if (
        labels.get(_LABEL_KIND) != runtime_kind
        or labels.get(owner_label) != owner_id
    ):
        raise PermissionError("Docker runtime owner label does not match")
    deployment = labels.get(_LABEL_DEPLOYMENT_DIGEST)
    generation = labels.get(_LABEL_GENERATION_DIGEST)
    if deployment is None and generation is None and allow_legacy:
        return False
    if (
        deployment != runtime_deployment_digest(deployment_id)
        or generation != runtime_generation_digest(container_name)
    ):
        raise PermissionError(
            "Docker runtime deployment or generation label does not match"
        )
    return True


def runtime_network_name(
    runtime_kind: str,
    container_name: str,
    deployment_id: str | None = None,
) -> str:
    """Return a stable, non-secret bridge name for one runtime container."""

    normalized_kind = "".join(
        character if character.isalnum() else "-"
        for character in runtime_kind.lower()
    ).strip("-") or "runtime"
    deployment = runtime_deployment_digest(deployment_id)[:10]
    generation = runtime_generation_digest(container_name)[:14]
    return f"manus-{normalized_kind}-{deployment}-{generation}"


def runtime_egress_network_name(
    runtime_kind: str,
    container_name: str,
    deployment_id: str | None = None,
) -> str:
    """Return the companion outbound bridge name for one runtime."""

    return runtime_network_name(
        f"{runtime_kind}-egress",
        container_name,
        deployment_id,
    )


def _owner_digest(owner_id: str | None, container_name: str) -> str:
    owner = owner_id if owner_id is not None else container_name
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _expected_labels(
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    *,
    network_role: str = RUNTIME_NETWORK_ROLE_CONTROL,
    deployment_id: str | None = None,
) -> dict[str, str]:
    return {
        _LABEL_KIND: RUNTIME_NETWORK_KIND,
        _LABEL_RUNTIME_KIND: runtime_kind,
        _LABEL_RUNTIME_CONTAINER: container_name,
        _LABEL_OWNER_DIGEST: _owner_digest(owner_id, container_name),
        _LABEL_NETWORK_ROLE: network_role,
        _LABEL_DEPLOYMENT_DIGEST: runtime_deployment_digest(deployment_id),
        _LABEL_GENERATION_DIGEST: runtime_generation_digest(container_name),
    }


def _network_labels(network: Any) -> dict[str, str]:
    network.reload()
    return dict((network.attrs or {}).get("Labels") or {})


def _verify_network_owner(
    network: Any,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    *,
    network_role: str = RUNTIME_NETWORK_ROLE_CONTROL,
    deployment_id: str | None = None,
) -> None:
    labels = _network_labels(network)
    expected = _expected_labels(
        runtime_kind,
        container_name,
        owner_id,
        network_role=network_role,
        deployment_id=deployment_id,
    )
    # When cleanup is invoked through the legacy unowned interface, retain the
    # deterministic container binding but do not pretend to know an owner ID.
    keys = (
        _LABEL_KIND,
        _LABEL_RUNTIME_KIND,
        _LABEL_RUNTIME_CONTAINER,
        _LABEL_NETWORK_ROLE,
        _LABEL_DEPLOYMENT_DIGEST,
        _LABEL_GENERATION_DIGEST,
    )
    if owner_id is not None:
        keys = (*keys, _LABEL_OWNER_DIGEST)
    if any(labels.get(key) != expected[key] for key in keys):
        raise PermissionError(
            "Refusing to use a Docker runtime network owned by another runtime"
        )


def _running_in_container() -> bool:
    return os.path.exists("/.dockerenv") or bool(
        os.environ.get("KUBERNETES_SERVICE_HOST")
    )


def require_containerized_runtime_gateway() -> None:
    """Reject a topology that cannot route isolated runtime bridges safely."""

    if not _running_in_container():
        raise RuntimeError(
            "Per-runtime Docker network isolation requires the AI Manus "
            "backend to run in Docker; configure a fixed/external runtime "
            "when running the backend directly on the host"
        )


def _read_runtime_forwarding_sysctl(path: str) -> str:
    with open(path, encoding="ascii") as sysctl_file:
        return sysctl_file.read()


def assert_runtime_process_forwarding_disabled() -> None:
    """Fail closed unless this container cannot route runtime traffic.

    The backend and Celery workers join a runtime's internal control bridge.
    If forwarding is enabled in either container's network namespace, that
    trusted endpoint can accidentally become a route from the user-controlled
    runtime to its other Docker networks.  Check both the global and default
    IPv4/IPv6 forwarding switches because changing either can make later
    interfaces routable.
    """

    for path in _RUNTIME_FORWARDING_SYSCTLS:
        try:
            value = _read_runtime_forwarding_sysctl(path).strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Cannot verify that runtime gateway forwarding is disabled: {path}"
            ) from exc
        if value != "0":
            raise RuntimeError(
                f"Runtime gateway forwarding sysctl must be 0: {path}"
            )


def _gateway_reference() -> str:
    configured = os.environ.get("RUNTIME_GATEWAY_CONTAINER")
    if not configured:
        # Pydantic may load this value from an application .env file even when
        # it is not exported into ``os.environ`` (notably custom workers).
        from app.core.config import get_settings

        configured = get_settings().runtime_gateway_container
    return configured or socket.gethostname()


def _current_container_reference() -> str:
    return socket.gethostname()


def _assert_gateway_deployment_scope(
    gateway: Any,
    current: Any,
    deployment_id: str,
) -> None:
    """Bind the configured deployment ID to Docker Compose's real project."""

    gateway_labels = dict(
        ((gateway.attrs or {}).get("Config") or {}).get("Labels") or {}
    )
    current_labels = dict(
        ((current.attrs or {}).get("Config") or {}).get("Labels") or {}
    )
    gateway_project = gateway_labels.get("com.docker.compose.project")
    current_project = current_labels.get("com.docker.compose.project")
    if gateway_project and gateway_project != deployment_id:
        raise RuntimeError(
            "RUNTIME_DEPLOYMENT_ID must exactly match the Docker Compose project"
        )
    if (
        gateway_project
        and current_project
        and current_project != gateway_project
    ):
        raise PermissionError(
            "Runtime client and gateway belong to different Compose projects"
        )


def _validated_runtime_gateway(
    docker_client: Any,
    deployment_id: str,
) -> tuple[Any, Any]:
    """Resolve and authorize gateway/current containers before any mutation."""

    assert_runtime_process_forwarding_disabled()
    gateway = docker_client.containers.get(_gateway_reference())
    gateway.reload()
    current = docker_client.containers.get(_current_container_reference())
    current.reload()
    _assert_gateway_deployment_scope(gateway, current, deployment_id)
    if getattr(current, "id", None) != getattr(gateway, "id", None):
        gateway_labels = dict(
            ((gateway.attrs or {}).get("Config") or {}).get("Labels") or {}
        )
        if (
            gateway_labels.get("com.docker.compose.service") != "backend"
            and gateway_labels.get("ai-manus.runtime_gateway") != "true"
        ):
            raise PermissionError(
                "Configured runtime gateway is not an AI Manus backend"
            )
    return gateway, current


def _has_trusted_runtime_alias(container: Any, network_name: str) -> bool:
    container.reload()
    network = (
        (((container.attrs or {}).get("NetworkSettings") or {}).get("Networks") or {})
        .get(network_name)
        or {}
    )
    aliases = set(network.get("Aliases") or [])
    return bool(aliases.intersection({RUNTIME_GATEWAY_ALIAS, RUNTIME_CLIENT_ALIAS}))


def _attach_exact_alias(
    network: Any,
    container: Any,
    *,
    network_name: str,
    alias: str,
) -> None:
    container_id = getattr(container, "id", None)
    if not container_id:
        raise RuntimeError("Docker runtime client has no container ID")
    network.reload()
    endpoints = (network.attrs or {}).get("Containers") or {}
    if container_id not in endpoints:
        network.connect(container, aliases=[alias])
        return
    container.reload()
    attached = (
        (((container.attrs or {}).get("NetworkSettings") or {}).get("Networks") or {})
        .get(network_name)
        or {}
    )
    if alias not in (attached.get("Aliases") or []):
        raise PermissionError(
            "Runtime network contains a trusted client without its exact alias"
        )


def _verify_network_shape(
    network: Any,
    *,
    internal: bool,
    require_icc_disabled: bool = False,
) -> None:
    network.reload()
    attrs = network.attrs or {}
    if attrs.get("Driver") != "bridge" or bool(attrs.get("Internal")) != internal:
        raise PermissionError(
            "Refusing to use a Docker runtime network with an unsafe shape"
        )
    if require_icc_disabled:
        options = dict(attrs.get("Options") or {})
        if str(
            options.get("com.docker.network.bridge.enable_icc", "")
        ).lower() != "false":
            raise PermissionError(
                "Runtime egress network must disable inter-container traffic"
            )


def _ensure_owned_bridge(
    docker_client: Any,
    *,
    name: str,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    network_role: str,
    internal: bool,
    create: bool,
    address_pool: str,
    subnet_prefix: int,
    options: dict[str, str] | None = None,
    deployment_id: str | None = None,
) -> Any | None:
    expected_labels = _expected_labels(
        runtime_kind,
        container_name,
        owner_id,
        network_role=network_role,
        deployment_id=deployment_id,
    )
    try:
        network = docker_client.networks.get(name)
    except docker.errors.NotFound:
        if not create:
            return None
        pool = ipaddress.ip_network(address_pool, strict=True)
        if pool.version != 4:
            raise ValueError("Runtime network address pool must be IPv4")
        if subnet_prefix <= pool.prefixlen or subnet_prefix > 29:
            raise ValueError(
                "Runtime subnet prefix must be larger than the address-pool "
                "prefix and no larger than /29"
            )
        subnet_size = 1 << (32 - subnet_prefix)
        subnet_count = 1 << (subnet_prefix - pool.prefixlen)
        start_index = int.from_bytes(
            hashlib.sha256(name.encode("utf-8")).digest()[:8],
            "big",
        ) % subnet_count
        network = None
        # Docker rejects overlapping IPAM atomically. Linear probing from a
        # deterministic start closes cross-process create races without a
        # shared allocator, while the large /12 -> /28 pool keeps probes short.
        for offset in range(min(subnet_count, 4096)):
            candidate_index = (start_index + offset) % subnet_count
            candidate_address = int(pool.network_address) + (
                candidate_index * subnet_size
            )
            candidate = ipaddress.ip_network((candidate_address, subnet_prefix))
            ipam = docker.types.IPAMConfig(
                pool_configs=[docker.types.IPAMPool(subnet=str(candidate))]
            )
            try:
                network = docker_client.networks.create(
                    name,
                    driver="bridge",
                    internal=internal,
                    check_duplicate=True,
                    labels={
                        **expected_labels,
                        _LABEL_CREATED_AT: f"{time.time():.6f}",
                    },
                    ipam=ipam,
                    options=dict(options or {}),
                )
                break
            except docker.errors.APIError as exc:
                # Another backend/worker may have won the same-name create.
                try:
                    network = docker_client.networks.get(name)
                except docker.errors.NotFound:
                    detail = str(exc).lower()
                    if not any(
                        marker in detail
                        for marker in (
                            "overlap",
                            "address pool",
                            "pool overlaps",
                        )
                    ):
                        raise
                    continue
                break
        if network is None:
            raise RuntimeError(
                "No non-overlapping subnet is available in the configured "
                "runtime network address pool"
            )
    _verify_network_owner(
        network,
        runtime_kind,
        container_name,
        owner_id,
        network_role=network_role,
        deployment_id=deployment_id,
    )
    _verify_network_shape(
        network,
        internal=internal,
        require_icc_disabled=(
            network_role == RUNTIME_NETWORK_ROLE_EGRESS
        ),
    )
    return network


def ensure_private_runtime_network(
    docker_client: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    create: bool = True,
    address_pool: str = "10.240.0.0/12",
    subnet_prefix: int = 28,
    deployment_id: str | None = None,
) -> str | None:
    """Create/verify one internal control bridge and attach trusted clients.

    Internal Docker networks install no default route. Attaching many of these
    bridges therefore cannot steal the backend/worker's control-plane egress
    route on Docker engines that predate endpoint ``GwPriority``.
    """

    running_in_container = _running_in_container()
    deployment_id = _runtime_deployment_id(deployment_id)
    gateway = None
    current = None
    if running_in_container:
        # This is intentionally checked before creating or attaching anything.
        # API and Celery worker containers have separate network namespaces.
        gateway, current = _validated_runtime_gateway(
            docker_client,
            deployment_id,
        )

    name = runtime_network_name(runtime_kind, container_name, deployment_id)
    network = _ensure_owned_bridge(
        docker_client,
        name=name,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        network_role=RUNTIME_NETWORK_ROLE_CONTROL,
        internal=True,
        create=create,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        deployment_id=deployment_id,
    )
    if network is None:
        return None

    if running_in_container:
        _attach_exact_alias(
            network,
            gateway,
            network_name=name,
            alias=RUNTIME_GATEWAY_ALIAS,
        )
        if getattr(current, "id", None) != getattr(gateway, "id", None):
            _attach_exact_alias(
                network,
                current,
                network_name=name,
                alias=RUNTIME_CLIENT_ALIAS,
            )
    return name


def ensure_runtime_egress_network(
    docker_client: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    create: bool = True,
    address_pool: str = "10.240.0.0/12",
    subnet_prefix: int = 28,
    deployment_id: str | None = None,
) -> str | None:
    """Create/verify the runtime's one-container external egress bridge."""

    deployment_id = _runtime_deployment_id(deployment_id)
    if _running_in_container():
        _validated_runtime_gateway(docker_client, deployment_id)
    name = runtime_egress_network_name(
        runtime_kind,
        container_name,
        deployment_id,
    )
    network = _ensure_owned_bridge(
        docker_client,
        name=name,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        network_role=RUNTIME_NETWORK_ROLE_EGRESS,
        internal=False,
        create=create,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        options={"com.docker.network.bridge.enable_icc": "false"},
        deployment_id=deployment_id,
    )
    return name if network is not None else None


def connect_runtime_to_private_network(
    docker_client: Any,
    container: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    deployment_id: str | None = None,
) -> str:
    """Attach the exact runtime endpoint to its internal control bridge."""

    deployment_id = _runtime_deployment_id(deployment_id)
    name = runtime_network_name(runtime_kind, container_name, deployment_id)
    network = docker_client.networks.get(name)
    _verify_network_owner(
        network,
        runtime_kind,
        container_name,
        owner_id,
        network_role=RUNTIME_NETWORK_ROLE_CONTROL,
        deployment_id=deployment_id,
    )
    _verify_network_shape(network, internal=True)
    container.reload()
    labels = dict(((container.attrs or {}).get("Config") or {}).get("Labels") or {})
    expected_owner_label = (
        "ai-manus.session_id" if runtime_kind == "sandbox" else "ai-manus.claw_id"
    )
    if (
        labels.get(_LABEL_KIND) != runtime_kind
        or (owner_id is not None and labels.get(expected_owner_label) != owner_id)
        or labels.get(_LABEL_DEPLOYMENT_DIGEST)
        != runtime_deployment_digest(deployment_id)
        or labels.get(_LABEL_GENERATION_DIGEST)
        != runtime_generation_digest(container_name)
    ):
        raise PermissionError(
            "Refusing to attach a runtime container with mismatched ownership"
        )
    network.reload()
    endpoints = dict((network.attrs or {}).get("Containers") or {})
    if getattr(container, "id", None) not in endpoints:
        network.connect(container)
    return container_ip_on_network(container, name)


def connect_runtime_to_egress_network(
    docker_client: Any,
    container: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    deployment_id: str | None = None,
) -> str:
    """Attach the exact runtime to its one-container outbound bridge."""

    deployment_id = _runtime_deployment_id(deployment_id)
    name = runtime_egress_network_name(
        runtime_kind,
        container_name,
        deployment_id,
    )
    network = docker_client.networks.get(name)
    _verify_network_owner(
        network,
        runtime_kind,
        container_name,
        owner_id,
        network_role=RUNTIME_NETWORK_ROLE_EGRESS,
        deployment_id=deployment_id,
    )
    _verify_network_shape(
        network,
        internal=False,
        require_icc_disabled=True,
    )
    container.reload()
    labels = dict(((container.attrs or {}).get("Config") or {}).get("Labels") or {})
    expected_owner_label = (
        "ai-manus.session_id" if runtime_kind == "sandbox" else "ai-manus.claw_id"
    )
    if (
        labels.get(_LABEL_KIND) != runtime_kind
        or (owner_id is not None and labels.get(expected_owner_label) != owner_id)
        or labels.get(_LABEL_DEPLOYMENT_DIGEST)
        != runtime_deployment_digest(deployment_id)
        or labels.get(_LABEL_GENERATION_DIGEST)
        != runtime_generation_digest(container_name)
    ):
        raise PermissionError(
            "Refusing to attach a runtime container with mismatched ownership"
        )
    network.reload()
    endpoints = set((network.attrs or {}).get("Containers") or {})
    container_id = getattr(container, "id", None)
    if endpoints - {container_id}:
        raise PermissionError("Runtime egress network contains a foreign endpoint")
    if container_id not in endpoints:
        network.connect(container)
    return container_ip_on_network(container, name)


def container_ip_on_network(container: Any, network_name: str) -> str:
    """Return only the address on the expected private bridge."""

    container.reload()
    networks = (
        ((container.attrs or {}).get("NetworkSettings") or {}).get("Networks")
        or {}
    )
    address = (networks.get(network_name) or {}).get("IPAddress") or ""
    if not address:
        raise RuntimeError("Runtime container has no private-network address")
    return address


def resolve_owned_runtime_control_ip(
    docker_client: Any,
    container: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str,
    deployment_id: str | None = None,
    address_pool: str = "10.240.0.0/12",
    subnet_prefix: int = 28,
) -> str | None:
    """Repair a v2 runtime's bridge pair and return its control address.

    Any managed companion bridge (or v2 container labels) commits this lookup
    to the isolated topology.  Only a wholly unscoped v1 container with no v2
    bridges may fall back to the legacy shared-network path.
    """

    deployment_id = _runtime_deployment_id(deployment_id)
    control_name = ensure_private_runtime_network(
        docker_client,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        create=False,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        deployment_id=deployment_id,
    )
    egress_name = ensure_runtime_egress_network(
        docker_client,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        create=False,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        deployment_id=deployment_id,
    )
    labels = dict(((container.attrs or {}).get("Config") or {}).get("Labels") or {})
    v2_container = (
        labels.get(_LABEL_DEPLOYMENT_DIGEST) is not None
        or labels.get(_LABEL_GENERATION_DIGEST) is not None
    )
    if not control_name and not egress_name and not v2_container:
        verify_owned_runtime_container(
            container,
            runtime_kind=runtime_kind,
            container_name=container_name,
            owner_id=owner_id,
            deployment_id=deployment_id,
            allow_legacy=True,
        )
        return None

    verify_owned_runtime_container(
        container,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        deployment_id=deployment_id,
    )
    # Ensure the external bridge first and attach it before the internal
    # control bridge. The internal bridge cannot provide a default route.
    egress_name = ensure_runtime_egress_network(
        docker_client,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        deployment_id=deployment_id,
    )
    connect_runtime_to_egress_network(
        docker_client,
        container,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        deployment_id=deployment_id,
    )
    control_name = ensure_private_runtime_network(
        docker_client,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        address_pool=address_pool,
        subnet_prefix=subnet_prefix,
        deployment_id=deployment_id,
    )
    connect_runtime_to_private_network(
        docker_client,
        container,
        runtime_kind=runtime_kind,
        container_name=container_name,
        owner_id=owner_id,
        deployment_id=deployment_id,
    )
    container_ip_on_network(container, egress_name)
    return container_ip_on_network(container, control_name)


def owned_runtime_network_intent_exists(
    docker_client: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    deployment_id: str | None = None,
) -> bool:
    """Confirm whether either exact, deployment-scoped intent bridge exists.

    Creation pre-publishes these bridges before ``containers.run``.  A remote
    replica must therefore treat an absent container plus an existing bridge
    as an indeterminate create, not as proof that deletion is complete.
    """

    deployment_id = _runtime_deployment_id(deployment_id)
    found = False
    for name, role, internal in (
        (
            runtime_network_name(runtime_kind, container_name, deployment_id),
            RUNTIME_NETWORK_ROLE_CONTROL,
            True,
        ),
        (
            runtime_egress_network_name(
                runtime_kind,
                container_name,
                deployment_id,
            ),
            RUNTIME_NETWORK_ROLE_EGRESS,
            False,
        ),
    ):
        try:
            network = docker_client.networks.get(name)
        except docker.errors.NotFound:
            continue
        _verify_network_owner(
            network,
            runtime_kind,
            container_name,
            owner_id,
            network_role=role,
            deployment_id=deployment_id,
        )
        _verify_network_shape(
            network,
            internal=internal,
            require_icc_disabled=(role == RUNTIME_NETWORK_ROLE_EGRESS),
        )
        found = True
    return found


def remove_private_runtime_network(
    docker_client: Any,
    *,
    runtime_kind: str,
    container_name: str,
    owner_id: str | None,
    deployment_id: str | None = None,
) -> bool:
    """Remove both exact-owner control and egress bridges fail closed.

    Ownership and every live endpoint are validated on *both* networks before
    either network is mutated. This prevents partial cleanup if a deterministic
    name was manually reused between inspection and session deletion.
    """

    deployment_id = _runtime_deployment_id(deployment_id)
    try:
        docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        pass
    else:
        raise RuntimeError(
            "Refusing to remove runtime networks while the named container exists"
        )

    specs = (
        (
            runtime_network_name(runtime_kind, container_name, deployment_id),
            RUNTIME_NETWORK_ROLE_CONTROL,
            True,
        ),
        (
            runtime_egress_network_name(
                runtime_kind,
                container_name,
                deployment_id,
            ),
            RUNTIME_NETWORK_ROLE_EGRESS,
            False,
        ),
    )
    verified: list[tuple[Any, str, list[Any]]] = []
    for name, role, internal in specs:
        try:
            network = docker_client.networks.get(name)
        except docker.errors.NotFound:
            continue
        _verify_network_owner(
            network,
            runtime_kind,
            container_name,
            owner_id,
            network_role=role,
            deployment_id=deployment_id,
        )
        _verify_network_shape(
            network,
            internal=internal,
            require_icc_disabled=(role == RUNTIME_NETWORK_ROLE_EGRESS),
        )
        network.reload()
        trusted_endpoints: list[Any] = []
        for endpoint_id in sorted(
            dict((network.attrs or {}).get("Containers") or {})
        ):
            # The runtime container must already have been removed. A
            # same-name replacement or any unknown endpoint makes cleanup
            # fail closed. Only backend/worker aliases are removable, and
            # those are valid exclusively on the internal control network.
            try:
                endpoint = docker_client.containers.get(endpoint_id)
            except docker.errors.NotFound:
                continue
            endpoint.reload()
            labels = dict(
                ((endpoint.attrs or {}).get("Config") or {}).get("Labels") or {}
            )
            if labels.get(_LABEL_KIND) in {"sandbox", "claw"}:
                raise RuntimeError(
                    "Refusing to remove a private network with a live runtime endpoint"
                )
            if (
                role != RUNTIME_NETWORK_ROLE_CONTROL
                or not _has_trusted_runtime_alias(endpoint, name)
            ):
                raise RuntimeError(
                    "Refusing to disconnect an unrecognized private-network endpoint"
                )
            trusted_endpoints.append(endpoint)
        verified.append((network, name, trusted_endpoints))

    for network, _name, trusted_endpoints in verified:
        for endpoint in trusted_endpoints:
            network.disconnect(endpoint, force=True)
        network.reload()
        if (network.attrs or {}).get("Containers"):
            raise RuntimeError(
                "Private runtime network still has attached endpoints"
            )
        network.remove()
    return True


def gc_orphaned_runtime_networks(
    settings: Any,
    *,
    now: float | None = None,
) -> int:
    """Reclaim old owner-labelled bridges after runtime AutoRemove.

    A grace window protects creation before ``containers.run`` becomes
    visible. Cleanup then rechecks the deterministic container name and every
    endpoint immediately before mutation, so a late/retried create either
    blocks cleanup or recreates a bridge that was removed just before attach.
    """

    if not bool(getattr(settings, "runtime_network_isolation", False)):
        return 0
    dynamic_docker_sandbox = (
        str(getattr(settings, "sandbox_provider", "docker")).strip().lower()
        == "docker"
        and not getattr(settings, "sandbox_address", None)
    )
    dynamic_docker_claw = bool(getattr(settings, "claw_enabled", False)) and not (
        getattr(settings, "claw_address", None)
    )
    if not dynamic_docker_sandbox and not dynamic_docker_claw:
        return 0

    observed_at = time.time() if now is None else float(now)
    grace_seconds = max(
        60.0,
        float(getattr(settings, "runtime_network_gc_grace_seconds", 300)),
    )
    deployment_id = _runtime_deployment_id(
        getattr(settings, "runtime_deployment_id", None)
    )
    deployment_digest = runtime_deployment_digest(deployment_id)
    docker_client = docker.from_env(timeout=15.0)
    try:
        grouped: dict[tuple[str, str], list[Any]] = {}
        networks = docker_client.networks.list(
            filters={"label": f"{_LABEL_KIND}={RUNTIME_NETWORK_KIND}"}
        )
        for network in networks:
            labels = _network_labels(network)
            if labels.get(_LABEL_DEPLOYMENT_DIGEST) != deployment_digest:
                # Unscoped v1 bridges and other deployments are never adopted
                # or mutated by this deployment's automatic reconciler.
                continue
            runtime_kind = labels.get(_LABEL_RUNTIME_KIND)
            container_name = labels.get(_LABEL_RUNTIME_CONTAINER)
            role = labels.get(_LABEL_NETWORK_ROLE)
            if (
                runtime_kind not in {"sandbox", "claw"}
                or not container_name
                or role
                not in {
                    RUNTIME_NETWORK_ROLE_CONTROL,
                    RUNTIME_NETWORK_ROLE_EGRESS,
                }
            ):
                raise PermissionError("Malformed AI Manus private runtime network")
            _verify_network_owner(
                network,
                runtime_kind,
                container_name,
                owner_id=None,
                network_role=role,
                deployment_id=deployment_id,
            )
            _verify_network_shape(
                network,
                internal=(role == RUNTIME_NETWORK_ROLE_CONTROL),
                require_icc_disabled=(role == RUNTIME_NETWORK_ROLE_EGRESS),
            )
            try:
                created_at = float(labels[_LABEL_CREATED_AT])
            except (KeyError, TypeError, ValueError) as exc:
                raise PermissionError(
                    "Runtime network has no valid creation timestamp"
                ) from exc
            if created_at > observed_at or observed_at - created_at < grace_seconds:
                continue
            grouped.setdefault((runtime_kind, container_name), []).append(network)

        cleaned = 0
        for (runtime_kind, container_name), eligible_networks in grouped.items():
            # If one companion bridge is still inside the grace period, the
            # group is an in-flight/partial create and must not be collected.
            all_names = {
                runtime_network_name(
                    runtime_kind,
                    container_name,
                    deployment_id,
                ),
                runtime_egress_network_name(
                    runtime_kind,
                    container_name,
                    deployment_id,
                ),
            }
            existing_names = set()
            has_recent_companion = False
            for name in all_names:
                try:
                    companion = docker_client.networks.get(name)
                except docker.errors.NotFound:
                    continue
                existing_names.add(name)
                companion_labels = _network_labels(companion)
                try:
                    companion_created = float(
                        companion_labels[_LABEL_CREATED_AT]
                    )
                except (KeyError, TypeError, ValueError):
                    has_recent_companion = True
                    break
                if observed_at - companion_created < grace_seconds:
                    has_recent_companion = True
                    break
            if has_recent_companion or not existing_names:
                continue
            try:
                docker_client.containers.get(container_name)
            except docker.errors.NotFound:
                pass
            else:
                continue
            try:
                remove_private_runtime_network(
                    docker_client,
                    runtime_kind=runtime_kind,
                    container_name=container_name,
                    owner_id=None,
                    deployment_id=deployment_id,
                )
            except (PermissionError, RuntimeError) as exc:
                logger.info(
                    "Deferred orphan runtime network cleanup for %s (%s)",
                    container_name,
                    type(exc).__name__,
                )
                continue
            cleaned += 1
        return cleaned
    finally:
        close = getattr(docker_client, "close", None)
        if callable(close):
            close()


def assert_legacy_network_has_at_most_one_runtime(
    docker_client: Any,
    *,
    network_names: Iterable[str | None],
    sandbox_name_prefix: str | None,
    claw_name_prefix: str | None,
    deployment_project: str | None = None,
) -> None:
    """Fail closed when multiple pre-isolation runtimes share one bridge.

    One already-running legacy runtime may be retained during a non-destructive
    deployment.  Every newly created runtime uses a private bridge, so that
    retained container cannot reach another user runtime.  Two legacy runtime
    endpoints on the same bridge would preserve the old lateral-access flaw
    and therefore prevent startup.
    """

    prefixes = tuple(
        dict.fromkeys(
            prefix
            for prefix in (
                sandbox_name_prefix,
                claw_name_prefix,
                "sandbox",
                "manus-sandbox",
                "ai-manus-sandbox",
                "None",
                "claw",
                "manus-claw",
                "ai-manus-claw",
            )
            if prefix
        )
    )

    def is_runtime(endpoint: Any) -> bool:
        labels = dict(
            ((endpoint.attrs or {}).get("Config") or {}).get("Labels") or {}
        )
        if labels.get(_LABEL_KIND) in {"sandbox", "claw"}:
            return True
        endpoint_name = str(getattr(endpoint, "name", "") or "")
        if any(
            endpoint_name == prefix
            or endpoint_name.startswith(f"{prefix}-")
            for prefix in prefixes
        ):
            return True
        config = (endpoint.attrs or {}).get("Config") or {}
        image = str(config.get("Image") or "").lower()
        if "manus-sandbox" in image or "manus-claw" in image:
            return True
        environment_keys = {
            str(item).split("=", 1)[0]
            for item in (config.get("Env") or [])
        }
        return (
            {"SERVICE_TIMEOUT_MINUTES", "CHROME_ARGS"}
            <= environment_keys
            or {"CLAW_TTL_SECONDS", "MANUS_API_BASE_URL"}
            <= environment_keys
        )

    network_names_to_scan = {name for name in network_names if name}
    if deployment_project:
        for network in docker_client.networks.list(
            filters={
                "label": (
                    "com.docker.compose.project=" f"{deployment_project}"
                )
            }
        ):
            labels = _network_labels(network)
            if labels.get("com.docker.compose.project") != deployment_project:
                continue
            name = str(getattr(network, "name", "") or "")
            if name:
                network_names_to_scan.add(name)

    for network_name_value in sorted(network_names_to_scan):
        try:
            network = docker_client.networks.get(network_name_value)
        except docker.errors.NotFound:
            continue
        network.reload()
        runtime_names: list[str] = []
        for endpoint_id in ((network.attrs or {}).get("Containers") or {}):
            try:
                endpoint = docker_client.containers.get(endpoint_id)
            except docker.errors.NotFound:
                continue
            endpoint.reload()
            if not bool(
                ((endpoint.attrs or {}).get("State") or {}).get("Running")
            ):
                continue
            endpoint_name = str(getattr(endpoint, "name", "") or "")
            if is_runtime(endpoint):
                runtime_names.append(endpoint_name or endpoint_id[:12])
        if len(runtime_names) > 1:
            raise RuntimeError(
                "Multiple legacy user runtimes share Docker network "
                f"{network_name_value}; refusing insecure startup"
            )


def reattach_backend_to_private_runtime_networks(
    docker_client: Any,
    settings: Any | None = None,
) -> None:
    """Reconnect a recreated backend container to every live private runtime."""

    if not _running_in_container():
        return
    deployment_id = _runtime_deployment_id(
        getattr(settings, "runtime_deployment_id", None)
        if settings is not None
        else None
    )
    deployment_digest = runtime_deployment_digest(deployment_id)
    backend = docker_client.containers.get(_gateway_reference())
    backend.reload()
    current = docker_client.containers.get(_current_container_reference())
    current.reload()
    _assert_gateway_deployment_scope(backend, current, deployment_id)
    backend_labels = dict(
        ((backend.attrs or {}).get("Config") or {}).get("Labels") or {}
    )
    if (
        getattr(current, "id", None) != getattr(backend, "id", None)
        and backend_labels.get("com.docker.compose.service") != "backend"
        and backend_labels.get("ai-manus.runtime_gateway") != "true"
    ):
        raise PermissionError(
            "Runtime gateway reattachment target is not an AI Manus backend"
        )
    networks = docker_client.networks.list(
        filters={"label": f"{_LABEL_KIND}={RUNTIME_NETWORK_KIND}"}
    )
    for network in networks:
        labels = _network_labels(network)
        if labels.get(_LABEL_DEPLOYMENT_DIGEST) != deployment_digest:
            continue
        runtime_kind = labels.get(_LABEL_RUNTIME_KIND)
        container_name = labels.get(_LABEL_RUNTIME_CONTAINER)
        network_role = labels.get(_LABEL_NETWORK_ROLE)
        if (
            runtime_kind not in {"sandbox", "claw"}
            or not container_name
            or network_role
            not in {
                RUNTIME_NETWORK_ROLE_CONTROL,
                RUNTIME_NETWORK_ROLE_EGRESS,
            }
        ):
            raise PermissionError("Malformed AI Manus private runtime network")
        _verify_network_owner(
            network,
            runtime_kind,
            container_name,
            owner_id=None,
            network_role=network_role,
            deployment_id=deployment_id,
        )
        _verify_network_shape(
            network,
            internal=(network_role == RUNTIME_NETWORK_ROLE_CONTROL),
            require_icc_disabled=(
                network_role == RUNTIME_NETWORK_ROLE_EGRESS
            ),
        )
        try:
            runtime = docker_client.containers.get(container_name)
        except docker.errors.NotFound:
            # Durable provider cleanup owns orphan-network reconciliation.
            continue
        runtime.reload()
        if not bool(((runtime.attrs or {}).get("State") or {}).get("Running")):
            continue
        runtime_labels = dict(
            ((runtime.attrs or {}).get("Config") or {}).get("Labels") or {}
        )
        owner_label = (
            "ai-manus.session_id" if runtime_kind == "sandbox"
            else "ai-manus.claw_id"
        )
        owner_id = runtime_labels.get(owner_label)
        if (
            runtime_labels.get(_LABEL_KIND) != runtime_kind
            or runtime_labels.get(_LABEL_DEPLOYMENT_DIGEST)
            != deployment_digest
            or runtime_labels.get(_LABEL_GENERATION_DIGEST)
            != runtime_generation_digest(container_name)
        ):
            raise PermissionError(
                "Private runtime network points to a foreign container"
            )
        _verify_network_owner(
            network,
            runtime_kind,
            container_name,
            owner_id,
            network_role=network_role,
            deployment_id=deployment_id,
        )
        address_pool = str(
            getattr(settings, "runtime_network_address_pool", "10.240.0.0/12")
        )
        subnet_prefix = int(
            getattr(settings, "runtime_network_subnet_prefix", 28)
        )
        if network_role == RUNTIME_NETWORK_ROLE_EGRESS:
            ensure_private_runtime_network(
                docker_client,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                address_pool=address_pool,
                subnet_prefix=subnet_prefix,
                deployment_id=deployment_id,
            )
            connect_runtime_to_private_network(
                docker_client,
                runtime,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                deployment_id=deployment_id,
            )
            connect_runtime_to_egress_network(
                docker_client,
                runtime,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                deployment_id=deployment_id,
            )
        else:
            ensure_runtime_egress_network(
                docker_client,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                address_pool=address_pool,
                subnet_prefix=subnet_prefix,
                deployment_id=deployment_id,
            )
            connect_runtime_to_egress_network(
                docker_client,
                runtime,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                deployment_id=deployment_id,
            )
            connect_runtime_to_private_network(
                docker_client,
                runtime,
                runtime_kind=runtime_kind,
                container_name=container_name,
                owner_id=owner_id,
                deployment_id=deployment_id,
            )
        container_ip_on_network(runtime, network.name)
        network.reload()
        endpoint_ids = set((network.attrs or {}).get("Containers") or {})
        runtime_id = getattr(runtime, "id", None)
        if runtime_id not in endpoint_ids:
            raise PermissionError(
                "Private runtime network is missing its exact runtime endpoint"
            )
        if network_role == RUNTIME_NETWORK_ROLE_EGRESS:
            if endpoint_ids != {runtime_id}:
                raise PermissionError(
                    "Runtime egress network contains a foreign endpoint"
                )
            continue
        for endpoint_id in endpoint_ids - {runtime_id}:
            try:
                endpoint = docker_client.containers.get(endpoint_id)
            except docker.errors.NotFound as exc:
                raise PermissionError(
                    "Runtime control network contains an unknown endpoint"
                ) from exc
            if not _has_trusted_runtime_alias(endpoint, network.name):
                raise PermissionError(
                    "Runtime control network contains an untrusted endpoint"
                )
        _attach_exact_alias(
            network,
            backend,
            network_name=network.name,
            alias=RUNTIME_GATEWAY_ALIAS,
        )


def audit_legacy_runtime_networks(settings: Any) -> None:
    """Run the legacy shared-bridge invariant in a bounded worker thread."""

    if not bool(getattr(settings, "runtime_network_isolation", False)):
        return
    dynamic_docker_sandbox = (
        str(getattr(settings, "sandbox_provider", "docker")).strip().lower()
        == "docker"
        and not getattr(settings, "sandbox_address", None)
    )
    dynamic_docker_claw = bool(getattr(settings, "claw_enabled", False)) and not (
        getattr(settings, "claw_address", None)
    )
    if not dynamic_docker_sandbox and not dynamic_docker_claw:
        return
    require_containerized_runtime_gateway()
    assert_runtime_process_forwarding_disabled()
    legacy_network_names: list[str] = []
    if dynamic_docker_sandbox:
        legacy_network_names.append(
            getattr(settings, "sandbox_network", None) or "bridge"
        )
    if dynamic_docker_claw:
        legacy_network_names.append(
            getattr(settings, "claw_network", None) or "bridge"
        )
    docker_client = docker.from_env(timeout=15.0)
    try:
        gateway = docker_client.containers.get(_gateway_reference())
        gateway.reload()
        gateway_config_labels = dict(
            ((gateway.attrs or {}).get("Config") or {}).get("Labels") or {}
        )
        deployment_project = gateway_config_labels.get(
            "com.docker.compose.project"
        )
        legacy_network_names.extend(
            (
                ((gateway.attrs or {}).get("NetworkSettings") or {}).get(
                    "Networks"
                )
                or {}
            ).keys()
        )
        assert_legacy_network_has_at_most_one_runtime(
            docker_client,
            network_names=legacy_network_names,
            sandbox_name_prefix=(
                getattr(settings, "sandbox_name_prefix", None) or "None"
            ),
            claw_name_prefix=getattr(settings, "claw_name_prefix", None),
            deployment_project=deployment_project,
        )
        reattach_backend_to_private_runtime_networks(docker_client, settings)
    finally:
        close = getattr(docker_client, "close", None)
        if callable(close):
            close()
