# AI Manus

English | [中文](README_zh.md) | [Official Site](https://ai-manus.com) | [Documents](https://docs.ai-manus.com/#/en/)

[![GitHub stars](https://img.shields.io/github/stars/simpleyyt/ai-manus?style=social)](https://github.com/simpleyyt/ai-manus/stargazers)
&ensp;
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

AI Manus is a general-purpose AI Agent system that supports running various tools and operations in a sandbox environment. Now with **Claw** — a deeply integrated [OpenClaw](https://github.com/anthropics/openclaw) AI assistant that brings one-click deployment, persistent per-user isolated containers, optional expiry policies, and seamless chat history to the Manus ecosystem.

Enjoy your own agent with AI Manus!

👏 Join [QQ Group(1005477581)](https://qun.qq.com/universal-share/share?ac=1&authKey=p4X3Da5iMpR4liAenxwvhs7IValPKiCFtUevRlJouz9qSTSZsMnPJc3hzsJjgQYv&busi_data=eyJncm91cENvZGUiOiIxMDA1NDc3NTgxIiwidG9rZW4iOiJNZmUrTmQ0UzNDZDNqNDFVdjVPS1VCRkJGRWVlV0R3RFJSRVFoZDAwRjFDeUdUM0t6aUIyczlVdzRjV1BYN09IIiwidWluIjoiMzQyMjExODE1In0%3D&data=C3B-E6BlEbailV32co77iXL5vxPIhtD9y_itWLSq50hKqosO_55_isOZym2Faaq4hs9-517tUY8GSWaDwPom-A&svctype=4&tempid=h5_group_info)

❤️ Like AI Manus? Give it a star 🌟 or [Sponsor](docs/sponsor.md) to support the development!

🚀 [Try a Demo](https://app.ai-manus.com)

📝 [Blog: Rebuild Manus with WebUI and Sandbox](https://simpleyyt.com/2026/03/07/rebuild-manus-with-webui-and-sandbox/)

## Demos

### Basic Features

https://github.com/user-attachments/assets/37060a09-c647-4bcb-920c-959f7fa73ebe

### Browser Use

* Task: Latest LLM papers

<https://github.com/user-attachments/assets/4e35bc4d-024a-4617-8def-a537a94bd285>

### Code Use

* Task: Write a complex Python example

<https://github.com/user-attachments/assets/765ea387-bb1c-4dc2-b03e-716698feef77>


## Key Features

 * Deployment: Minimal deployment requires only an LLM service, with no dependency on other external services.
 * Tools: Supports Terminal, Browser, File, Web Search, and messaging tools with real-time viewing and takeover capabilities, supports external MCP tool integration.
 * Claw: Integrated [OpenClaw](https://github.com/anthropics/openclaw) AI assistant with one-click deployment, persistent per-user isolated containers, optional expiry policies, and full chat history.
 * Sandbox: Each task gets an isolated sandbox using local Docker or optional [Alibaba Cloud Wuying AgentBay](docs/en/agentbay.md).
 * Task Sessions: Session history is managed through MongoDB/Redis, supporting background tasks.
 * Conversations: Supports stopping and interrupting, file upload and download.
 * Multilingual: Supports both Chinese and English.
 * Authentication: User login and authentication.

## Development Roadmap

 * Tools: Support for Deploy & Expose.
 * Sandbox: Support for mobile and Windows computer access.
 * Deployment: Support for K8s and Docker Swarm multi-cluster deployment.

### Overall Design

![Image](https://github.com/user-attachments/assets/69775011-1eb7-452f-adaf-cd6603a4dde5)

**When a user initiates a conversation:**

1. Web sends a request to create an Agent to the Server, which creates a Sandbox through `/var/run/docker.sock` and returns a session ID.
2. The Sandbox is an Ubuntu Docker environment that starts Chrome browser and API services for tools like File/Shell.
3. Web sends user messages to the session ID, and when the Server receives user messages, it forwards them to the PlanAct Agent for processing.
4. During processing, the PlanAct Agent calls relevant tools to complete tasks.
5. All events generated during Agent processing are sent back to Web via SSE.

**When users browse tools:**

- Browser:
    1. The Sandbox's headless browser starts a VNC service through xvfb and x11vnc, and converts VNC to websocket through websockify.
    2. Web's NoVNC component connects to the Sandbox through the Server's Websocket Forward, enabling browser viewing.
- Other tools: Other tools work on similar principles.

## Environment Requirements

This project primarily relies on Docker for development and deployment, requiring a relatively new version of Docker:
- Docker 20.10+
- Docker Compose

Model capability requirements:
- Supports LangChain chat model providers (default `openai`)
- Support for FunctionCall
- Support for Json Format output

Deepseek and GPT models are recommended.

## Deployment Guide

Docker Compose is recommended for deployment:

<!-- docker-compose-example.yml -->
```yaml
services:
  frontend:
    image: simpleyyt/manus-frontend
    ports:
      - "5173:80"
    depends_on:
      - backend
    restart: unless-stopped
    networks:
      - manus-network
    environment:
      - BACKEND_URL=http://backend:8000

  backend:
    image: simpleyyt/manus-backend
    depends_on:
      - sandbox
      - claw
    restart: unless-stopped
    sysctls:
      net.ipv4.ip_forward: "0"
      net.ipv4.conf.all.forwarding: "0"
      net.ipv4.conf.default.forwarding: "0"
      net.ipv6.conf.all.forwarding: "0"
      net.ipv6.conf.default.forwarding: "0"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      #- ./mcp.json:/etc/mcp.json # Mount MCP servers directory
    networks:
      - manus-network
      - manus-data-network
    env_file:
      # All configuration is loaded from the .env file, see .env.example
      # More configuration options: https://docs.ai-manus.com/#/configuration
      - .env
    environment:
      # Keep deployment security and runtime isolation as explicit overrides.
      - API_KEY=${API_KEY:?required}
      - JWT_SECRET_KEY=${JWT_SECRET_KEY:?required}
      - REGISTRATION_ENABLED=false
      - DEPLOYMENT_ENVIRONMENT=production
      - CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS:?required}
      - SANDBOX_IMAGE=simpleyyt/manus-sandbox
      - SANDBOX_NETWORK=manus-network
      - SANDBOX_MEMORY_LIMIT=2g
      - SANDBOX_CPU_LIMIT=2.0
      - SANDBOX_PIDS_LIMIT=512
      - CLAW_IMAGE=simpleyyt/manus-claw
      - CLAW_NETWORK=manus-network
      - RUNTIME_NETWORK_ISOLATION=true
      - RUNTIME_DEPLOYMENT_ID=${RUNTIME_DEPLOYMENT_ID:-ai-manus}
      - CLAW_PUBLISH_HOST_PORTS=false

  sandbox:
    image: simpleyyt/manus-sandbox
    command: /bin/sh -c "exit 0"  # prevent sandbox from starting, ensure image is pulled
    restart: "no"
    networks:
      - manus-network

  claw:
    image: simpleyyt/manus-claw
    entrypoint: /bin/sh -c "exit 0"  # prevent claw from starting, ensure image is pulled
    restart: "no"
    networks:
      - manus-network

  mongodb:
    image: mongo:7.0
    volumes:
      - mongodb_data:/data/db
    restart: unless-stopped
    #ports:
    #  - "27017:27017"
    networks:
      - manus-data-network

  redis:
    image: redis:7.0
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--maxmemory-policy", "noeviction"]
    volumes:
      - redis_data:/data
    restart: unless-stopped
    networks:
      - manus-data-network

volumes:
  mongodb_data:
    name: manus-mongodb-data
  redis_data:
    name: manus-redis-data

networks:
  manus-network:
    name: manus-network
    driver: bridge
  manus-data-network:
    name: manus-data-network
    driver: bridge
    internal: true
```
<!-- /docker-compose-example.yml -->

Save as `docker-compose.yml`. Model and feature configuration is loaded from a `.env` file, while the Compose file keeps security and runtime-isolation invariants as explicit overrides. Create `.env` next to it based on [.env.example](https://github.com/simpleyyt/ai-manus/blob/main/.env.example). At minimum set `API_KEY` and generate a unique `JWT_SECRET_KEY` with `openssl rand -hex 32`:

```ini
API_KEY=
# Paste the unique output of: openssl rand -hex 32
JWT_SECRET_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
```

Then run:

```shell
docker compose up -d
```

> Note: If you see `sandbox-1 exited with code 0`, this is normal, as it ensures the sandbox image is successfully pulled locally.

Open your browser and visit <http://localhost:5173> to access Manus. For more configuration options, see: https://docs.ai-manus.com/#/en/configuration

## Development Guide

### Project Structure

This project consists of the following sub-projects:

* `frontend`: Manus frontend
* `backend`: Manus backend
* `sandbox`: Manus sandbox
* `claw`: Manus Claw — OpenClaw plugin & container image bridging OpenClaw Gateway with Manus backend
* `mockserver`: Mock LLM server (for development/testing)

### Environment Setup

1. Download the project:
```bash
git clone https://github.com/simpleyyt/ai-manus.git
cd ai-manus
```

2. Copy the configuration file:
```bash
cp .env.example .env
```

3. Modify the configuration file. At minimum set `API_KEY` and a unique `JWT_SECRET_KEY` of at least 32 bytes (for example, generate one with `openssl rand -hex 32`). See [.env.example](https://github.com/simpleyyt/ai-manus/blob/main/.env.example) or [Configuration](https://docs.ai-manus.com/#/en/configuration) for the full list of options:

```ini
API_KEY=
# Paste the unique output of: openssl rand -hex 32
JWT_SECRET_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
```

### Development and Debugging

1. Run in debug mode:
```bash
# Equivalent to docker compose -p ai-manus-dev -f docker-compose-development.yml up
./dev.sh up
```

All services will run in reload mode, and code changes will be automatically reloaded. The exposed ports are as follows:
- 5173: Web frontend port
- 8000: Server API service port
- 5678: Server debugpy port (remote Python debugging)
- 8080: Sandbox API service port
- 5902: Sandbox VNC port (mapped to 5900 inside the container)
- 18788: Claw (OpenClaw Gateway) port
- 27017: MongoDB port

> *Note: In Debug mode, only one sandbox will be started globally*

Development MongoDB now uses the dedicated `manus-mongodb-data-dev` volume so
it cannot modify production data. An existing `manus-mongodb-data` volume is
left untouched and is not attached automatically; back it up and migrate only
the data you intentionally want in development. The helper scripts also use
different Compose projects (`ai-manus` and `ai-manus-dev`) and runtime networks.

2. When dependencies change (`backend/pyproject.toml` or `frontend/package.json`), clean up and rebuild:
```bash
# Clean up all related resources
./dev.sh down -v

# Rebuild images
./dev.sh build

# Run in debug mode
./dev.sh up
```

### Image Publishing

```bash
export IMAGE_REGISTRY=your-registry-url
export IMAGE_TAG=latest

# Build images
./run.sh build

# Push to the corresponding image repository
./run.sh push
```

Production Compose also derives dynamically-created Sandbox and Claw images
from `IMAGE_REGISTRY` and `IMAGE_TAG`. Set `SANDBOX_IMAGE` or `CLAW_IMAGE` only
when a runtime intentionally comes from a different repository.

## Production Security Checklist

- Set `DEPLOYMENT_ENVIRONMENT=production` and generate a unique `JWT_SECRET_KEY` of at least 32 bytes. Public registration is closed by default (`REGISTRATION_ENABLED=false`), password hashes use random per-user PBKDF2-SHA256 salts, and authentication endpoints are Redis rate-limited.
- Treat Redis as security state, not a disposable cache. Keep the bundled AOF `everysec`, `noeviction`, and named-volume settings, or give an external Redis equivalent persistence, backups, and high availability. A host crash may still lose roughly the last second of AOF writes, so Redis is not used as the hard AgentBay billing ledger; data loss can erase logout and refresh-replay records.
- Configure only exact `CORS_ALLOWED_ORIGINS`; `*`, paths, query strings, and URL credentials are rejected. Refresh tokens are single-use and rotate inside a family that logout revokes.
- Production BYOK requires an independent `MODEL_CREDENTIAL_ENCRYPTION_KEYS` keyring. Use the dry-run-first `backend/scripts/rotate_model_credential_keys.py` for rotation; old plaintext agent records use `migrate_agent_credentials.py` with the complete temporary `LEGACY_SYSTEM_API_KEYS` history.
- Configure an independent `CLAW_API_KEY_HMAC_KEYS` keyring when Claw is enabled. Claw applies WebSocket message/attachment limits, distributed request/turn limits, upload limits, and a bounded history.
- File uploads have a pre-parser body cap, a per-file cap, and atomic per-user byte/file-count quotas. Keep reverse-proxy limits aligned with backend settings.
- `TASK_BACKEND=local` supports exactly one backend Python process. Declare the real `BACKEND_REPLICA_COUNT`; use Celery for multiple replicas or workers.
- Lifecycle fencing fields (`deleting`, `sandbox_destroying`, and task/runtime generation binding) require a quiesced upgrade: stop every old backend and worker before starting this release. Do not run old and new lifecycle protocols side by side; the bundled fork deployment performs this full stop/start sequence.
- `run.sh` derives a unique `CORE_RESOURCE_PREFIX` for a custom `COMPOSE_PROJECT_NAME`, isolating its control/data networks and Mongo/Redis volumes. Direct custom Compose invocations must set the same unique prefix explicitly; never reuse a prefix between deployments on one Docker daemon.
- Per-runtime control and egress bridges block cross-runtime and datastore access, and expired AutoRemove bridges are garbage-collected. This is not a host/VPC egress firewall: add host firewall rules or a destination-filtering proxy if sandboxes must also be blocked from Docker-host, RFC1918, link-local, or cloud-metadata addresses.
- AgentBay cleanup preserves provider IDs when deletion cannot be confirmed, allowing retries. Its signed gateway URLs are bearer capabilities and must never be written to logs.

See [Configuration](docs/en/configuration.md) and [AgentBay Cloud Sandbox](docs/en/agentbay.md) for the complete operational procedures.

## ⭐️ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Simpleyyt/ai-manus&type=Date)](https://www.star-history.com/#Simpleyyt/ai-manus&Date)
