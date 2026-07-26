# AI Manus × Claw 后端服务

[English](README.md) | 中文

AI Manus × Claw 是一个基于 FastAPI 和 LangChain Chat Model 的智能对话代理系统。该后端采用领域驱动设计(DDD)架构，支持智能对话、文件操作、Shell命令执行、浏览器自动化，以及集成 [OpenClaw](https://github.com/anthropics/openclaw) AI 助手管理（Claw）等功能。

## 项目架构

项目采用领域驱动设计(DDD)架构，清晰地分离各层职责：

```
backend/
├── app/
│   ├── domain/          # 领域层：包含核心业务逻辑
│   │   ├── models/      # 领域模型定义
│   │   ├── services/    # 领域服务
│   │   ├── external/    # 外部服务接口
│   │   └── prompts/     # 提示词模板
│   ├── application/     # 应用层：编排业务流程
│   │   ├── services/    # 应用服务（agent、auth、file、token、email、claw）
│   │   └── schemas/     # 数据模式定义
│   ├── interfaces/      # 接口层：定义系统对外接口
│   │   ├── api/         # API 路由（会话、文件、认证、配置、Claw、OpenAI 代理）
│   │   └── schemas/     # 请求/响应与 SSE 事件模式
│   ├── infrastructure/  # 基础设施层：提供技术实现
│   ├── core/            # 核心配置（config.py）
│   └── main.py          # 应用入口
├── Dockerfile           # Docker配置文件
├── pyproject.toml       # 项目依赖与元数据
└── README.md            # 项目文档
```

## 核心功能

1. **会话管理**：创建和管理对话会话实例
2. **实时对话**：通过Server-Sent Events (SSE)实现实时对话
3. **工具调用**：支持多种工具调用，包括：
   - 浏览器自动化操作（使用Playwright）
   - Shell命令执行与查看
   - 文件读写操作
   - 网络搜索集成
4. **沙盒环境**：使用Docker容器提供隔离的执行环境
5. **VNC可视化**：通过WebSocket连接支持远程查看沙盒环境
6. **Claw（Manus × Claw）**：为每个用户管理 OpenClaw 容器生命周期，合并聊天历史（MongoDB + OpenClaw `.jsonl` 会话），WebSocket 实时通信，文件上传/解析，以及为 Claw 容器提供 OpenAI 兼容 LLM 代理

## 环境要求

- Python 3.12+
- Docker 20.10+
- MongoDB 4.4+
- Redis 6.0+

## 安装配置

1. **安装 uv**:
```bash
pip install uv
```

2. **安装依赖**:
```bash
uv sync
```

3. **环境变量配置**:
创建 `.env` 文件并设置以下环境变量（完整列表见 `app/core/config.py` 或根目录 [.env.example](https://github.com/simpleyyt/ai-manus/blob/main/.env.example)）:
```
# Model provider configuration
API_KEY=                                 # 注入模型供应商 API 密钥（必填）
API_BASE=https://api.openai.com/v1       # 模型 API 基础 URL（部分供应商可选）

# Model configuration
MODEL_NAME=gpt-4o                        # 使用的模型名称
MODEL_PROVIDER=openai                    # LangChain 模型供应商
LLM_PROVIDER=langchain                   # LLM 网关: langchain（默认）或 openai（OpenAI SDK）
TEMPERATURE=0.7                          # 模型温度参数
MAX_TOKENS=2000                          # 模型单次请求最大输出 token 数量

# Search engine configuration
SEARCH_PROVIDER=bing_web                 # anthropic_web / baidu / baidu_web / google / bing / bing_web / tavily / serper / custom
GOOGLE_SEARCH_API_KEY=                   # Google Search API 密钥（SEARCH_PROVIDER=google）
GOOGLE_SEARCH_ENGINE_ID=                 # Google 自定义搜索引擎 ID（SEARCH_PROVIDER=google）

# anthropic_web 使用当前 Anthropic 端点/密钥调用按次计费的服务端网页搜索。
# 该 API 没有可强制执行的搜索日期参数，请求 freshness/date_range 时会失败关闭。
# 非官方兼容网关必须显式绑定专用端点和密钥：
#ANTHROPIC_WEB_SEARCH_API_BASE=https://gateway.example
#ANTHROPIC_WEB_SEARCH_API_KEY=

# Sandbox configuration
SANDBOX_PROVIDER=docker                   # docker 或 agentbay
SANDBOX_ADDRESS=                         # 固定沙盒地址（开发用）；未设置时按会话创建容器
SANDBOX_IMAGE=simpleyyt/manus-sandbox    # 沙盒环境 Docker 镜像
SANDBOX_NAME_PREFIX=sandbox              # 沙盒容器名称前缀
SANDBOX_TTL_MINUTES=30                   # 沙盒容器生存时间（分钟）
SANDBOX_NETWORK=manus-network            # 仅运行时网络；禁止 sandbox/Claw 加入数据网络
SANDBOX_MEMORY_LIMIT=2g                  # 每个本地 Docker 沙箱的内存硬上限
SANDBOX_CPU_LIMIT=2.0                    # 每个本地 Docker 沙箱的 CPU 核数上限
SANDBOX_PIDS_LIMIT=512                   # 每个本地 Docker 沙箱的进程数上限
# 仅 AgentBay：从密钥管理器/provider 控制台注入这两个值
#AGENTBAY_API_KEY=
#AGENTBAY_IMAGE_ID=
#AGENTBAY_DEPLOYMENT_ID=

# Authentication configuration
AUTH_PROVIDER=password                   # password / local / none / sub2api
REGISTRATION_ENABLED=false               # 默认关闭公共注册
DEPLOYMENT_ENVIRONMENT=production         # 启用生产校验/默认行为
JWT_SECRET_KEY=                          # 用 openssl rand -hex 32 生成唯一值
CORS_ALLOWED_ORIGINS=https://manus.example.com  # 仅精确 origin，禁止 '*'
PASSWORD_HASH_ROUNDS=600000               # PBKDF2-SHA256，新用户使用随机 per-user salt
# Redis 限流默认值：账号/IP 登录每 300 秒 10/30 次、注册每小时 5 次、
# 密码重置每小时 5 次、refresh 每分钟 60 次（覆盖项见根目录 .env.example）
# 独立 BYOK keyring；JSON 数组中的每个条目都用 openssl rand -hex 32 生成
#MODEL_CREDENTIAL_ENCRYPTION_KEYS=[]

# Claw (OpenClaw) configuration
CLAW_ENABLED=false                       # 是否启用 Claw 集成
CLAW_IMAGE=simpleyyt/manus-claw          # Claw 容器 Docker 镜像
CLAW_TTL_SECONDS=0                       # 默认常驻；仅临时 runtime 使用正数
CLAW_PROXY_MAX_INPUT_BYTES=131072         # 单次请求的提示词/工具定义预算
CLAW_PROXY_REQUESTS_PER_MINUTE=30         # 每个运行中 Claw 的分钟请求预算
CLAW_PROXY_MAX_CONCURRENT_REQUESTS=2      # 每个运行中 Claw 的并发预算
CLAW_CHAT_TURN_LEASE_SECONDS=300           # 跨副本单轮对话租约
CLAW_CHAT_MAX_MESSAGE_BYTES=65536          # WebSocket 消息字节预算
CLAW_CHAT_MAX_ATTACHMENTS=10               # 单轮附件数量上限
CLAW_CHAT_MAX_ATTACHMENT_BYTES=26214400    # 单附件大小上限
CLAW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES=52428800 # 单轮附件总大小上限
CLAW_UPLOAD_MAX_BYTES=26214400             # 运行时能力上传大小上限
CLAW_HISTORY_MAX_MESSAGES=128              # 原子追加的有界历史保留条数
CLAW_API_KEY_HMAC_KEYS=                  # 独立 current,previous HMAC keyring；每项单独生成
MULTIPART_UPLOAD_MAX_BODY_BYTES=27262976    # multipart 解析前的 HTTP 请求体上限
FILE_UPLOAD_MAX_BYTES=26214400             # GridFS 单文件上限
FILE_STORAGE_MAX_BYTES_PER_USER=1073741824 # 每用户累计字节上限
FILE_STORAGE_MAX_FILES_PER_USER=1000       # 每用户文件数量上限

# MCP configuration
MCP_CONFIG_PATH=/etc/mcp.json            # 外部 MCP 服务配置文件路径

# Task backend configuration
TASK_BACKEND=local                       # local（进程内 asyncio）或 celery（分布式 worker）
BACKEND_REPLICA_COUNT=1                  # 大于 1 时必须使用 TASK_BACKEND=celery

# Database configuration
MONGODB_URI=mongodb://localhost:27017    # MongoDB 连接 URL
MONGODB_DATABASE=manus                   # MongoDB 数据库名称
# 生产 Redis 必须持久化且禁止逐出；项目自带 Compose 使用 AOF everysec + noeviction + 命名持久卷
REDIS_HOST=localhost                     # Redis 主机地址
REDIS_PORT=6379                          # Redis 端口
REDIS_DB=0                               # Redis 数据库编号

# Log configuration
LOG_LEVEL=INFO                           # 日志级别，可选: DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### 安全与部署约束

- 启用认证或处于 staging/production 时，`JWT_SECRET_KEY` 必须是至少 32 字节的唯一随机值。`REGISTRATION_ENABLED` 默认 `false`。密码账号使用至少 600,000 轮 PBKDF2-SHA256 和每用户随机 salt；`PASSWORD_SALT`、`PASSWORD_LEGACY_HASH_ROUNDS` 仅用于用户成功登录后升级旧哈希。
- 认证限流和 token 撤销以 Redis 为权威。refresh token 单次使用，并在同一可撤销 family 内轮换 access/refresh pair；logout 撤销整个 family。Sub2API handoff 使用一次性随机 `state`，只从 URL fragment 读取凭证，清理 URL 并经 `/auth/me` 验证后才写入；不透明外部 token 同样使用 refresh family 与 logout 保护。
- 生产 Redis 必须持久化且禁止逐出。保留项目自带的 AOF `everysec`、`noeviction` 和命名持久卷配置；使用外部 Redis 时提供同等级的持久化、备份和高可用。主机崩溃仍可能丢失约最后一秒的 AOF 写入，因此 Redis 不承担 AgentBay 计费硬账本；临时 Redis 也会丢失限流、已使用 refresh 和已撤销 family 状态。
- `CORS_ALLOWED_ORIGINS` 是逗号分隔的精确 origin 列表，拒绝 wildcard、路径、查询参数、fragment 和 URL 内嵌凭证；非本地开发必须显式配置。
- 生产 BYOK 必须配置独立的 `MODEL_CREDENTIAL_ENCRYPTION_KEYS` JSON keyring。首项负责新写入，后续项只在轮换期解密。先 dry-run `scripts/rotate_model_credential_keys.py`，核对后加 `--apply`，再次 dry-run 为零后才移除旧 key。
- 迁移 pre-marker 版本的明文 Agent 凭证时，临时用 `LEGACY_SYSTEM_API_KEYS` JSON 数组提供所有历史部署/模型目录 key。先 dry-run `scripts/migrate_agent_credentials.py` 并核对分类，再加 `--apply`；完成后从配置中移除历史明文 key。
- `CLAW_API_KEY_HMAC_KEYS` 每项必须独立生成。Claw 会执行分布式代理/对话租约、WebSocket 消息与附件限制、上传限制，以及硬上限 128 的原子聊天历史；轮换期间可保留 previous HMAC key 供懒更新。
- `/files` 和 `/claw/upload` 在 multipart 解析前限流，GridFS 另有单文件上限以及原子 per-user 字节/文件数配额；边缘代理的 body 上限必须同步。
- `TASK_BACKEND=local` 仅对一个 backend Python 进程安全。`BACKEND_REPLICA_COUNT` 必须填写真实进程/副本数；大于一时必须使用 Celery，启动校验会拒绝不安全组合。
- AgentBay 创建/删除采用 fail-safe：链接解析失败会回滚，未确认的清理会保留 provider session ID 供重试，删除会先等待 task 取消确认。AgentBay signed gateway link 是 bearer capability，禁止记录；适配器会关闭 SDK console/file logging。

## 运行方式

### 开发环境
```bash
# 启动开发服务器（带热重载功能）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --ws-max-size 131072
```

服务将在 http://localhost:8000 启动。

### Docker部署
```bash
# 构建Docker镜像
docker build -t manus-ai-agent .

# 运行容器
docker run -p 8000:8000 --env-file .env -v /var/run/docker.sock:/var/run/docker.sock manus-ai-agent
```

> 注意：如果使用Docker部署，需要挂载Docker套接字以便后端可以创建沙盒容器。

## API接口文档

基础URL: `/api/v1`。服务运行时可通过 `/docs` 访问交互式 Swagger UI。

所有 JSON 接口返回统一格式：
```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

### 会话接口（`/api/v1/sessions`）

| 方法 | 路径 | 描述 |
|---|---|---|
| PUT | `/sessions` | 创建新的对话会话 |
| GET | `/sessions` | 获取所有会话列表 |
| POST | `/sessions` | 以 SSE 流式获取会话列表更新 |
| GET | `/sessions/{session_id}` | 获取会话详情（包括事件历史） |
| DELETE | `/sessions/{session_id}` | 删除会话 |
| POST | `/sessions/{session_id}/stop` | 停止活跃的会话 |
| POST | `/sessions/{session_id}/chat` | 发送消息并接收 SSE 事件流 |
| POST | `/sessions/{session_id}/clear_unread_message_count` | 清除未读消息计数 |
| POST | `/sessions/{session_id}/shell` | 查看沙盒中的 Shell 会话输出 |
| POST | `/sessions/{session_id}/file` | 查看沙盒中的文件内容 |
| GET | `/sessions/{session_id}/files` | 获取会话关联的文件列表 |
| WebSocket | `/sessions/{session_id}/vnc` | 与沙盒建立 VNC 连接（binary 子协议） |
| POST | `/sessions/{session_id}/vnc/signed-url` | 生成 VNC WebSocket 访问签名 URL |
| POST | `/sessions/{session_id}/share` | 公开分享会话 |
| DELETE | `/sessions/{session_id}/share` | 取消分享会话 |
| GET | `/sessions/{session_id}/share/files` | 获取已分享会话的文件列表 |
| GET | `/sessions/shared/{session_id}` | 获取已分享会话（无需认证） |

`/chat` 输出的 SSE 事件类型：`message`、`title`、`plan`、`step`、`tool`、`wait`、`error`、`done`。

### 文件接口（`/api/v1/files`）

| 方法 | 路径 | 描述 |
|---|---|---|
| POST | `/files` | 上传文件 |
| GET | `/files/{file_id}` | 下载文件（支持签名访问令牌） |
| GET | `/files/{file_id}/download` | 以附件形式下载文件 |
| DELETE | `/files/{file_id}` | 删除文件 |
| GET | `/files/{file_id}/info` | 获取文件元信息 |
| POST | `/files/{file_id}/signed-url` | 生成签名下载 URL |

### 认证接口（`/api/v1/auth`）

| 方法 | 路径 | 描述 |
|---|---|---|
| POST | `/auth/login` | 登录 |
| POST | `/auth/register` | 仅在 `REGISTRATION_ENABLED=true` 时注册新用户 |
| GET | `/auth/status` | 获取认证提供方状态 |
| GET | `/auth/me` | 获取当前用户信息 |
| POST | `/auth/refresh` | 轮换单次使用的 refresh token，并返回新的 access/refresh pair |
| POST | `/auth/logout` | 撤销登录 token family（Sub2API 需在 body 中提交 refresh token） |
| POST | `/auth/change-password` | 修改密码 |
| POST | `/auth/change-fullname` | 修改用户名称 |
| POST | `/auth/send-verification-code` | 发送邮箱验证码 |
| POST | `/auth/reset-password` | 通过验证码重置密码 |
| GET | `/auth/user/{user_id}` | 按 ID 获取用户 |
| POST | `/auth/user/{user_id}/activate` | 激活用户 |
| POST | `/auth/user/{user_id}/deactivate` | 停用用户 |

### Claw 接口（`/api/v1/claw`）

| 方法 | 路径 | 描述 |
|---|---|---|
| GET | `/claw` | 获取当前用户的 Claw 实例 |
| POST | `/claw` | 为当前用户创建 Claw 实例 |
| DELETE | `/claw` | 删除当前用户的 Claw 实例 |
| GET | `/claw/history` | 获取合并后的 Claw 聊天历史 |
| POST | `/claw/upload` | 从 Claw 工作区上传文件（Claw API 密钥认证） |
| GET | `/claw/files/{filename}` | 代理下载 Claw 工作区中的文件 |
| GET | `/claw/resolve/{file_id}` | 解析 `manus-file://` 元信息（Claw API 密钥认证） |
| GET | `/claw/resolve/{file_id}/download` | 下载 `manus-file://` 内容（Claw API 密钥认证） |
| WebSocket | `/claw/ws` | Claw 聊天的持久 WebSocket 连接 |

LLM 代理能力只会注入所属的 Claw 运行时，不提供任何面向用户的密钥接口，
并且仅在对应 Claw 记录处于运行状态时有效。

### 其它接口

| 方法 | 路径 | 描述 |
|---|---|---|
| GET | `/api/v1/config/frontend` | 前端运行时配置 |
| POST | `/v1/chat/completions` | 供 Claw 容器使用的 OpenAI 兼容 LLM 代理 |

## 错误处理

所有API在发生错误时会返回统一格式的响应：
```json
{
  "code": 400,
  "msg": "错误描述",
  "data": null
}
```

常见错误码：
- `400`: 请求参数错误
- `404`: 资源不存在
- `500`: 服务器内部错误

## 开发指南

### 添加新工具

1. 在 `domain/external` 目录下定义 Protocol 接口
2. 在 `infrastructure/external` 层实现功能
3. 在 `interfaces/dependencies.py` 中完成依赖注入
4. 如需供 Agent 调用，在 `domain/services/tools` 中封装为工具集
