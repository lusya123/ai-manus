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
$COMPOSE -p "$PROJECT_NAME" -f docker-compose-development.yml "$@"
