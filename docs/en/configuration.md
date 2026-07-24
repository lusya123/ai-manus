# 📋 Configuration Guide

## Configuration Items

### Model Provider Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `API_KEY` | - | Yes | API key for the LLM model |
| `API_BASE` | `http://mockserver:8090/v1` | No | Base API address for specifying model service endpoint |

### Model Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `MODEL_PROVIDER` | `openai` | No | Model provider that selects the underlying LLM integration (e.g. `openai`, `deepseek`, `anthropic`, `ollama`); only used when `LLM_PROVIDER=langchain` |
| `MODEL_NAME` | `deepseek-chat` | Yes | Name of the model to use |
| `AVAILABLE_MODELS` | built-in list | No | JSON array of models selectable per chat; API keys in the array remain backend-only |
| `TEMPERATURE` | `0.7` | No | Randomness level of model responses, range 0-1 |
| `MAX_TOKENS` | `2000` | No | Maximum number of tokens in model response |
| `LLM_PROVIDER` | `langchain` | No | LLM gateway implementation: `langchain` (default, many providers via `init_chat_model`) or `openai` (direct OpenAI Python SDK for OpenAI / compatible endpoints) |
| `EXTRA_HEADERS` | - | No | Extra HTTP headers for model requests, as a JSON object string (e.g. `{"X-Api-Key":"xxx"}`); required by some gateways |
| `MODEL_CREDENTIAL_ENCRYPTION_KEYS` | - | Required for production BYOK | Independent credential-encryption keyring as a JSON array. The first item encrypts new records; later items only decrypt/rotate old records. Every item must be distinct and at least 32 bytes |

For per-chat `AVAILABLE_MODELS` selections, server-side catalog keys are never copied into session data. A user-supplied custom model (BYOK) must provide all four values: API key, a publicly routable HTTP(S) API base, model name, and provider. Private, loopback, link-local, and cloud-metadata destinations are rejected. BYOK keys use authenticated encryption through `MODEL_CREDENTIAL_ENCRYPTION_KEYS`, independently of JWT signing. Generate every key with `openssl rand -hex 32`, and use the same keyring in the backend and Celery workers; never use a public example value in a real deployment.

### Configuring Different Models / Providers

The backend calls the LLM through **LangChain's [`init_chat_model`](https://python.langchain.com/api_reference/langchain/chat_models/langchain.chat_models.base.init_chat_model.html)**, so you can **switch model providers entirely via environment variables — no code changes required**: `MODEL_PROVIDER` selects the integration, `MODEL_NAME` picks the specific model, `API_KEY` / `API_BASE` supply the credentials and endpoint, and `EXTRA_HEADERS` can add custom request headers.

The following providers are built in (their LangChain integration packages are pre-installed in `backend/pyproject.toml`):

| `MODEL_PROVIDER` | Description | Integration package |
|------------------|-------------|----------------------|
| `openai` | OpenAI and **any OpenAI-compatible endpoint** (DeepSeek, Moonshot, Qwen, vLLM, OneAPI, local gateways, …), pointed at via `API_BASE` | `langchain-openai` |
| `deepseek` | Native DeepSeek integration | `langchain-deepseek` |
| `anthropic` | Anthropic Claude | `langchain-anthropic` |
| `ollama` | Open-source models served locally by Ollama | `langchain-ollama` |

**Examples:**

- **OpenAI**
  ```env
  MODEL_PROVIDER=openai
  MODEL_NAME=gpt-4o
  API_KEY=sk-...
  # API_BASE can be omitted to use the official default endpoint
  ```

- **OpenAI-compatible endpoint** (DeepSeek official API / OneAPI / vLLM, the most common setup)
  ```env
  MODEL_PROVIDER=openai
  MODEL_NAME=deepseek-chat
  API_BASE=https://api.deepseek.com/v1
  API_KEY=sk-...
  ```

- **DeepSeek native integration**
  ```env
  MODEL_PROVIDER=deepseek
  MODEL_NAME=deepseek-chat
  API_KEY=sk-...
  ```

- **Anthropic Claude**
  ```env
  MODEL_PROVIDER=anthropic
  MODEL_NAME=claude-3-5-sonnet-latest
  API_KEY=sk-ant-...
  ```

