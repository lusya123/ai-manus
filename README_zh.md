# AI Manus

[English](README.md) | 中文 | [官方网站](https://ai-manus.com) | [文档](https://docs.ai-manus.com)

[![GitHub stars](https://img.shields.io/github/stars/simpleyyt/ai-manus?style=social)](https://github.com/simpleyyt/ai-manus/stargazers)
&ensp;
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

AI Manus 是一个通用的 AI Agent 系统，支持在沙盒环境中运行各种工具和操作。现已深度集成 **Claw** —— 基于 [OpenClaw](https://github.com/anthropics/openclaw) 的 AI 助手，一键部署、用户隔离常驻容器、可选过期策略与无缝聊天历史，为 Manus 生态带来全新体验。

用 AI Manus 开启你的智能体之旅吧！

👏 欢迎加入 [QQ群(1005477581)](https://qun.qq.com/universal-share/share?ac=1&authKey=p4X3Da5iMpR4liAenxwvhs7IValPKiCFtUevRlJouz9qSTSZsMnPJc3hzsJjgQYv&busi_data=eyJncm91cENvZGUiOiIxMDA1NDc3NTgxIiwidG9rZW4iOiJNZmUrTmQ0UzNDZDNqNDFVdjVPS1VCRkJGRWVlV0R3RFJSRVFoZDAwRjFDeUdUM0t6aUIyczlVdzRjV1BYN09IIiwidWluIjoiMzQyMjExODE1In0%3D&data=C3B-E6BlEbailV32co77iXL5vxPIhtD9y_itWLSq50hKqosO_55_isOZym2Faaq4hs9-517tUY8GSWaDwPom-A&svctype=4&tempid=h5_group_info)

❤️ 喜欢 AI Manus? 点亮小星星 🌟 或 [赞助开发者](docs/sponsor.md)! ❤️

🚀 [Demo 演示](https://app.ai-manus.com)

📝 [博客：我也复刻了一个 Manus，带高仿 WebUI 和沙盒](https://simpleyyt.com/2026/03/07/rebuild-manus-with-webui-and-sandbox/)

## 示例

<!-- demos:readme:zh -->
### 基本功能

* 任务: Code Use、Browser Use 与多会话切换

https://github.com/user-attachments/assets/83d9549b-1a99-4c06-b39e-1bc0b48b3055

### Browser Use

* 任务: 找一下最新新闻

https://github.com/user-attachments/assets/f7297f8f-51fd-44c0-9ff9-0b7fcfaabf0f

### Code Use

* 任务: 写一个复杂的 python 示例

https://github.com/user-attachments/assets/7b39b828-ec27-4b8f-b5f7-527e29efbe48
<!-- /demos:readme:zh -->

## 主要特性

 * 部署：最小只需要一个 LLM 服务即可完成部署，不需要依赖其它外部服务。
 * Agent 循环：Plan-and-Execute，可组合 System Prompt，以及原生结构化输出工具（不再使用脆弱的 Prompt 内嵌 JSON 协议）。
 * 工具：支持 Terminal、Browser、File、Web Search、消息工具，并支持实时查看和接管，支持外部 MCP 工具集成。
 * Claw：集成 [OpenClaw](https://github.com/anthropics/openclaw) AI 助手，一键部署、用户隔离常驻容器、可选过期策略、完整聊天历史。
 * 沙盒：每个 Task 会分配独立沙盒，可使用本地 Docker 或可选的[阿里云无影 AgentBay](docs/agentbay.md)。
 * 任务会话：通过 Mongo/Redis 对会话历史进行管理，支持后台任务。
 * 库：侧栏「库」聚合各会话产生的附件与产物，支持类型筛选、搜索、文件收藏与预览，并可定位回原任务。
 * 对话：支持停止与打断，支持文件上传与下载。
 * 多语言：支持中文与英文。
 * 认证：用户登录与认证。

## 开发计划

 * 工具：支持 Deploy & Expose。
 * 沙盒：支持手机与 Windows 电脑接入。
 * 部署：支持 K8s 和 Docker Swarm 多集群部署。

完整清单见 [docs/roadmap.md](docs/roadmap.md)（含已完成的 Docker Compose、设置页、Celery 后端、上下文工程等）。

## 环境要求

本项目主要依赖Docker进行开发与部署，需要安装较新版本的Docker：
- Docker 20.10+
- Docker Compose

模型能力要求：
- 支持 LangChain Chat Model（默认 `openai` 提供商）
- 支持原生 **Tool / Function Calling**（计划与步骤结果通过 `create_plan` / `complete_step` 等结构化输出工具提交，不再依赖 Prompt 内嵌 JSON）

推荐使用具备稳定工具调用能力的 Deepseek 与 GPT 模型。


## 部署指南

推荐使用Docker Compose进行部署：

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

保存成 `docker-compose.yml` 文件。模型与功能配置通过 `.env` 加载，安全和运行时隔离约束仍由 Compose 显式覆盖。请在同级目录下参考 [.env.example](https://github.com/simpleyyt/ai-manus/blob/main/.env.example) 创建 `.env`，至少填写 `API_KEY`，并用 `openssl rand -hex 32` 生成独立的 `JWT_SECRET_KEY`：

```ini
API_KEY=
# 粘贴 `openssl rand -hex 32` 的唯一输出
JWT_SECRET_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
```

然后运行：

```shell
docker compose up -d
```

> 注意：如果提示`sandbox-1 exited with code 0`，这是正常的，这是为了让 sandbox 镜像成功拉取到本地。

打开浏览器访问<http://localhost:5173>即可访问 Manus。更多配置见：https://docs.ai-manus.com/#/configuration

## 开发指南

### 项目结构

本项目由以下子项目组成：

* `frontend`: Manus 前端
* `backend`: Manus 后端
* `sandbox`: Manus 沙盒
* `claw`: Manus Claw —— OpenClaw 插件与容器镜像，桥接 OpenClaw Gateway 与 Manus 后端
* `mockserver`: 模拟 LLM 服务（开发/测试用）

### 整体设计

![Image](https://github.com/user-attachments/assets/69775011-1eb7-452f-adaf-cd6603a4dde5)

**当用户发起对话时：**

1. Web 向 Server 发送创建 Agent 请求，Server 通过`/var/run/docker.sock`创建出 Sandbox，并返回会话 ID。
2. Sandbox 是一个 Ubuntu Docker 环境，里面会启动 chrome 浏览器及 File/Shell 等工具的 API 服务。
3. Web 往会话 ID 中发送用户消息，Server 收到用户消息后，将消息发送给 PlanAct Agent 处理。
4. PlanAct Agent 进行规划与执行：规划器/执行器通过原生工具调用提交结构化结果，并按需调用沙盒工具（Shell / Browser / File / Search / MCP）。
5. Agent 处理过程中产生的所有事件通过 WebSocket 发回 Web。

**当用户浏览工具时：**

- 浏览器：
    1. Sandbox 的无头浏览器通过 xvfb 与 x11vnc 启动了 vnc 服务，并且通过 websockify 将 vnc 转化成 websocket。
    2. Web 的 NoVNC 组件通过 Server 的 Websocket Forward 转发到 Sandbox，实现浏览器查看。
- 其它工具：其它工具原理也是差不多。

### 环境准备

1. 下载项目：
```bash
git clone https://github.com/simpleyyt/ai-manus.git
cd ai-manus
```

2. 复制配置文件：
```bash
cp .env.example .env
```

3. 修改配置文件，至少设置 `API_KEY`，并配置一个不少于 32 字节的独立 `JWT_SECRET_KEY`（可用 `openssl rand -hex 32` 生成）。完整配置项见 [.env.example](https://github.com/simpleyyt/ai-manus/blob/main/.env.example) 或[配置说明](https://docs.ai-manus.com/#/configuration)：

```ini
API_KEY=
# 粘贴 `openssl rand -hex 32` 的唯一输出
JWT_SECRET_KEY=
API_BASE=https://api.openai.com/v1
MODEL_NAME=gpt-4o
```

### 开发调试

1. 运行调试：
```bash
# 相当于 docker compose -p ai-manus-dev -f docker-compose-development.yml up
./dev.sh up
```

各服务会以 reload 模式运行，代码改动会自动重新加载。暴露的端口如下：
- 5173: Web前端端口
- 8000: Server API服务端口
- 5678: Server debugpy 端口（Python 远程调试）
- 8080: Sandbox API服务端口
- 5902: Sandbox VNC端口（映射容器内 5900）
- 18788: Claw（OpenClaw Gateway）端口
- 27017: MongoDB 端口

> *注意：在 Debug 模式全局只会启动一个沙盒*

开发环境的 MongoDB 现在使用独立的 `manus-mongodb-data-dev` 卷，避免误改生产数据。
已有的 `manus-mongodb-data` 不会被删除，也不会自动挂载；如确实需要旧数据，请先备份，
再有选择地迁移到开发卷。辅助脚本还会分别使用 `ai-manus`、`ai-manus-dev`
两个 Compose 项目和各自的运行时网络。

2. 当依赖变化时（`backend/pyproject.toml` 或 `frontend/package.json`），清理并重新构建：
```bash
# 清理所有相关资源
./dev.sh down -v

# 重新构建镜像
./dev.sh build

# 调试运行
./dev.sh up
```

### 镜像发布

```bash
export IMAGE_REGISTRY=your-registry-url
export IMAGE_TAG=latest

# 构建镜像
./run.sh build

# 推送到相应的镜像仓库
./run.sh push
```

生产 Compose 会让动态创建的 Sandbox、Claw 镜像同步跟随 `IMAGE_REGISTRY` 与
`IMAGE_TAG`。只有运行时确实来自另一仓库时，才单独设置 `SANDBOX_IMAGE` 或
`CLAW_IMAGE`。

## 生产安全检查清单

- 设置 `DEPLOYMENT_ENVIRONMENT=production`，并生成至少 32 字节且每套部署唯一的 `JWT_SECRET_KEY`。公共注册默认关闭（`REGISTRATION_ENABLED=false`），密码使用每用户随机 salt 的 PBKDF2-SHA256，认证端点由 Redis 限流。
- Redis 保存的是安全状态，不是可随意丢弃的缓存。保留项目自带的 AOF `everysec`、`noeviction` 和命名持久卷设置；使用外部 Redis 时提供同等级的持久化、备份和高可用。主机崩溃仍可能丢失约最后一秒的 AOF 写入，因此 Redis 不承担 AgentBay 计费硬账本；数据丢失可能会清除 logout 与 refresh replay 记录。
- `CORS_ALLOWED_ORIGINS` 只配置精确 origin；`*`、路径、查询参数和 URL 内嵌凭证都会被拒绝。refresh token 单次使用，并在 logout 可撤销的 family 内轮换。
- 生产 BYOK 必须使用独立 `MODEL_CREDENTIAL_ENCRYPTION_KEYS` keyring。轮换使用 dry-run 优先的 `backend/scripts/rotate_model_credential_keys.py`；旧明文 Agent 记录使用 `migrate_agent_credentials.py`，并临时提供完整的 `LEGACY_SYSTEM_API_KEYS` 历史列表。
- 启用 Claw 时配置独立 `CLAW_API_KEY_HMAC_KEYS` keyring。Claw 会限制 WebSocket 消息/附件、分布式请求/对话、上传大小及历史长度。
- 文件上传有 multipart 解析前 body 上限、单文件上限和原子的每用户字节/文件数配额；反向代理限制必须与 backend 保持一致。
- `TASK_BACKEND=local` 仅支持一个 backend Python 进程。填写真实 `BACKEND_REPLICA_COUNT`；多个副本或 worker 必须使用 Celery。
- 生命周期围栏字段（`deleting`、`sandbox_destroying`、task/runtime generation 绑定）要求停机升级：必须先停止全部旧 backend 与 worker，再启动此版本；禁止新旧生命周期协议混跑。项目内置的 fork 部署流程会执行完整的停机/启动顺序。
- `run.sh` 会为自定义 `COMPOSE_PROJECT_NAME` 派生唯一 `CORE_RESOURCE_PREFIX`，隔离控制网、数据网及 Mongo/Redis 数据卷。直接调用自定义 Compose 项目时也必须显式设置唯一前缀；同一 Docker daemon 上不同部署不得复用。
- 每个运行时的控制网与出口网会阻断跨运行时和数据网访问，TTL AutoRemove 后的孤儿网桥也会定期回收；但它不是宿主机/VPC 出口防火墙。若还要禁止沙箱访问 Docker host、RFC1918、link-local 或云 metadata，请另配宿主机防火墙或目标过滤代理。
- AgentBay 删除未获确认时会保留 provider ID 供重试；其 signed gateway URL 是 bearer capability，禁止写入日志。

完整操作步骤见[配置说明](docs/configuration.md)与 [AgentBay 云沙箱](docs/agentbay.md)。

## ⭐️ Star 记录

[![Star History Chart](https://api.star-history.com/svg?repos=Simpleyyt/ai-manus&type=Date)](https://www.star-history.com/#Simpleyyt/ai-manus&Date)
