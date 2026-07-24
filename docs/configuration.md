# 📋 配置说明

## 配置项

### 模型提供商配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `API_KEY` | - | 是 | LLM 模型的 API 密钥 |
| `API_BASE` | `http://mockserver:8090/v1` | 否 | API 基础地址，用于指定模型服务的端点 |

### 模型配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `MODEL_PROVIDER` | `openai` | 否 | 模型提供商，决定底层使用哪个 LLM 集成（如 `openai`、`deepseek`、`anthropic`、`ollama`），仅在 `LLM_PROVIDER=langchain` 时生效 |
| `MODEL_NAME` | `deepseek-chat` | 是 | 要使用的模型名称 |
| `AVAILABLE_MODELS` | 内置列表 | 否 | 可由用户按会话选择的模型 JSON 数组；其中的 API Key 只保留在后端 |
| `TEMPERATURE` | `0.7` | 否 | 模型响应的随机性程度，范围 0-1 |
| `MAX_TOKENS` | `2000` | 否 | 模型响应的最大 token 数量 |
| `LLM_PROVIDER` | `langchain` | 否 | LLM 网关实现：`langchain`（默认，经 `init_chat_model` 支持多种提供商）或 `openai`（直接使用官方 `openai` Python SDK 调用 OpenAI / 兼容端点） |
| `EXTRA_HEADERS` | - | 否 | 为模型请求附加的自定义 HTTP 头，JSON 对象字符串（如 `{"X-Api-Key":"xxx"}`），部分网关鉴权时需要 |
| `MODEL_CREDENTIAL_ENCRYPTION_KEYS` | - | BYOK 生产部署必需 | JSON 数组形式的独立凭证加密 keyring；首项加密新记录，其余项仅用于读取/轮换旧记录，每项至少 32 字节且不得重复 |

按会话选择 `AVAILABLE_MODELS` 时，目录模型的服务端密钥不会写入会话数据。用户通过界面提供自定义模型（BYOK）时，必须同时提供 API Key、公开可路由的 HTTP(S) API Base、模型名和提供商；内网、回环、链路本地及云元数据地址会被拒绝。BYOK 密钥使用 `MODEL_CREDENTIAL_ENCRYPTION_KEYS` 认证加密，与 JWT 签名密钥分离。用 `openssl rand -hex 32` 为每个 key 生成唯一值，并确保 backend 与 Celery worker 使用相同 keyring；不要把公开示例值用于真实部署。

### 配置不同的模型 / 提供商

