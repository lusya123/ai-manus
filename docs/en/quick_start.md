# 🚀 Quick Start

## Environment Requirements

This project mainly relies on Docker for development and deployment, requiring a newer version of Docker:

 * Docker 20.10+
 * Docker Compose

Model capabilities required:

 * Supports LangChain chat models (default provider is `openai`)
 * Supports Function Call
 * Supports JSON Format output

Recommended models: Deepseek and ChatGPT.

## Docker Installation

### Windows & Mac Systems

Install Docker Desktop according to official requirements: https://docs.docker.com/desktop/

### Linux Systems

Install Docker Engine according to official requirements: https://docs.docker.com/engine/

## Deployment

Deploy using Docker Compose. Model and feature configuration is managed through a `.env` file (via `env_file`); deployment security and runtime-isolation invariants remain explicit Compose overrides:

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

Save as `docker-compose.yml` file.

### Create the `.env` Configuration File

Next to `docker-compose.yml`, create a `.env` file based on [`.env.example`](https://github.com/simpleyyt/ai-manus/blob/main/.env.example). At minimum set `API_KEY`, generate a unique `JWT_SECRET_KEY` with `openssl rand -hex 32`, and adjust `API_BASE` and `MODEL_NAME` for your model service:

```ini
API_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
# Paste the unique output of: openssl rand -hex 32
JWT_SECRET_KEY=
```

The full `.env.example` is shown below (search engine, authentication, sandbox, Claw, and more options):

<!-- .env.example -->
```ini
# Model provider configuration
API_KEY=
API_BASE=http://mockserver:8090/v1

# Model configuration
# MODEL_PROVIDER selects the LLM integration (via LangChain init_chat_model).
# Built-in providers: openai, deepseek, anthropic, ollama. OpenAI-compatible
# endpoints (DeepSeek / OneAPI / vLLM / ...) work with openai + API_BASE.
# See docs/configuration.md for per-provider examples.
MODEL_PROVIDER=openai
MODEL_NAME=deepseek-chat
TEMPERATURE=0.7
MAX_TOKENS=2000

# LLM gateway provider: langchain (default) or openai.
# - langchain: uses init_chat_model, supports many providers via MODEL_PROVIDER.
# - openai:    talks to OpenAI / OpenAI-compatible endpoints (API_BASE) directly
#              via the official openai Python SDK (MODEL_PROVIDER is ignored).
#LLM_PROVIDER=langchain

# MongoDB configuration
#MONGODB_URI=mongodb://mongodb:27017
#MONGODB_DATABASE=manus
#MONGODB_USERNAME=
#MONGODB_PASSWORD=

# Redis configuration
#REDIS_HOST=redis
#REDIS_PORT=6379
#REDIS_DB=0
#REDIS_PASSWORD=
# Production Redis is security state. The bundled Compose uses AOF everysec and
# a named volume; external Redis needs equivalent persistence/backups/HA.

# Sandbox configuration
# Provider: docker (default) or agentbay (Alibaba Cloud Wuying)
#SANDBOX_PROVIDER=docker
#SANDBOX_ADDRESS=
# Leave unset for the direct-run default; production Compose follows IMAGE_REGISTRY/IMAGE_TAG
#SANDBOX_IMAGE=
SANDBOX_NAME_PREFIX=sandbox
SANDBOX_TTL_MINUTES=30
SANDBOX_NETWORK=manus-network
SANDBOX_MEMORY_LIMIT=2g
SANDBOX_CPU_LIMIT=2.0
SANDBOX_PIDS_LIMIT=512
# Dynamic Docker runtimes use an internal control bridge plus a one-container
# egress bridge. Each live runtime therefore consumes two /28 subnets.
RUNTIME_NETWORK_ISOLATION=true
#RUNTIME_GATEWAY_CONTAINER=
#RUNTIME_NETWORK_ADDRESS_POOL=10.240.0.0/12
#RUNTIME_NETWORK_SUBNET_PREFIX=28
#RUNTIME_NETWORK_GC_INTERVAL_SECONDS=60
#RUNTIME_NETWORK_GC_GRACE_SECONDS=300
# This blocks cross-runtime/datastore access, not Docker-host, RFC1918,
# link-local, or cloud-metadata egress; use a host firewall/proxy for that.
#SANDBOX_CHROME_ARGS=
#SANDBOX_HTTPS_PROXY=
#SANDBOX_HTTP_PROXY=
#SANDBOX_NO_PROXY=

# AgentBay only. Inject the API key from a secret manager and register the
# custom sandbox image first; see agentbay.md for lifecycle precautions.
#AGENTBAY_API_KEY=
#AGENTBAY_REGION_ID=
#AGENTBAY_IMAGE_ID=
#AGENTBAY_DEPLOYMENT_ID=
#AGENTBAY_QUOTA_CONFIG_VERSION=1
#AGENTBAY_API_PORT=30150
#AGENTBAY_CDP_PORT=30151
#AGENTBAY_VNC_PORT=30152

# Browser engine configuration
# Options: playwright, browser_use (default)
# - playwright:   uses Playwright directly via CDP (stable, well-tested)
# - browser_use:  uses the browser_use library's BrowserSession via CDP
#                 (richer DOM state extraction via AI-friendly selector map)
#BROWSER_ENGINE=browser_use

# Search engine configuration
# Options: baidu, baidu_web, google, bing, bing_web, tavily, serper, custom
# baidu:    uses the Baidu Qianfan AI Search API (requires BAIDU_SEARCH_API_KEY)
# baidu_web: scrapes Baidu search results with browser impersonation (no API key needed)
# bing:     uses the official Bing Web Search API (requires BING_SEARCH_API_KEY)
# bing_web: scrapes Bing search results directly (no API key needed)
# tavily:   uses the Tavily Search API (requires TAVILY_API_KEY)
# serper:   uses the Serper.dev Google Search API (requires SERPER_API_KEY)
# custom:   calls any third-party search REST API via SEARCH_API_URL + SEARCH_API_KEY
SEARCH_PROVIDER=bing_web

# Baidu search configuration, only used when SEARCH_PROVIDER=baidu
# Get your API key from https://console.bce.baidu.com/qianfan/ais/console/onlineService
#BAIDU_SEARCH_API_KEY=

# Bing search configuration, only used when SEARCH_PROVIDER=bing
# Get your API key from https://www.microsoft.com/en-us/bing/apis/bing-web-search-api
#BING_SEARCH_API_KEY=

# Google search configuration, only used when SEARCH_PROVIDER=google
#GOOGLE_SEARCH_API_KEY=
#GOOGLE_SEARCH_ENGINE_ID=

# Tavily search configuration, only used when SEARCH_PROVIDER=tavily
# Get your API key from https://tavily.com
#TAVILY_API_KEY=

# Serper.dev search configuration, only used when SEARCH_PROVIDER=serper
# Returns reliable Google results. Get your API key from https://serper.dev
#SERPER_API_KEY=

# Custom search API configuration, only used when SEARCH_PROVIDER=custom
# Allows integration with any third-party search REST API.
#
# Minimal setup (POST + Bearer token, e.g. a custom internal API):
#   SEARCH_API_URL=https://your-search-api.example.com/search
#   SEARCH_API_KEY=your-api-key
#
# Serper.dev via custom provider:
#   SEARCH_API_URL=https://google.serper.dev/search
#   SEARCH_API_KEY=your-serper-key
#   SEARCH_API_KEY_HEADER=X-API-KEY
#   SEARCH_API_KEY_HEADER_PREFIX=
#   SEARCH_RESULT_FIELD=organic
#
# SerpAPI via custom provider:
#   SEARCH_API_URL=https://serpapi.com/search
#   SEARCH_API_KEY=your-serpapi-key
#   SEARCH_API_KEY_PARAM=api_key
#   SEARCH_API_METHOD=GET
#   SEARCH_RESULT_FIELD=organic_results
#
# Brave Search API via custom provider:
#   SEARCH_API_URL=https://api.search.brave.com/res/v1/web/search
#   SEARCH_API_KEY=your-brave-key
#   SEARCH_API_KEY_HEADER=X-Subscription-Token
#   SEARCH_API_KEY_HEADER_PREFIX=
#   SEARCH_API_METHOD=GET
#   SEARCH_RESULT_FIELD=web.results
#   SEARCH_SNIPPET_FIELD=description
#
#SEARCH_API_URL=
#SEARCH_API_KEY=
#SEARCH_API_KEY_HEADER=Authorization
#SEARCH_API_KEY_HEADER_PREFIX=Bearer
#SEARCH_API_KEY_PARAM=
#SEARCH_API_METHOD=POST
#SEARCH_QUERY_FIELD=q
#SEARCH_RESULT_FIELD=results
#SEARCH_TITLE_FIELD=title
#SEARCH_LINK_FIELD=link
#SEARCH_SNIPPET_FIELD=snippet

# Google Analytics configuration
# Set your Google Analytics Measurement ID (e.g. G-XXXXXXXXXX)
#GOOGLE_ANALYTICS_ID=

# Auth configuration
# Options: password, none, local, sub2api
AUTH_PROVIDER=password
REGISTRATION_ENABLED=false
# Exact comma-separated origins; never use '*'
#CORS_ALLOWED_ORIGINS=https://manus.example.com
# Redis-authoritative authentication limits
#AUTH_LOGIN_ATTEMPTS_PER_WINDOW=10
#AUTH_LOGIN_IP_ATTEMPTS_PER_WINDOW=30
#AUTH_LOGIN_WINDOW_SECONDS=300
#AUTH_REGISTER_ATTEMPTS_PER_HOUR=5
#AUTH_PASSWORD_RESET_ATTEMPTS_PER_HOUR=5
#AUTH_REFRESH_ATTEMPTS_PER_MINUTE=60

# Password auth configuration, only used when AUTH_PROVIDER=password
# New hashes use a fresh random per-user salt. PASSWORD_SALT is legacy-only.
#PASSWORD_SALT=
PASSWORD_HASH_ROUNDS=600000
#PASSWORD_LEGACY_HASH_ROUNDS=10

# Local auth configuration, only used when AUTH_PROVIDER=local
#LOCAL_AUTH_EMAIL=admin@example.com
#LOCAL_AUTH_PASSWORD=

# Sub2API auth configuration, only used when AUTH_PROVIDER=sub2api
#SUB2API_BASE_URL=
#SUB2API_LOGIN_URL=
#SUB2API_AUTH_ME_PATH=/api/v1/auth/me
#SUB2API_AUTH_REFRESH_PATH=/api/v1/auth/refresh
#SUB2API_REFRESH_TOKEN_MAX_AGE_DAYS=90

# JWT configuration
# Paste the unique output of `openssl rand -hex 32`
JWT_SECRET_KEY=
# Independent production BYOK keyring; generate every JSON-array entry
# separately with `openssl rand -hex 32`.
#MODEL_CREDENTIAL_ENCRYPTION_KEYS=[]
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7

# Email configuration
# Only used when AUTH_PROVIDER=password
#EMAIL_HOST=smtp.gmail.com
#EMAIL_PORT=587
#EMAIL_USERNAME=your-email@gmail.com
#EMAIL_PASSWORD=
#EMAIL_FROM=your-email@gmail.com

# Claw (OpenClaw) configuration
# Enable or disable Claw feature (hides sidebar entry when false)
#CLAW_ENABLED=false
# Docker image used for Claw containers
#CLAW_IMAGE=simpleyyt/manus-claw
# Prefix for Claw container names
#CLAW_NAME_PREFIX=manus-claw
# Time-to-live for Claw containers; persistent by default, positive only for temporary use
#CLAW_TTL_SECONDS=0
# Docker network bridge name for Claw containers
CLAW_NETWORK=manus-network
# Max seconds to wait for Claw container to become ready
#CLAW_READY_TIMEOUT=300
# Fixed Claw address (single-user AUTH_PROVIDER=none/local development only)
#CLAW_ADDRESS=
# Internal fixed-runtime bootstrap capability; never expose through user APIs
#CLAW_API_KEY=
# Backend API URL used by Claw containers for callbacks
#MANUS_API_BASE_URL=http://backend:8000
# Independent Claw runtime-key HMAC keyring; generate every entry separately.
#CLAW_API_KEY_HMAC_KEYS=
# Claw proxy/WebSocket abuse limits
#CLAW_PROXY_REQUESTS_PER_MINUTE=30
#CLAW_PROXY_MAX_CONCURRENT_REQUESTS=2
#CLAW_CHAT_TURN_LEASE_SECONDS=300
#CLAW_CHAT_MAX_MESSAGE_BYTES=65536
#CLAW_CHAT_MAX_ATTACHMENTS=10
#CLAW_CHAT_MAX_ATTACHMENT_BYTES=26214400
#CLAW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES=52428800
#CLAW_UPLOAD_MAX_BYTES=26214400
#CLAW_HISTORY_MAX_MESSAGES=128

# Upload and per-user GridFS limits
#MULTIPART_UPLOAD_MAX_BODY_BYTES=27262976
#FILE_UPLOAD_MAX_BYTES=26214400
#FILE_STORAGE_MAX_BYTES_PER_USER=1073741824
#FILE_STORAGE_MAX_FILES_PER_USER=1000

# Extra headers for LLM API requests (JSON format)
#EXTRA_HEADERS={"X-Custom-Header": "value"}

# Task backend configuration
# local: run agent tasks in-process (default)
# celery: run agent tasks on distributed Celery workers
#         (requires a worker container, see docs/configuration.md)
#TASK_BACKEND=local
# Actual backend Python process/replica count; values >1 require Celery
#BACKEND_REPLICA_COUNT=1
# Optional custom Celery broker URL (defaults to the Redis settings above)
#CELERY_BROKER_URL=

# MCP configuration
#MCP_CONFIG_PATH=/etc/mcp.json

# Log configuration
LOG_LEVEL=INFO
```
<!-- /.env.example -->

> **Tip**: `env_file` and `environment` can be used together — values in `environment` override those from `env_file`. See [Configuration](configuration.md) for a full list of available options.

### Secure First Start

Before exposing the service, verify the following:

1. `DEPLOYMENT_ENVIRONMENT=production`, `JWT_SECRET_KEY` contains a unique `openssl rand -hex 32` value, `REGISTRATION_ENABLED=false` unless public signup is intentional, and `CORS_ALLOWED_ORIGINS` lists only the exact frontend origin.
2. Keep Redis durable: retain the bundled AOF `everysec`, `noeviction`, and named-volume settings, or configure equivalent persistence, backups, and high availability on an external service. A host crash can still lose roughly the last second of AOF writes; do not use ephemeral Redis for security state or treat it as the hard AgentBay billing ledger.
3. If users can enter custom model credentials, configure an independent `MODEL_CREDENTIAL_ENCRYPTION_KEYS` keyring. If Claw is enabled, configure an independent `CLAW_API_KEY_HMAC_KEYS` keyring as well.
4. Keep the default upload body, per-file, and per-user quotas unless you have deliberately sized MongoDB, Nginx, and storage for larger values.
5. Keep `TASK_BACKEND=local` and `BACKEND_REPLICA_COUNT=1` together. Before adding a second backend process or replica, switch to Celery and give every backend/worker the same Redis, MongoDB, JWT, model-keyring, and sandbox settings.
6. With AgentBay, treat every signed gateway link as a secret. Cleanup failures preserve the provider session ID for retry; do not delete the database record manually until the provider confirms deletion.
7. Dynamic Docker runtimes use separate control and egress bridges, isolating other runtimes and MongoDB/Redis, and TTL AutoRemove orphan bridges are collected. This is not a host/VPC egress firewall; add host firewall rules or a destination-filtering proxy to block Docker-host, RFC1918, link-local, or cloud-metadata destinations.

Refresh tokens are single-use and are replaced on every refresh; logout revokes their family. Sub2API redirects additionally require a one-time random `state` and verified fragment handoff. See [Configuration](configuration.md) for key rotation and legacy credential migration commands.

### Start Services

```bash
docker compose up -d
```

> Note: If you see `sandbox-1 exited with code 0`, this is normal — it ensures the sandbox image is successfully pulled locally.

Open your browser and visit <http://localhost:5173> to access Manus.
