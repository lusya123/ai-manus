# 🚀 快速上手

## 环境准备

本项目主要依赖Docker进行开发与部署，需要安装较新版本的Docker：

 * Docker 20.10+
 * Docker Compose

模型能力要求：

 * 支持 LangChain Chat Model（默认 `openai` 提供商）
 * 支持原生 Tool / Function Calling（计划与步骤结果通过结构化输出工具提交，不再依赖 Prompt 内嵌 JSON）

推荐使用具备稳定工具调用能力的 Deepseek 与 ChatGPT 模型。


## Docker 安装

### Windows & Mac 系统

按照官方要求安装 Docker Desktop ：https://docs.docker.com/desktop/

### Linux 系统

按照官方要求安装 Docker Engine：https://docs.docker.com/engine/

## 部署

使用 Docker Compose 进行部署。模型与功能配置通过 `.env` 文件（`env_file`）管理，部署安全和运行时隔离约束仍由 Compose 显式覆盖：

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

保存成 `docker-compose.yml` 文件。

### 创建 `.env` 配置文件

在 `docker-compose.yml` 同级目录下，基于 [`.env.example`](https://github.com/simpleyyt/ai-manus/blob/main/.env.example) 创建 `.env` 文件，至少填写 `API_KEY`，用 `openssl rand -hex 32` 生成独立的 `JWT_SECRET_KEY`，并根据模型服务调整 `API_BASE` 与 `MODEL_NAME`：

```ini
API_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
# 粘贴 `openssl rand -hex 32` 的唯一输出
JWT_SECRET_KEY=
```

完整的 `.env.example` 如下（搜索引擎、认证方式、沙箱、Claw 等更多配置项）：

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
# 生产 Redis 保存的是安全状态。项目自带 Compose 使用 AOF everysec 和命名
# 持久卷；外部 Redis 必须提供同等级的持久化、备份和高可用。

# Sandbox configuration
# Provider：docker（默认）或 agentbay（阿里云无影）
#SANDBOX_PROVIDER=docker
#SANDBOX_ADDRESS=
# 不设置时，本地直跑使用默认镜像；生产 Compose 会跟随 IMAGE_REGISTRY/IMAGE_TAG
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

# 仅 AgentBay。API key 从密钥管理器注入，并先注册自定义沙箱镜像；
# 生命周期注意事项见 agentbay.md。
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
# 逗号分隔的精确 origin；禁止使用 '*'
#CORS_ALLOWED_ORIGINS=https://manus.example.com
# Redis 权威认证限流
#AUTH_LOGIN_ATTEMPTS_PER_WINDOW=10
#AUTH_LOGIN_IP_ATTEMPTS_PER_WINDOW=30
#AUTH_LOGIN_WINDOW_SECONDS=300
#AUTH_REGISTER_ATTEMPTS_PER_HOUR=5
#AUTH_PASSWORD_RESET_ATTEMPTS_PER_HOUR=5
#AUTH_REFRESH_ATTEMPTS_PER_MINUTE=60

# Password auth configuration, only used when AUTH_PROVIDER=password
# 新哈希使用 per-user 随机 salt；PASSWORD_SALT 仅用于旧数据迁移
#PASSWORD_SALT=
PASSWORD_HASH_ROUNDS=600000
#PASSWORD_LEGACY_HASH_ROUNDS=10

# Local auth configuration, only used when AUTH_PROVIDER=local
#LOCAL_AUTH_EMAIL=admin@example.com
#LOCAL_AUTH_PASSWORD=

# Sub2API 认证配置，仅 AUTH_PROVIDER=sub2api 时使用
#SUB2API_BASE_URL=
#SUB2API_LOGIN_URL=
#SUB2API_AUTH_ME_PATH=/api/v1/auth/me
#SUB2API_AUTH_REFRESH_PATH=/api/v1/auth/refresh
#SUB2API_REFRESH_TOKEN_MAX_AGE_DAYS=90

# JWT configuration
# 粘贴 `openssl rand -hex 32` 的唯一输出
JWT_SECRET_KEY=
# 生产 BYOK 的独立 keyring；JSON 数组中的每一项都单独生成
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
# Claw 容器 TTL；默认常驻，正数仅用于临时部署
#CLAW_TTL_SECONDS=0
# Docker network bridge name for Claw containers
CLAW_NETWORK=manus-network
# Max seconds to wait for Claw container to become ready
#CLAW_READY_TIMEOUT=300
# 固定 Claw 地址（仅单用户 AUTH_PROVIDER=none/local 开发环境）
#CLAW_ADDRESS=
# 固定 runtime 内部启动 capability；禁止通过用户 API 暴露
#CLAW_API_KEY=
# Backend API URL used by Claw containers for callbacks
#MANUS_API_BASE_URL=http://backend:8000
# 独立 Claw runtime-key HMAC keyring；每一项都单独生成
#CLAW_API_KEY_HMAC_KEYS=
# Claw 代理/WebSocket 防滥用限制
#CLAW_PROXY_REQUESTS_PER_MINUTE=30
#CLAW_PROXY_MAX_CONCURRENT_REQUESTS=2
#CLAW_CHAT_TURN_LEASE_SECONDS=300
#CLAW_CHAT_MAX_MESSAGE_BYTES=65536
#CLAW_CHAT_MAX_ATTACHMENTS=10
#CLAW_CHAT_MAX_ATTACHMENT_BYTES=26214400
#CLAW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES=52428800
#CLAW_UPLOAD_MAX_BYTES=26214400
#CLAW_HISTORY_MAX_MESSAGES=128

# 上传与每用户 GridFS 配额
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
# 实际 backend Python 进程/副本数；大于 1 时必须使用 Celery
#BACKEND_REPLICA_COUNT=1
# Optional custom Celery broker URL (defaults to the Redis settings above)
#CELERY_BROKER_URL=

# MCP configuration
#MCP_CONFIG_PATH=/etc/mcp.json

# Log configuration
LOG_LEVEL=INFO
```
<!-- /.env.example -->

> **提示**：`env_file` 和 `environment` 可以同时使用，`environment` 中的值会覆盖 `env_file` 中的同名变量。完整的配置项说明请参阅[配置说明](configuration.md)。

### 安全首次启动

对外开放服务前，请确认：

1. 已设置 `DEPLOYMENT_ENVIRONMENT=production`，`JWT_SECRET_KEY` 已填入唯一的 `openssl rand -hex 32` 输出；除非确实开放公共注册，否则保持 `REGISTRATION_ENABLED=false`；`CORS_ALLOWED_ORIGINS` 只包含精确的前端 origin。
2. 保持 Redis 持久化：保留项目自带的 AOF `everysec`、`noeviction` 和命名持久卷设置；使用外部服务时配置同等级的持久化、备份和高可用。AOF `everysec` 在主机崩溃时仍可能丢失约一秒写入，禁止用临时 Redis 承载安全状态，也不要把它当作 AgentBay 计费硬账本。
3. 允许用户填写自定义模型凭证时，配置独立 `MODEL_CREDENTIAL_ENCRYPTION_KEYS` keyring；启用 Claw 时也配置独立 `CLAW_API_KEY_HMAC_KEYS` keyring。
4. 除非已经按更大容量规划 MongoDB、Nginx 和存储，否则保留默认的请求体、单文件和每用户配额。
5. `TASK_BACKEND=local` 必须与 `BACKEND_REPLICA_COUNT=1` 配套。增加第二个 backend 进程/副本前先切换到 Celery，并让所有 backend/worker 共享 Redis、MongoDB、JWT、模型 keyring 和沙箱配置。
6. 使用 AgentBay 时，把每个 signed gateway link 都当作 secret。清理失败会保留 provider session ID 供重试；provider 未确认删除前不要手工删数据库记录。
7. 动态 Docker 运行时采用独立控制网和出口网，能隔离其它运行时及 MongoDB/Redis，并会回收 TTL AutoRemove 遗留网桥；这不等于限制宿主机/VPC 出口。如需阻断 Docker host、RFC1918、link-local 或云 metadata，必须另配宿主机防火墙或目标过滤代理。

refresh token 单次使用且每次刷新都会替换，logout 会撤销其 family。Sub2API 重定向还要求一次性随机 `state` 和验证通过的 fragment handoff。key 轮换与旧凭证迁移命令见[配置说明](configuration.md)。

### 启动服务

```bash
docker compose up -d
```

> 注意：如果提示 `sandbox-1 exited with code 0`，这是正常的，这是为了让 sandbox 镜像成功拉取到本地。

打开浏览器访问 <http://localhost:5173> 即可访问 Manus。

## 本地开发快速验证

开发调试推荐使用热重载栈：

```bash
cp .env.example .env
# 开发时可设 AUTH_PROVIDER=none，API_BASE 指向 mockserver 或真实 LLM
./dev.sh up -d
```

访问 <http://localhost:5173>。调试模式下全局只启动一个共享沙盒（`SANDBOX_ADDRESS=sandbox`）。更多见仓库根目录 README「开发指南」。