后端底层通过 **LangChain 的 [`init_chat_model`](https://python.langchain.com/api_reference/langchain/chat_models/langchain.chat_models.base.init_chat_model.html)** 调用大模型，因此**只需通过环境变量即可切换不同的模型提供商，无需改动任何代码**：`MODEL_PROVIDER` 决定使用哪个集成，`MODEL_NAME` 指定具体模型，`API_KEY` / `API_BASE` 提供凭证与端点，`EXTRA_HEADERS` 可附加自定义请求头。

以下提供商已内置（对应的 LangChain 集成包已预装在 `backend/pyproject.toml` 中）：

| `MODEL_PROVIDER` | 说明 | 集成包 |
|------------------|------|--------|
| `openai` | OpenAI 及**所有 OpenAI 兼容端点**（DeepSeek、Moonshot、通义千问、vLLM、OneAPI、本地网关等），通过 `API_BASE` 指定端点 | `langchain-openai` |
| `deepseek` | DeepSeek 原生集成 | `langchain-deepseek` |
| `anthropic` | Anthropic Claude | `langchain-anthropic` |
| `ollama` | 本地 Ollama 运行的开源模型 | `langchain-ollama` |

**配置示例：**

- **OpenAI**
  ```env
  MODEL_PROVIDER=openai
  MODEL_NAME=gpt-4o
  API_KEY=sk-...
  # API_BASE 可省略以使用官方默认端点
  ```

- **OpenAI 兼容端点**（DeepSeek 官方 API / OneAPI / vLLM 等，最常见的接入方式）
  ```env
  MODEL_PROVIDER=openai
  MODEL_NAME=deepseek-chat
  API_BASE=https://api.deepseek.com/v1
  API_KEY=sk-...
  ```

- **DeepSeek 原生集成**
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

- **Ollama（本地）**
  ```env
  MODEL_PROVIDER=ollama
  MODEL_NAME=llama3.1
  API_BASE=http://host.docker.internal:11434
  API_KEY=ollama   # Ollama 无需真实密钥，但 API_KEY 必须非空以通过校验
  ```

> **接入更多提供商**：`init_chat_model` 还支持 Google Gemini、AWS Bedrock、Azure OpenAI、Mistral 等更多提供商。只需在 `backend/pyproject.toml` 增加对应的 `langchain-xxx` 集成包（如 `langchain-google-genai`）并重新构建镜像（`./build.sh` 或 `./dev.sh build`），再将 `MODEL_PROVIDER` 设为对应值即可。完整的提供商列表与命名参见 [LangChain `init_chat_model` 文档](https://python.langchain.com/api_reference/langchain/chat_models/langchain.chat_models.base.init_chat_model.html)。

### 切换 LLM 网关实现（`LLM_PROVIDER`）

后端在领域层通过统一的 `LLM` 接口调用大模型，具体实现由 `LLM_PROVIDER` 选择：

| `LLM_PROVIDER` | 说明 | 适用场景 |
|---------------|------|----------|
| `langchain`（默认） | 经 LangChain `init_chat_model` 调用，配合 `MODEL_PROVIDER` 支持 OpenAI、DeepSeek、Anthropic、Ollama 等多种提供商 | 需要多提供商、依赖 LangChain 生态（JSON 修复、重试等）时 |
| `openai` | 直接使用官方 `openai` Python SDK 调用 OpenAI 及**所有 OpenAI 兼容端点**（通过 `API_BASE`），不经过 LangChain | 只用 OpenAI / 兼容端点、希望减少依赖、更贴近原生 SDK 行为时 |

- 两种实现均消费同一套配置（`MODEL_NAME`、`API_KEY`、`API_BASE`、`TEMPERATURE`、`MAX_TOKENS`、`EXTRA_HEADERS`）。
- 选择 `openai` 时，`MODEL_PROVIDER` 被忽略（该实现始终使用 OpenAI SDK）。

**配置示例（使用 OpenAI SDK 直连 DeepSeek 兼容端点）：**

```env
LLM_PROVIDER=openai
MODEL_NAME=deepseek-chat
API_BASE=https://api.deepseek.com/v1
API_KEY=sk-...
```

### BYOK 凭证迁移与 keyring 轮换

独立 keyring 的安全轮换顺序如下：

1. 保留当前 `JWT_SECRET_KEY`，将新随机 key 放在 `MODEL_CREDENTIAL_ENCRYPTION_KEYS` 首项；轮换既有独立 key 时，把旧 key 暂放后续项。
2. 使用同一配置重启 backend 与 worker。
3. 在 `backend/` 目录先运行 `uv run python scripts/rotate_model_credential_keys.py` 审计；确认数量后再加 `--apply`。
4. 再次 dry-run，确认待轮换数量为 `0`，然后才移除旧 key 或轮换 `JWT_SECRET_KEY`。

来自旧自定义分支的 `agents.api_key` 可能同时包含复制的系统 key 与真实用户 BYOK。迁移前把**所有历史部署 key 和模型目录 key**放入临时的 JSON 数组 `LEGACY_SYSTEM_API_KEYS`；兼容单 key 的 `LEGACY_SYSTEM_API_KEY` 只适用于确实只有一个历史值的部署。先运行 `uv run python scripts/migrate_agent_credentials.py` dry-run，核对分类数量后再运行 `--apply`。脚本不会打印凭证值；迁移完成后立即从环境和 secret manager 中移除这些历史明文 key。遗漏历史系统 key 会造成错误分类，因此数量不符合预期时不要执行写入。

### MongoDB 配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `MONGODB_URI` | `mongodb://mongodb:27017` | 否 | MongoDB 连接字符串 |
| `MONGODB_DATABASE` | `manus` | 否 | 数据库名称 |
| `MONGODB_USERNAME` | - | 否 | MongoDB 用户名 |
| `MONGODB_PASSWORD` | - | 否 | MongoDB 密码 |

> MongoDB 是会话、用户、Claw 历史、GridFS 文件和配额计数的持久化依赖；`.env.example` 中注释这些项仅表示使用容器默认值，并不表示 MongoDB 可选。

### Redis 配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `REDIS_HOST` | `redis` | 否 | Redis 服务器地址 |
| `REDIS_PORT` | `6379` | 否 | Redis 服务器端口 |
| `REDIS_DB` | `0` | 否 | Redis 数据库编号 |
| `REDIS_PASSWORD` | - | 否 | Redis 密码 |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | `5.0` | 否 | 建立 Redis 连接的超时秒数 |
| `REDIS_HEALTH_CHECK_INTERVAL` | `30` | 否 | 连接池健康检查间隔秒数 |
| `REDIS_MAX_CONNECTIONS` | `100` | 否 | Redis 连接池上限 |
| `REDIS_RETRY_ATTEMPTS` | `3` | 否 | 临时 Redis 操作失败时的最大重试次数 |

> Redis 保存的是安全状态，不是可随意丢弃的缓存。认证限流、已使用 refresh 标记、token family 撤销、分布式租约和任务元数据都依赖它。项目自带的生产 Compose 使用 `--appendonly yes --appendfsync everysec --maxmemory-policy noeviction` 启动 Redis，并挂载名为 `manus-redis-data` 的持久卷。外部生产 Redis 必须提供同等级的持久存储、`noeviction` 语义、备份和高可用。主机崩溃仍可能丢失约最后一秒的 AOF 写入，因此 Redis 不承担 AgentBay 计费硬账本；Redis 数据丢失也可能在相关 token 到期前遗忘 logout 和 refresh replay 状态。

### 沙箱配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `SANDBOX_PROVIDER` | `docker` | 否 | 沙箱提供商：本地 `docker` 或阿里云无影 `agentbay` |
| `SANDBOX_ADDRESS` | - | 否 | 沙箱服务器地址 |
| `SANDBOX_IMAGE` | `simpleyyt/manus-sandbox` | 否 | Docker 沙箱镜像名称 |
| `SANDBOX_NAME_PREFIX` | `sandbox` | 否 | 沙箱容器名称前缀 |
| `SANDBOX_TTL_MINUTES` | `30` | 否 | 沙箱生存时间（分钟） |
| `SANDBOX_NETWORK` | `manus-network` | 否 | 仅供 backend 与不受信任运行时通信的 Docker 网络；禁止使用数据网络 |
| `SANDBOX_MEMORY_LIMIT` | `2g` | 否 | 每个本地 Docker 沙箱的内存硬上限 |
| `SANDBOX_CPU_LIMIT` | `2.0` | 否 | 每个本地 Docker 沙箱可使用的 CPU 核数上限 |
| `SANDBOX_PIDS_LIMIT` | `512` | 否 | 每个本地 Docker 沙箱的进程数上限（最小 32） |
| `SANDBOX_CHROME_ARGS` | - | 否 | Chrome 浏览器启动参数 |
| `SANDBOX_HTTPS_PROXY` | - | 否 | HTTPS 代理设置 |
| `SANDBOX_HTTP_PROXY` | - | 否 | HTTP 代理设置 |
| `SANDBOX_NO_PROXY` | - | 否 | 不使用代理的地址列表 |

#### AgentBay 云沙箱配置

仅当 `SANDBOX_PROVIDER=agentbay` 时使用；完整接入流程见 [AgentBay 云沙箱](agentbay.md)。

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `AGENTBAY_API_KEY` | - | 是 | AgentBay API Key |
| `AGENTBAY_REGION_ID` | SDK 默认值 | 否 | AgentBay 地域 ID |
| `AGENTBAY_IMAGE_ID` | - | 是 | 由 `sandbox/` 构建并注册的自定义镜像 ID |
| `AGENTBAY_DEPLOYMENT_ID` | - | 是 | 所有副本共享且长期稳定的部署标识；仅以摘要形式进入 provider label 与 Mongo 成本账本 |
| `AGENTBAY_QUOTA_CONFIG_VERSION` | `1` | 否 | 成本账本配置版本；修改部署标识或配额时必须通过显式迁移升级 |
| `AGENTBAY_MAX_SESSIONS_TOTAL` | `20` | 否 | 全部署并发计费 session 上限；硬上限为 20 |
| `AGENTBAY_MAX_SESSIONS_PER_USER` | `3` | 否 | 单用户并发计费 session 上限，不得超过全局上限 |
| `AGENTBAY_QUOTA_COMMAND_TIMEOUT_SECONDS` | `2` | 否 | Mongo 成本账本命令超时；无法确认时拒绝创建 |
| `AGENTBAY_API_PORT` | `30150` | 否 | Sandbox API 网关端口，必须匹配镜像内 socat 配置 |
| `AGENTBAY_CDP_PORT` | `30151` | 否 | Chrome CDP 网关端口，必须匹配镜像内 socat 配置 |
| `AGENTBAY_VNC_PORT` | `30152` | 否 | VNC WebSocket 网关端口，必须匹配镜像内 socat 配置 |

AgentBay session 是计费资源。多数写入、journaled Mongo 单文档账本是跨副本配额与 provider cleanup handle 的唯一真源；Redis 不承担这份硬账本。创建顺序固定为 reservation → provider ID 入账 → Session 指针 → 网关链接，删除只有在独立 exact lookup 确认 provider 不存在后才释放账本。旧版已有资源必须先运行 `scripts/reconcile_agentbay_quota.py` 的只读审计，再显式 `--apply`。AgentBay 签名网关链接属于 bearer capability：SDK 控制台/文件日志会被禁用，应用日志只记录 session ID，禁止在日志、错误信息或监控标签中写入完整链接。

Docker 部署必须保持网络分层：`manus-network` 只连接 backend、frontend、sandbox 与 Claw；内部 `manus-data-network` 只连接 backend、MongoDB 与 Redis。backend 是唯一同时接入两张网络的应用服务。sandbox 和 Claw 都会执行用户/模型驱动的代码，绝不能加入 MongoDB/Redis 所在的数据网络；MongoDB/Redis 密码也不能替代这层隔离。

### 运行拓扑配置

这些 URL 会写入 Agent 的运行环境提示，避免生成的命令混淆用户访问地址、容器内部地址和沙箱访问地址。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `DEPLOYMENT_ENVIRONMENT` | `development` | 当前部署环境名称；真实对外部署必须显式设为 `production` 或 `staging`，以启用对应安全校验/默认行为 |
| `FRONTEND_PUBLIC_URL` / `BACKEND_PUBLIC_URL` | - | 用户浏览器可访问的公开地址 |
| `FRONTEND_INTERNAL_URL` / `BACKEND_INTERNAL_URL` | - | 私有网络内服务互访地址 |
| `FRONTEND_SANDBOX_URL` / `BACKEND_SANDBOX_URL` | - | 沙箱内命令和 Chrome 使用的地址 |
| `CLAW_PUBLIC_URL` / `CLAW_INTERNAL_URL` | - | Claw 的公开与内部地址 |
| `HOST_GATEWAY_URL` | - | 容器访问宿主机时使用的网关地址 |
| `CORS_ALLOWED_ORIGINS` | - | 逗号分隔的精确浏览器 origin；`FRONTEND_PUBLIC_URL` 也会自动加入允许列表 |

`CORS_ALLOWED_ORIGINS` 只接受 `http(s)://主机[:端口]`，拒绝 `*`、路径、查询参数、fragment 和 URL 内嵌凭证。development/local/test 在未配置 origin 时仅回退到 `http://localhost:5173` 与 `http://127.0.0.1:5173`；staging/production 必须显式配置。认证使用 Bearer header，CORS 不启用 credential cookies。

### Claw (OpenClaw) 配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `CLAW_ENABLED` | `false` | 否 | 是否启用 Claw 功能，设为 `true` 显示左侧面板入口 |
| `CLAW_IMAGE` | `simpleyyt/manus-claw` | 否 | Claw Docker 镜像名称 |
| `CLAW_NAME_PREFIX` | `manus-claw` | 否 | Claw 容器名称前缀 |
| `CLAW_TTL_SECONDS` | `0` | 否 | Claw 容器生存时间（秒）；默认常驻，正数仅用于临时部署 |
| `CLAW_NETWORK` | `manus-network` | 否 | Claw 运行时网络；禁止使用 `manus-data-network` |
| `CLAW_READY_TIMEOUT` | `300` | 否 | 等待 Claw 容器就绪的最大秒数（默认 5 分钟） |
| `CLAW_ADDRESS` | - | 否 | 固定 Claw 地址（仅限单用户 `AUTH_PROVIDER=none/local` 开发环境，设置后跳过 Docker 创建） |
| `CLAW_API_KEY` | - | 否 | 固定运行时的内部启动能力；不会由用户接口返回，且仅对所属的运行中记录有效 |
| `MANUS_API_BASE_URL` | `http://backend:8000` | 否 | 后端 API 地址，供 Claw 容器回调使用 |
| `CLAW_PUBLISH_HOST_PORTS` | `true` | 否 | 为动态 Claw 容器发布随机宿主机端口，支持 backend 直接在宿主机运行 |
| `CLAW_HOST_BIND_ADDRESS` | `127.0.0.1` | 否 | 动态端口绑定地址 |
| `CLAW_MAX_INSTANCES_TOTAL` | `20` | 否 | 全局 Claw 实例容量上限 |
| `CLAW_IDLE_TIMEOUT_SECONDS` | `0` | 否 | 空闲清理秒数；`0` 表示不清理运行中的用户工作站 |
| `CLAW_CLEANUP_INTERVAL_SECONDS` | `60` | 否 | 清理任务检查间隔 |
| `CLAW_DESTROY_ON_DELETE` | `true` | 否 | 固定外部运行时可选择不销毁；动态创建的所属容器在删除记录前始终会被销毁 |
| `CLAW_MEMORY_LIMIT` / `CLAW_NANO_CPUS` / `CLAW_PIDS_LIMIT` | `1g` / `1000000000` / `256` | 否 | 每个 Claw 容器的资源限制 |
| `CLAW_PROXY_MAX_INPUT_BYTES` | `131072` | 否 | 单次模型代理请求中提示词和工具定义的最大序列化字节数 |
| `CLAW_PROXY_REQUESTS_PER_MINUTE` | `30` | 否 | 每个 Claw 的分布式模型代理分钟请求上限 |
| `CLAW_PROXY_MAX_CONCURRENT_REQUESTS` | `2` | 否 | 每个 Claw 的分布式模型代理并发上限 |
| `CLAW_PROXY_REQUEST_LEASE_SECONDS` | `300` | 否 | 模型代理并发槽的在途安全租约秒数 |
| `CLAW_CHAT_TURN_LEASE_SECONDS` | `300` | 否 | 单轮 Claw 对话的分布式租约；流式响应期间自动续租 |
| `CLAW_CHAT_MAX_MESSAGE_BYTES` | `65536` | 否 | WebSocket 单条聊天消息的 UTF-8 字节上限 |
| `CLAW_CHAT_MAX_ATTACHMENTS` | `10` | 否 | 单轮 Claw 对话附件数量上限 |
| `CLAW_CHAT_MAX_ATTACHMENT_BYTES` | `26214400` | 否 | 单个 Claw 对话附件大小上限 |
| `CLAW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES` | `52428800` | 否 | 单轮 Claw 对话附件总大小上限 |
| `CLAW_UPLOAD_MAX_BYTES` | `26214400` | 否 | Claw 运行时能力上传到文件存储的单文件大小上限 |
| `CLAW_HISTORY_MAX_MESSAGES` | `128` | 否 | MongoDB 每次原子追加后保留的最近 Claw 历史条数（硬上限 128，避免超过 MongoDB 文档上限） |
| `CLAW_API_KEY_HMAC_KEYS` | 未设置 | 建议 | 逗号分隔的 `current,previous...` 运行时密钥摘要 secret（每项至少 32 字节）；命中旧 key 后会懒重算。首次从 JWT 回退迁出时，应把旧 `JWT_SECRET_KEY` 暂放 previous 一轮。未设置时轮换 JWT secret 必须重建活跃 Claw。 |
| `MULTIPART_UPLOAD_MAX_BODY_BYTES` | `27262976` | 否 | `/files` 与 `/claw/upload` 在 multipart 解析前执行的 HTTP 请求体总上限；需与反向代理限制保持一致 |

为 `CLAW_API_KEY_HMAC_KEYS` 的每个条目单独运行 `openssl rand -hex 32`。轮换时把新 key 放首位并暂留旧 key；旧 key 验证成功后记录会懒更新。Claw WebSocket 在握手、每轮消息和 token 到期时重新校验认证，并对单轮消息、附件数量、单附件/总附件字节数及跨副本并发执行限制；不要只依赖前端校验。`CLAW_HISTORY_MAX_MESSAGES` 即使配置更大也硬限制为 128。

### 文件上传与存储配额

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `MULTIPART_UPLOAD_MAX_BODY_BYTES` | `27262976` | 否 | FastAPI multipart 解析前的总 HTTP body 上限，覆盖 `/files` 与 `/claw/upload`；反向代理限制必须不小于该值 |
| `FILE_UPLOAD_MAX_BYTES` | `26214400` | 否 | GridFS 单文件上限（25 MiB） |
| `FILE_STORAGE_MAX_BYTES_PER_USER` | `1073741824` | 否 | 每用户 GridFS 累计字节上限（1 GiB） |
| `FILE_STORAGE_MAX_FILES_PER_USER` | `1000` | 否 | 每用户 GridFS 文件数量上限 |

上传前会先验证可读取的实际流大小并在 MongoDB 中原子预留用户配额；失败或删除会释放配额。Claw 上传同时受 `CLAW_UPLOAD_MAX_BYTES`、单文件上限和用户总配额约束。默认前端 Nginx 使用 `client_max_body_size 26m`，修改 backend body 上限时需同步修改边缘代理，避免代理与应用行为不一致。

### 搜索引擎配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `SEARCH_PROVIDER` | `bing_web` | 否 | 搜索引擎提供商（`baidu`、`baidu_web`、`google`、`bing`、`bing_web`、`tavily`、`serper` 或 `custom`） |

#### 百度搜索配置

仅当 `SEARCH_PROVIDER=baidu` 时使用（通过百度千帆 AI 搜索 API）：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `BAIDU_SEARCH_API_KEY` | - | 是 | 百度千帆 AI 搜索 API 密钥，从[百度千帆控制台](https://console.bce.baidu.com/qianfan/ais/console/onlineService)获取 |

> 若不想申请 API 密钥，可将 `SEARCH_PROVIDER` 设为 `baidu_web`，直接通过网页抓取百度搜索结果，无需任何密钥。

#### Bing 搜索配置

仅当 `SEARCH_PROVIDER=bing` 时使用（通过官方 API 搜索）：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `BING_SEARCH_API_KEY` | - | 是 | Bing Web Search API 密钥，从 [Azure](https://www.microsoft.com/en-us/bing/apis/bing-web-search-api) 获取 |

> 若不想申请 API 密钥，可将 `SEARCH_PROVIDER` 设为 `bing_web`，直接通过网页抓取 Bing 搜索结果，无需任何密钥。

#### Google 搜索配置

仅当 `SEARCH_PROVIDER=google` 时使用：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `GOOGLE_SEARCH_API_KEY` | - | 是 | Google 搜索 API 密钥 |
| `GOOGLE_SEARCH_ENGINE_ID` | - | 是 | Google 自定义搜索引擎 ID |

#### Tavily 搜索配置

仅当 `SEARCH_PROVIDER=tavily` 时使用：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `TAVILY_API_KEY` | - | 是 | Tavily 搜索 API 密钥，从 [tavily.com](https://tavily.com) 获取 |

#### Serper.dev 搜索配置

仅当 `SEARCH_PROVIDER=serper` 时使用。Serper.dev 返回可靠的 Google 搜索结果，推荐作为默认搜索提供商：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `SERPER_API_KEY` | - | 是 | Serper.dev API 密钥，从 [serper.dev](https://serper.dev) 获取（提供免费额度） |

#### 自定义搜索 API 配置

仅当 `SEARCH_PROVIDER=custom` 时使用。可对接任意第三方搜索 REST API，只需配置接口地址、密钥和字段映射即可：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `SEARCH_API_URL` | - | 是 | 搜索接口的完整 URL |
| `SEARCH_API_KEY` | - | 否 | 接口 API 密钥 |
| `SEARCH_API_KEY_HEADER` | `Authorization` | 否 | 传递密钥的 HTTP Header 名称（如 `X-API-KEY`） |
| `SEARCH_API_KEY_HEADER_PREFIX` | `Bearer ` | 否 | Header 值的前缀（含空格时请保留，如 `Bearer `；若 Header 直接是 key 则设为空） |
| `SEARCH_API_KEY_PARAM` | - | 否 | 将密钥作为 URL 查询参数传递时的参数名（设置后优先于 Header 方式） |
| `SEARCH_API_METHOD` | `POST` | 否 | HTTP 请求方式（`POST` 或 `GET`） |
| `SEARCH_QUERY_FIELD` | `q` | 否 | 请求体 / 查询参数中搜索词的字段名 |
| `SEARCH_RESULT_FIELD` | `results` | 否 | 响应 JSON 中结果数组的字段路径（支持点分隔的嵌套路径，如 `web.results`） |
| `SEARCH_TITLE_FIELD` | `title` | 否 | 每条结果中标题的字段名 |
| `SEARCH_LINK_FIELD` | `link` | 否 | 每条结果中 URL 的字段名 |
| `SEARCH_SNIPPET_FIELD` | `snippet` | 否 | 每条结果中摘要的字段名 |

**典型对接示例：**

- **Serper.dev（POST）**
  ```env
  SEARCH_PROVIDER=custom
  SEARCH_API_URL=https://google.serper.dev/search
  SEARCH_API_KEY=your-serper-key
  SEARCH_API_KEY_HEADER=X-API-KEY
  SEARCH_API_KEY_HEADER_PREFIX=
  SEARCH_RESULT_FIELD=organic
  ```

- **SerpAPI（GET）**
  ```env
  SEARCH_PROVIDER=custom
  SEARCH_API_URL=https://serpapi.com/search
  SEARCH_API_KEY=your-serpapi-key
  SEARCH_API_KEY_PARAM=api_key
  SEARCH_API_METHOD=GET
  SEARCH_RESULT_FIELD=organic_results
  ```

- **Brave Search API（GET）**
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

### 认证配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `AUTH_PROVIDER` | `password` | 否 | 认证提供商（`password`、`none`、`local` 或 `sub2api`） |
| `REGISTRATION_ENABLED` | `false` | 否 | 是否开放公共注册；面向互联网部署应保持关闭，除非明确需要开放注册 |
| `SHOW_GITHUB_BUTTON` | `true` | 否 | 是否在前端显示 GitHub 按钮 |
| `GITHUB_REPOSITORY_URL` | `https://github.com/simpleyyt/ai-manus` | 否 | 前端 GitHub 按钮跳转地址 |

登录、注册、密码重置和 refresh 均使用 Redis 权威限流。Redis 不可用时认证请求会暂时失败，而不是绕过限流、refresh 单次使用检查或登出撤销。后端默认以直接 TCP peer 作为 IP key，不信任未经配置的 `X-Forwarded-For`。

| 配置项 | 默认值 | 作用 |
|--------|--------|------|
| `AUTH_LOGIN_ATTEMPTS_PER_WINDOW` | `10` | 每账号在登录窗口内的尝试次数 |
| `AUTH_LOGIN_IP_ATTEMPTS_PER_WINDOW` | `30` | 每直接来源 IP 在登录窗口内的尝试次数 |
| `AUTH_LOGIN_WINDOW_SECONDS` | `300` | 登录限流窗口秒数 |
| `AUTH_REGISTER_ATTEMPTS_PER_HOUR` | `5` | 每 IP 每小时注册尝试次数 |
| `AUTH_PASSWORD_RESET_ATTEMPTS_PER_HOUR` | `5` | 每 IP/账号每小时密码重置尝试次数 |
| `AUTH_REFRESH_ATTEMPTS_PER_MINUTE` | `60` | 每 refresh token 每分钟刷新次数；IP 上限为该值的两倍 |

#### 密码认证配置

仅当 `AUTH_PROVIDER=password` 时使用：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `PASSWORD_HASH_ROUNDS` | `600000` | 否 | 新 PBKDF2-SHA256 密码哈希成本；后端最低强制 600,000 轮 |
| `PASSWORD_SALT` | - | 仅旧数据迁移 | 旧版本 deployment-wide salt；只用于验证旧哈希，不用于新密码 |
| `PASSWORD_LEGACY_HASH_ROUNDS` | `10` | 仅旧数据迁移 | 旧版本低成本哈希的历史轮数 |

新密码使用自描述的 `pbkdf2_sha256$rounds$salt$digest` 格式，每个用户生成新的随机 16 字节 salt。旧哈希在用户成功登录后自动升级，因此升级期间必须保留旧 `PASSWORD_SALT` 与历史轮数；不要为新部署设置共享 salt。修改/重置密码或停用账号会撤销该用户此前签发的 token。

#### 本地认证配置

仅当 `AUTH_PROVIDER=local` 时使用：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `LOCAL_AUTH_EMAIL` | `admin@example.com` | 否 | 本地管理员邮箱 |
| `LOCAL_AUTH_PASSWORD` | 不安全的仅开发内置值 | 是 | 本地管理员密码；必须使用 `openssl rand -base64 32` 显式生成，非 development/local/test 环境会拒绝空值和常见弱值 |

#### Sub2API 认证配置

仅当 `AUTH_PROVIDER=sub2api` 时使用。

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `SUB2API_BASE_URL` | - | 是 | Sub2API 服务根地址 |
| `SUB2API_LOGIN_URL` | - | 是 | 启动/登录页地址 |
| `SUB2API_CONSOLE_URL` / `SUB2API_MARKETPLACE_URL` / `SUB2API_USE_TOKEN_URL` | 从 base URL 推导 | 否 | 前端控制台、市场和用量入口 |
| `SUB2API_AUTH_ME_PATH` | `/api/v1/auth/me` | 否 | token 校验路径 |
| `SUB2API_AUTH_REFRESH_PATH` | `/api/v1/auth/refresh` | 否 | refresh 路径 |
| `SUB2API_TIMEOUT_SECONDS` | `10.0` | 否 | 外部认证请求超时 |
| `SUB2API_REFRESH_TOKEN_MAX_AGE_DAYS` | `90` | 否 | 不透明外部 refresh token 的撤销记录保留期；不得短于提供商最大寿命 |

浏览器发起登录时会生成 32 字节随机一次性 `state` nonce，并同时写入登录请求和 `redirect_uri`。回调只从 URL fragment 读取凭证，必须匹配并消费该 `state`，随后调用 `/auth/me` 验证成功才原子写入认证和模型状态；query 中的凭证会被丢弃，fragment 会立即清理。外部 refresh token 也是单次使用，并绑定可由 logout 整体撤销的 family；Sub2API logout 请求必须同时提交 refresh token 才能完整撤销登录。

### JWT 配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `JWT_SECRET_KEY` | 无安全默认值 | 是 | JWT 签名与文件/VNC/preview 能力链接的根密钥；启用认证或处于 staging/production 时必须为非默认且至少 32 字节，并在 backend/worker 间保持一致。仅显式 `AUTH_PROVIDER=none` 的一次性本地 development 可使用代码默认值 |
| `JWT_ALGORITHM` | `HS256` | 否 | JWT 签名算法 |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | 否 | 访问令牌过期时间（分钟） |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | 否 | 刷新令牌过期时间（天） |

本地 JWT access/refresh pair 共用一个可撤销 family。每个 refresh token 只能成功使用一次，成功后返回新的 access 与 refresh token；并发重放会被拒绝。logout 撤销整个 family，密码修改/重置或账号停用会撤销该用户此前签发的 token。客户端必须保存 refresh 响应中的新 refresh token，并在 logout 时一并提交当前 refresh token。

### 邮箱配置

仅当 `AUTH_PROVIDER=password` 时使用：

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `EMAIL_HOST` | - | 否 | SMTP 服务器地址 |
| `EMAIL_PORT` | `587` | 否 | SMTP 服务器端口 |
| `EMAIL_USERNAME` | - | 否 | 邮箱用户名 |
| `EMAIL_PASSWORD` | - | 否 | 邮箱密码 |
| `EMAIL_FROM` | - | 否 | 发件人邮箱地址 |

### 任务后端配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `TASK_BACKEND` | `local` | 否 | Agent 任务执行后端：`local`（在 backend 进程内执行）或 `celery`（投递到分布式 Celery worker 执行） |
| `BACKEND_REPLICA_COUNT` | `1` | 否 | 实际 backend Python 进程/副本数量；大于 1 时必须使用 `TASK_BACKEND=celery`，否则启动失败 |
| `CELERY_BROKER_URL` | - | 否 | 自定义 Celery broker 地址，默认复用上面的 Redis 配置 |

#### 使用 Celery 任务后端

`TASK_BACKEND=celery` 时，agent 任务不再运行在 backend 进程内，而是投递到独立的 Celery worker 容器执行，backend 可以水平扩容多副本。事件仍通过 Redis Stream 流式返回，前端行为不变。

worker 容器复用 backend 镜像，通过 `start_worker.sh` 脚本启动，在 compose 中额外添加一个 worker 服务即可：

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

说明：

- worker 需要与 backend 使用**相同的 `.env` 配置**（模型、MongoDB、Redis、沙箱等），因为它在执行任务时会直接访问这些服务。
- `BACKEND_REPLICA_COUNT` 必须填写真实的 API 副本/worker-process 数量。`TASK_BACKEND=local` 的 task registry 仅存在于单个 Python 进程，不能用于多个容器、多个 Uvicorn/Gunicorn worker 或热备副本；配置为大于 `1` 会被启动校验拒绝。
- backend 与 worker 必须共享相同的 `JWT_SECRET_KEY`、`MODEL_CREDENTIAL_ENCRYPTION_KEYS`、AgentBay 配置以及 Redis/MongoDB。keyring 不一致会导致历史 BYOK 凭证无法解密。
- worker 需要挂载 `/var/run/docker.sock`，用于创建和连接沙箱容器；开发模式使用固定沙箱时（`SANDBOX_ADDRESS=sandbox`）可省略。
- 每个 agent 任务运行期间会独占一个 worker 进程，可通过环境变量 `CELERY_CONCURRENCY`（默认 `4`）控制可并行执行的 agent 会话数量，`CELERY_LOG_LEVEL`（默认 `INFO`）控制日志级别。
- 也可以不通过容器直接启动 worker：`cd backend && ./start_worker.sh`。

### MCP 配置

| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `MCP_CONFIG_PATH` | `/etc/mcp.json` | 否 | MCP 配置文件路径 |

### 日志配置
| 配置项 | 默认值 | 是否必需 | 说明 |
|--------|--------|----------|------|
| `LOG_LEVEL` | `INFO` | 否 | 日志级别 (`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`) |
