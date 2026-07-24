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
PROJECT_NAME="${DEV_COMPOSE_PROJECT_NAME:-ai-manus-dev}"
if [ -n "${DEV_RESOURCE_PREFIX:-}" ]; then
    RESOURCE_PREFIX="$DEV_RESOURCE_PREFIX"
elif [ "$PROJECT_NAME" = "ai-manus-dev" ]; then
    # Preserve the historical development network/volume names.
    RESOURCE_PREFIX="manus"
else
    # Custom local projects must not share unauthenticated dev data services
    # or runtime control networks on the same Docker daemon.
    RESOURCE_PREFIX="manus-$PROJECT_NAME"
fi

DEV_RESOURCE_PREFIX="$RESOURCE_PREFIX" \
DEV_SANDBOX_NETWORK="${DEV_SANDBOX_NETWORK:-$RESOURCE_PREFIX-network-dev}" \
DEV_CLAW_NETWORK="${DEV_CLAW_NETWORK:-$RESOURCE_PREFIX-network-dev}" \
RUNTIME_DEPLOYMENT_ID="${RUNTIME_DEPLOYMENT_ID:-$PROJECT_NAME}" \
    $COMPOSE -p "$PROJECT_NAME" -f docker-compose-development.yml "$@"