- **Ollama (local)**
  ```env
  MODEL_PROVIDER=ollama
  MODEL_NAME=llama3.1
  API_BASE=http://host.docker.internal:11434
  API_KEY=ollama   # Ollama needs no real key, but API_KEY must be non-empty to pass validation
  ```

> **Adding more providers**: `init_chat_model` also supports Google Gemini, AWS Bedrock, Azure OpenAI, Mistral and many more. Just add the matching `langchain-xxx` integration package (e.g. `langchain-google-genai`) to `backend/pyproject.toml`, rebuild the images (`./build.sh` or `./dev.sh build`), and set `MODEL_PROVIDER` accordingly. See the [LangChain `init_chat_model` docs](https://python.langchain.com/api_reference/langchain/chat_models/langchain.chat_models.base.init_chat_model.html) for the full list of providers and names.

### Switching the LLM Gateway Provider (`LLM_PROVIDER`)

The backend calls the LLM through a single domain `LLM` interface, whose concrete implementation is chosen by `LLM_PROVIDER`:

| `LLM_PROVIDER` | Description | When to use |
|---------------|-------------|-------------|
| `langchain` (default) | Calls via LangChain `init_chat_model`; combined with `MODEL_PROVIDER` it supports OpenAI, DeepSeek, Anthropic, Ollama and more | When you need multiple providers or the LangChain ecosystem (JSON repair, retries, …) |
| `openai` | Uses the official `openai` Python SDK directly to call OpenAI and **any OpenAI-compatible endpoint** (via `API_BASE`), without going through LangChain | When you only use OpenAI / compatible endpoints and want fewer dependencies / native SDK behavior |

- Both implementations consume the same settings (`MODEL_NAME`, `API_KEY`, `API_BASE`, `TEMPERATURE`, `MAX_TOKENS`, `EXTRA_HEADERS`).
- When `openai` is selected, `MODEL_PROVIDER` is ignored (this backend always uses the OpenAI SDK).

**Example (OpenAI SDK talking directly to a DeepSeek-compatible endpoint):**

```env
LLM_PROVIDER=openai
MODEL_NAME=deepseek-chat
API_BASE=https://api.deepseek.com/v1
API_KEY=sk-...
```

### BYOK Credential Migration and Keyring Rotation

Rotate the independent keyring in this order:

1. Keep the current `JWT_SECRET_KEY`. Put a new random key first in `MODEL_CREDENTIAL_ENCRYPTION_KEYS`; when rotating an existing independent key, retain the old key in a later slot.
2. Restart the backend and workers with the same configuration.
3. From `backend/`, run `uv run python scripts/rotate_model_credential_keys.py` as a dry run; after checking the count, rerun with `--apply`.
4. Dry-run once more and confirm that `0` records need rotation before removing an old key or rotating `JWT_SECRET_KEY`.

The old custom branch could store copied system keys and genuine user BYOK keys together in plaintext `agents.api_key` fields. Before migration, place **every historical deployment and model-catalog key** in the temporary JSON array `LEGACY_SYSTEM_API_KEYS`; the singular `LEGACY_SYSTEM_API_KEY` is safe only when the deployment truly had one historical value. Run `uv run python scripts/migrate_agent_credentials.py` first as a dry run, verify its classifications, and only then rerun with `--apply`. The script never prints credential values. Remove those historical plaintext keys from the environment and secret manager immediately afterward. A missing historical system key causes misclassification, so do not apply when the counts are unexpected.

### MongoDB Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `MONGODB_URI` | `mongodb://mongodb:27017` | No | MongoDB connection string |
| `MONGODB_DATABASE` | `manus` | No | Database name |
| `MONGODB_USERNAME` | - | No | MongoDB username |
| `MONGODB_PASSWORD` | - | No | MongoDB password |

> MongoDB is the persistence dependency for sessions, users, Claw history, GridFS files, and quota counters. Commented entries in `.env.example` mean “use the container defaults,” not that MongoDB is optional.

### Redis Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `REDIS_HOST` | `redis` | No | Redis server address |
| `REDIS_PORT` | `6379` | No | Redis server port |
| `REDIS_DB` | `0` | No | Redis database number |
| `REDIS_PASSWORD` | - | No | Redis password |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | `5.0` | No | Redis connection timeout in seconds |
| `REDIS_HEALTH_CHECK_INTERVAL` | `30` | No | Connection-pool health-check interval in seconds |
| `REDIS_MAX_CONNECTIONS` | `100` | No | Redis connection-pool limit |
| `REDIS_RETRY_ATTEMPTS` | `3` | No | Maximum retries for transient Redis operation failures |

> Redis is security state, not a disposable cache. Authentication rate limits, used-refresh markers, token-family revocation, distributed leases, and task metadata depend on it. The bundled production Compose starts Redis with `--appendonly yes --appendfsync everysec --maxmemory-policy noeviction` and mounts the named `manus-redis-data` volume. An external production Redis must provide equivalent durable storage, `noeviction` semantics, backups, and high availability. A host crash can still lose roughly the last second of AOF writes, so Redis is not the hard AgentBay billing ledger; data loss can also forget logout and refresh-replay state before affected tokens expire.

### Sandbox Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `SANDBOX_PROVIDER` | `docker` | No | Sandbox provider: local `docker` or Alibaba Cloud Wuying `agentbay` |
| `SANDBOX_ADDRESS` | - | No | Sandbox server address |
| `SANDBOX_IMAGE` | `simpleyyt/manus-sandbox` | No | Docker sandbox image name |
| `SANDBOX_NAME_PREFIX` | `sandbox` | No | Sandbox container name prefix |
| `SANDBOX_TTL_MINUTES` | `30` | No | Sandbox time-to-live in minutes |
| `SANDBOX_NETWORK` | `manus-network` | No | Runtime-only Docker network used by backend and untrusted runtimes; never use the data network |
| `SANDBOX_MEMORY_LIMIT` | `2g` | No | Hard memory limit for each local Docker sandbox |
| `SANDBOX_CPU_LIMIT` | `2.0` | No | CPU-core limit for each local Docker sandbox |
| `SANDBOX_PIDS_LIMIT` | `512` | No | Process-count limit for each local Docker sandbox (minimum 32) |
| `SANDBOX_CHROME_ARGS` | - | No | Chrome browser startup arguments |
| `SANDBOX_HTTPS_PROXY` | - | No | HTTPS proxy settings |
| `SANDBOX_HTTP_PROXY` | - | No | HTTP proxy settings |
| `SANDBOX_NO_PROXY` | - | No | List of addresses to exclude from proxy |

#### AgentBay Cloud Sandbox Configuration

Used only with `SANDBOX_PROVIDER=agentbay`; see [AgentBay Cloud Sandbox](agentbay.md) for the full setup.

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `AGENTBAY_API_KEY` | - | Yes | AgentBay API key |
| `AGENTBAY_REGION_ID` | SDK default | No | AgentBay region ID |
| `AGENTBAY_IMAGE_ID` | - | Yes | Custom image ID built from `sandbox/` and registered with AgentBay |
| `AGENTBAY_DEPLOYMENT_ID` | - | Yes | Stable deployment identity shared by every replica; only digests enter provider labels and the Mongo cost ledger |
| `AGENTBAY_QUOTA_CONFIG_VERSION` | `1` | No | Cost-ledger configuration version; deployment/cap changes require an explicit migration |
| `AGENTBAY_MAX_SESSIONS_TOTAL` | `20` | No | Deployment-wide concurrent billable-session cap; hard maximum 20 |
| `AGENTBAY_MAX_SESSIONS_PER_USER` | `3` | No | Per-user concurrent billable-session cap; cannot exceed the global cap |
| `AGENTBAY_QUOTA_COMMAND_TIMEOUT_SECONDS` | `2` | No | Mongo cost-ledger command timeout; uncertain state rejects provisioning |
| `AGENTBAY_API_PORT` | `30150` | No | Sandbox API gateway port; must match the image's socat config |
| `AGENTBAY_CDP_PORT` | `30151` | No | Chrome CDP gateway port; must match the image's socat config |
| `AGENTBAY_VNC_PORT` | `30152` | No | VNC WebSocket gateway port; must match the image's socat config |

AgentBay sessions are billable resources. A majority-read, journaled Mongo single-document ledger is the sole cross-replica quota and provider-cleanup authority; Redis is not this hard ledger. Creation is ordered reservation → ledger provider ID → Session pointer → gateway links, and deletion releases capacity only after an independent exact lookup confirms provider absence. Existing legacy resources must first pass the read-only `scripts/reconcile_agentbay_quota.py` audit and then an explicit `--apply`. Signed AgentBay gateway links are bearer capabilities: SDK console/file logging is disabled, application logs include only the session ID, and logs, errors, or monitoring labels must never include a complete link.

Docker deployments must preserve network separation: `manus-network` connects only backend, frontend, sandbox, and Claw; the internal `manus-data-network` connects only backend, MongoDB, and Redis. Backend is the sole application service attached to both. Sandbox and Claw execute user/model-driven code and must never join the MongoDB/Redis data network; datastore passwords are defense in depth, not a replacement for this boundary.

### Runtime Topology Configuration

These URLs are advertised to the agent so generated commands do not confuse public, private-network, and sandbox-visible addresses.

| Configuration | Default | Description |
|---------------|---------|-------------|
| `DEPLOYMENT_ENVIRONMENT` | `development` | Current deployment environment. Real external deployments must set `production` or `staging` explicitly so the matching safety validation/defaults apply |
| `FRONTEND_PUBLIC_URL` / `BACKEND_PUBLIC_URL` | - | URLs reachable by the user's browser |
| `FRONTEND_INTERNAL_URL` / `BACKEND_INTERNAL_URL` | - | Service URLs on the private network |
| `FRONTEND_SANDBOX_URL` / `BACKEND_SANDBOX_URL` | - | URLs used by commands and Chrome inside a sandbox |
| `CLAW_PUBLIC_URL` / `CLAW_INTERNAL_URL` | - | Public and private Claw URLs |
| `HOST_GATEWAY_URL` | - | Gateway URL used by containers to reach the host |
| `CORS_ALLOWED_ORIGINS` | - | Comma-separated exact browser origins; `FRONTEND_PUBLIC_URL` is also added automatically |

`CORS_ALLOWED_ORIGINS` accepts only `http(s)://host[:port]`. Wildcards, paths, query strings, fragments, and embedded URL credentials are rejected. With no explicit origin, development/local/test falls back only to `http://localhost:5173` and `http://127.0.0.1:5173`; staging and production must configure origins explicitly. Authentication uses Bearer headers and CORS credential cookies are disabled.

### Claw (OpenClaw) Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `CLAW_ENABLED` | `false` | No | Enable Claw feature; set to `true` to show the sidebar entry |
| `CLAW_IMAGE` | `simpleyyt/manus-claw` | No | Claw Docker image name |
| `CLAW_NAME_PREFIX` | `manus-claw` | No | Claw container name prefix |
| `CLAW_TTL_SECONDS` | `0` | No | Claw container lifetime in seconds; persistent by default, positive only for temporary deployments |
| `CLAW_NETWORK` | `manus-network` | No | Claw runtime network; never use `manus-data-network` |
| `CLAW_READY_TIMEOUT` | `300` | No | Max seconds to wait for Claw container to become ready (default 5 minutes) |
| `CLAW_ADDRESS` | - | No | Fixed Claw address (single-user `AUTH_PROVIDER=none/local` development only; skips Docker creation) |
| `CLAW_API_KEY` | - | No | Internal fixed-runtime bootstrap capability; never returned by the user API and valid only for its running owned record |
| `MANUS_API_BASE_URL` | `http://backend:8000` | No | Backend API URL used by Claw containers for callbacks |
| `CLAW_PUBLISH_HOST_PORTS` | `true` | No | Publish random host ports for dynamic Claw containers when backend runs directly on the host |
| `CLAW_HOST_BIND_ADDRESS` | `127.0.0.1` | No | Dynamic host-port bind address |
| `CLAW_MAX_INSTANCES_TOTAL` | `20` | No | Global Claw instance capacity |
| `CLAW_IDLE_TIMEOUT_SECONDS` | `0` | No | Idle cleanup timeout; `0` never cleans running user workstations |
| `CLAW_CLEANUP_INTERVAL_SECONDS` | `60` | No | Cleanup scan interval |
| `CLAW_DESTROY_ON_DELETE` | `true` | No | Fixed external runtimes may opt out; owned dynamic containers are always destroyed before their record is deleted |
| `CLAW_MEMORY_LIMIT` / `CLAW_NANO_CPUS` / `CLAW_PIDS_LIMIT` | `1g` / `1000000000` / `256` | No | Per-container resource limits |
| `CLAW_PROXY_MAX_INPUT_BYTES` | `131072` | No | Maximum serialized prompt/tool-schema bytes per model-proxy request |
| `CLAW_PROXY_REQUESTS_PER_MINUTE` | `30` | No | Distributed per-Claw model-proxy request limit |
| `CLAW_PROXY_MAX_CONCURRENT_REQUESTS` | `2` | No | Distributed per-Claw model-proxy concurrency limit |
| `CLAW_PROXY_REQUEST_LEASE_SECONDS` | `300` | No | Safety lease for an in-flight proxy concurrency slot |
| `CLAW_CHAT_TURN_LEASE_SECONDS` | `300` | No | Distributed lease for one Claw chat turn; renewed while streaming |
| `CLAW_CHAT_MAX_MESSAGE_BYTES` | `65536` | No | UTF-8 byte limit for one WebSocket chat message |
| `CLAW_CHAT_MAX_ATTACHMENTS` | `10` | No | Attachment count limit for one Claw chat turn |
| `CLAW_CHAT_MAX_ATTACHMENT_BYTES` | `26214400` | No | Per-attachment size limit for Claw chat |
| `CLAW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES` | `52428800` | No | Total attachment size limit for one Claw chat turn |
| `CLAW_UPLOAD_MAX_BYTES` | `26214400` | No | Per-file cap for runtime-capability uploads to file storage |
| `CLAW_HISTORY_MAX_MESSAGES` | `128` | No | Recent Claw history records retained by each atomic MongoDB append (hard-capped at 128 to stay below MongoDB's document limit) |
| `CLAW_API_KEY_HMAC_KEYS` | unset | Recommended | Comma-separated `current,previous...` 32-byte-or-longer secrets for runtime-key digests. Previous-key matches are lazily rehashed. When first leaving the JWT fallback, include the old `JWT_SECRET_KEY` as a previous entry for one rotation window. If unset, JWT rotation requires rebuilding active Claws. |
| `MULTIPART_UPLOAD_MAX_BODY_BYTES` | `27262976` | No | Total request-body limit enforced before multipart parsing for `/files` and `/claw/upload`; keep the reverse-proxy limit aligned |

Generate every `CLAW_API_KEY_HMAC_KEYS` entry separately with `openssl rand -hex 32`. During rotation, put the new key first and temporarily retain the old key; a successful old-key verification lazily rehashes the record. Claw WebSockets recheck authorization at handshake, for every turn, and at token expiry, and enforce per-message, attachment-count, per-attachment/total-byte, and cross-replica execution limits. Do not rely only on frontend validation. `CLAW_HISTORY_MAX_MESSAGES` is hard-capped at 128 even if configured higher.

### File Upload and Storage Quotas

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `MULTIPART_UPLOAD_MAX_BODY_BYTES` | `27262976` | No | Total HTTP body cap before FastAPI multipart parsing for `/files` and `/claw/upload`; the reverse-proxy limit must be at least this large |
| `FILE_UPLOAD_MAX_BYTES` | `26214400` | No | GridFS per-file cap (25 MiB) |
| `FILE_STORAGE_MAX_BYTES_PER_USER` | `1073741824` | No | Per-user cumulative GridFS byte cap (1 GiB) |
| `FILE_STORAGE_MAX_FILES_PER_USER` | `1000` | No | Per-user GridFS file-count cap |

Before upload, the backend verifies the readable stream size and atomically reserves the user's quota in MongoDB; failures and deletes release the reservation. Claw uploads are subject to `CLAW_UPLOAD_MAX_BYTES`, the per-file limit, and the user's aggregate quota. The bundled frontend Nginx uses `client_max_body_size 26m`; update the edge proxy together with the backend body cap so the boundaries remain consistent.

### Search Engine Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `SEARCH_PROVIDER` | `bing_web` | No | Search engine provider (`baidu`, `baidu_web`, `google`, `bing`, `bing_web`, `tavily`, `serper`, or `custom`) |

#### Baidu Search Configuration

Used only when `SEARCH_PROVIDER=baidu` (Baidu Qianfan AI Search API):

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `BAIDU_SEARCH_API_KEY` | - | Yes | Baidu Qianfan AI Search API key, get from [Baidu Qianfan Console](https://console.bce.baidu.com/qianfan/ais/console/onlineService) |

> If you don't want to apply for an API key, set `SEARCH_PROVIDER` to `baidu_web` to scrape Baidu search results directly without any key.

#### Bing Search Configuration

Used only when `SEARCH_PROVIDER=bing` (official API):

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `BING_SEARCH_API_KEY` | - | Yes | Bing Web Search API key, get from [Azure](https://www.microsoft.com/en-us/bing/apis/bing-web-search-api) |

> If you don't want to apply for an API key, set `SEARCH_PROVIDER` to `bing_web` to scrape Bing search results directly without any key.

#### Google Search Configuration

Used only when `SEARCH_PROVIDER=google`:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `GOOGLE_SEARCH_API_KEY` | - | Yes | Google Search API key |
| `GOOGLE_SEARCH_ENGINE_ID` | - | Yes | Google Custom Search Engine ID |

#### Tavily Search Configuration

Used only when `SEARCH_PROVIDER=tavily`:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `TAVILY_API_KEY` | - | Yes | Tavily Search API key, get from [tavily.com](https://tavily.com) |

#### Serper.dev Search Configuration

Used only when `SEARCH_PROVIDER=serper`. Serper.dev delivers reliable Google search results and is recommended as the default provider:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `SERPER_API_KEY` | - | Yes | Serper.dev API key, get from [serper.dev](https://serper.dev) (free tier available) |

#### Custom Search API Configuration

Used only when `SEARCH_PROVIDER=custom`. Integrate any third-party search REST API by configuring the endpoint, credentials, and field mapping:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `SEARCH_API_URL` | - | Yes | Full URL of the search endpoint |
| `SEARCH_API_KEY` | - | No | API key for the endpoint |
| `SEARCH_API_KEY_HEADER` | `Authorization` | No | HTTP header name used to send the key (e.g. `X-API-KEY`) |
| `SEARCH_API_KEY_HEADER_PREFIX` | `Bearer ` | No | Prefix placed before the key value in the header (include trailing space, e.g. `Bearer `; set empty if the header value is the key itself) |
| `SEARCH_API_KEY_PARAM` | - | No | URL query parameter name for the key (alternative to header auth) |
| `SEARCH_API_METHOD` | `POST` | No | HTTP method: `POST` or `GET` |
| `SEARCH_QUERY_FIELD` | `q` | No | Field name for the search query in the request body / params |
| `SEARCH_RESULT_FIELD` | `results` | No | Dot-separated path to the results array in the JSON response (e.g. `web.results`) |
| `SEARCH_TITLE_FIELD` | `title` | No | Field name for the result title |
| `SEARCH_LINK_FIELD` | `link` | No | Field name for the result URL |
| `SEARCH_SNIPPET_FIELD` | `snippet` | No | Field name for the result snippet / description |

**Common integration examples:**

- **Serper.dev (POST)**
  ```env
  SEARCH_PROVIDER=custom
  SEARCH_API_URL=https://google.serper.dev/search
  SEARCH_API_KEY=your-serper-key
  SEARCH_API_KEY_HEADER=X-API-KEY
  SEARCH_API_KEY_HEADER_PREFIX=
  SEARCH_RESULT_FIELD=organic
  ```

- **SerpAPI (GET)**
  ```env
  SEARCH_PROVIDER=custom
  SEARCH_API_URL=https://serpapi.com/search
  SEARCH_API_KEY=your-serpapi-key
  SEARCH_API_KEY_PARAM=api_key
  SEARCH_API_METHOD=GET
  SEARCH_RESULT_FIELD=organic_results
  ```

- **Brave Search API (GET)**
  ```env
  SEARCH_PROVIDER=custom
  SEARCH_API_URL=https://api.search.brave.com/res/v1/web/search
  SEARCH_API_KEY=your-brave-key
  SEARCH_API_KEY_HEADER=X-Subscription-Token
  SEARCH_API_KEY_HEADER_PREFIX=
  SEARCH_API_METHOD=GET
  SEARCH_RESULT_FIELD=web.results
  SEARCH_SNIPPET_FIELD=description
  ```

### Authentication Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `AUTH_PROVIDER` | `password` | No | Authentication provider (`password`, `none`, `local`, or `sub2api`) |
| `REGISTRATION_ENABLED` | `false` | No | Enable public registration; keep disabled on Internet-facing deployments unless account creation is intentionally public |
| `SHOW_GITHUB_BUTTON` | `true` | No | Whether to show the GitHub button in the top bar |
| `GITHUB_REPOSITORY_URL` | `https://github.com/simpleyyt/ai-manus` | No | GitHub button target URL |

Login, registration, password-reset, and refresh endpoints use Redis-authoritative rate limits. When Redis is unavailable, authentication temporarily fails instead of bypassing rate limits, refresh single-use checks, or logout revocation. The backend keys IP limits by the direct TCP peer and does not trust unconfigured `X-Forwarded-For` values.

| Configuration | Default | Purpose |
|---------------|---------|---------|
| `AUTH_LOGIN_ATTEMPTS_PER_WINDOW` | `10` | Per-account attempts in one login window |
| `AUTH_LOGIN_IP_ATTEMPTS_PER_WINDOW` | `30` | Per-direct-peer-IP attempts in one login window |
| `AUTH_LOGIN_WINDOW_SECONDS` | `300` | Login rate-limit window in seconds |
| `AUTH_REGISTER_ATTEMPTS_PER_HOUR` | `5` | Registration attempts per IP per hour |
| `AUTH_PASSWORD_RESET_ATTEMPTS_PER_HOUR` | `5` | Password-reset attempts per IP/account per hour |
| `AUTH_REFRESH_ATTEMPTS_PER_MINUTE` | `60` | Refreshes per refresh token per minute; the IP limit is twice this value |

#### Password Authentication Configuration

Used only when `AUTH_PROVIDER=password`:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `PASSWORD_HASH_ROUNDS` | `600000` | No | Work factor for new PBKDF2-SHA256 password hashes; the backend enforces at least 600,000 rounds |
| `PASSWORD_SALT` | - | Legacy migration only | Historical deployment-wide salt used only to verify old hashes, never new passwords |
| `PASSWORD_LEGACY_HASH_ROUNDS` | `10` | Legacy migration only | Historical work factor for the old low-cost hashes |

New passwords use the self-describing `pbkdf2_sha256$rounds$salt$digest` format with a fresh random 16-byte salt per user. A successful login upgrades a legacy hash, so retain the historical `PASSWORD_SALT` and rounds during migration; do not configure a shared salt for a new deployment. Password change/reset and account deactivation revoke all tokens issued earlier for that user.

#### Local Authentication Configuration

Used only when `AUTH_PROVIDER=local`:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `LOCAL_AUTH_EMAIL` | `admin@example.com` | No | Local admin email |
| `LOCAL_AUTH_PASSWORD` | insecure development-only built-in | Yes | Local admin password; explicitly generate it with `openssl rand -base64 32`. Blank and common weak values are rejected outside development/local/test |

#### Sub2API Authentication Configuration

Used only with `AUTH_PROVIDER=sub2api`.

| Configuration | Default | Required | Description |
|---------------|---------|----------|-------------|
| `SUB2API_BASE_URL` | - | Yes | Sub2API service root URL |
| `SUB2API_LOGIN_URL` | - | Yes | Launch/login page URL |
| `SUB2API_CONSOLE_URL` / `SUB2API_MARKETPLACE_URL` / `SUB2API_USE_TOKEN_URL` | Derived from base URL | No | Frontend console, marketplace, and usage links |
| `SUB2API_AUTH_ME_PATH` | `/api/v1/auth/me` | No | Token-validation path |
| `SUB2API_AUTH_REFRESH_PATH` | `/api/v1/auth/refresh` | No | Refresh path |
| `SUB2API_TIMEOUT_SECONDS` | `10.0` | No | External authentication request timeout |
| `SUB2API_REFRESH_TOKEN_MAX_AGE_DAYS` | `90` | No | Revocation retention for opaque external refresh tokens; it must not be shorter than the provider's maximum lifetime |

When the browser starts a login, it generates a random 32-byte one-time `state` nonce and places it in both the login request and `redirect_uri`. A callback accepts credentials only from the URL fragment, must match and consume that `state`, and atomically commits auth/model state only after `/auth/me` verifies the token. Query credentials are discarded and the fragment is scrubbed immediately. External refresh tokens are also single-use and belong to a family that logout revokes together; a Sub2API logout request must include the refresh token to fully revoke the login.

### JWT Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `JWT_SECRET_KEY` | no secure default | Yes | Root key for JWT signing and file/VNC/preview capability links. Authentication-enabled and staging/production deployments require a non-default value of at least 32 bytes, shared by backend/workers. Only disposable local development explicitly using `AUTH_PROVIDER=none` may use the code default |
| `JWT_ALGORITHM` | `HS256` | No | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | No | Access token expiration time in minutes |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | No | Refresh token expiration time in days |

Each local JWT access/refresh pair belongs to one revocable family. A refresh token can succeed only once and returns a new access and refresh token; concurrent replay is rejected. Logout revokes the whole family, while password change/reset and account deactivation revoke all earlier tokens for that user. Clients must store the replacement refresh token returned by refresh and include the current refresh token on logout.

### Email Configuration

Used only when `AUTH_PROVIDER=password`:

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `EMAIL_HOST` | - | No | SMTP server address |
| `EMAIL_PORT` | `587` | No | SMTP server port |
| `EMAIL_USERNAME` | - | No | Email username |
| `EMAIL_PASSWORD` | - | No | Email password |
| `EMAIL_FROM` | - | No | Sender email address |

### Task Backend Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `TASK_BACKEND` | `local` | No | Agent task execution backend: `local` (run inside the backend process) or `celery` (dispatch to distributed Celery workers) |
| `BACKEND_REPLICA_COUNT` | `1` | No | Actual number of backend Python processes/replicas. Values above 1 require `TASK_BACKEND=celery`, otherwise startup fails |
| `CELERY_BROKER_URL` | - | No | Custom Celery broker URL; defaults to the Redis configuration above |

#### Using the Celery Task Backend

With `TASK_BACKEND=celery`, agent tasks no longer run inside the backend process but are dispatched to dedicated Celery worker containers, allowing the backend to scale horizontally. Events still stream back through Redis Streams, so frontend behavior is unchanged.

Workers reuse the backend image and start via the `start_worker.sh` script; just add an extra worker service to your compose file:

```yaml
  worker:
    image: simpleyyt/manus-backend:latest
    command: ["./start_worker.sh"]
    depends_on:
      - mongodb
      - redis
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - manus-network
    env_file:
      - .env
    environment:
      - TASK_BACKEND=celery
```

Notes:

- Workers must share the **same `.env` configuration** as the backend (model, MongoDB, Redis, sandbox, etc.), since they access these services directly while executing tasks.
- Set `BACKEND_REPLICA_COUNT` to the real API replica/worker-process count. The `TASK_BACKEND=local` task registry exists in only one Python process and cannot support multiple containers, Uvicorn/Gunicorn workers, or standby replicas; startup validation rejects a declared value above `1`.
- Backends and workers must share the same `JWT_SECRET_KEY`, `MODEL_CREDENTIAL_ENCRYPTION_KEYS`, AgentBay settings, Redis, and MongoDB. A mismatched keyring makes historical BYOK credentials unreadable.
- Workers need `/var/run/docker.sock` mounted to create and connect to sandbox containers; it can be omitted in development mode with a fixed sandbox (`SANDBOX_ADDRESS=sandbox`).
- Each agent task occupies one worker process for its whole run; use the `CELERY_CONCURRENCY` env var (default `4`) to bound how many agent sessions execute in parallel, and `CELERY_LOG_LEVEL` (default `INFO`) to control the log level.
- Workers can also be started without a container: `cd backend && ./start_worker.sh`.

### MCP Configuration

| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `MCP_CONFIG_PATH` | `/etc/mcp.json` | No | MCP configuration file path |

### Log Configuration
| Configuration | Default Value | Required | Description |
|---------------|---------------|----------|-------------|
| `LOG_LEVEL` | `INFO` | No | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
