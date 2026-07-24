#!/bin/bash

# Determine which Docker Compose command to use
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE="docker-compose"
else
    echo "Error: Neither docker compose nor docker-compose command found" >&2
    exit 1
fi


# Execute Docker Compose command
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-manus}"
if [ -n "${CORE_RESOURCE_PREFIX:-}" ]; then
    RESOURCE_PREFIX="$CORE_RESOURCE_PREFIX"
elif [ "$PROJECT_NAME" = "ai-manus" ]; then
    # Preserve the historical production network/volume names.
    RESOURCE_PREFIX="manus"
else
    # A custom Compose project must never share the default deployment's
    # control network, Mongo volume, or Redis volume on the same Docker host.
    RESOURCE_PREFIX="manus-$PROJECT_NAME"
fi

CORE_RESOURCE_PREFIX="$RESOURCE_PREFIX" \
SANDBOX_NETWORK="${SANDBOX_NETWORK:-$RESOURCE_PREFIX-network}" \
CLAW_NETWORK="${CLAW_NETWORK:-$RESOURCE_PREFIX-network}" \
RUNTIME_DEPLOYMENT_ID="${RUNTIME_DEPLOYMENT_ID:-$PROJECT_NAME}" \
    $COMPOSE -p "$PROJECT_NAME" -f docker-compose.yml "$@"
