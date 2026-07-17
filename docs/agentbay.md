# 使用阿里云无影 AgentBay 云沙箱

默认情况下，AI Manus 使用本机 Docker 创建沙箱。设置 `SANDBOX_PROVIDER=agentbay` 后，后端改为按需创建阿里云无影 AgentBay 会话；Shell、文件、浏览器和 VNC 仍复用相同的 `Sandbox` 接口。

## 端口桥接

AgentBay 网关默认开放 `30100-30199` 端口段。自定义沙箱镜像中的 `socat` 进程会转发三个服务（配置见 `sandbox/supervisord.conf`）：

| AgentBay 网关端口 | 镜像内服务 | 用途 |
|---|---|---|
| `30150` → `8080` | Sandbox FastAPI | Shell、文件、Supervisor 与网页代理 API |
| `30151` → `8222` | Chrome CDP | Playwright / browser-use 浏览器自动化 |
| `30152` → `5901` | websockify | 前端 VNC 实时画面 |

端口可以通过 `AGENTBAY_API_PORT`、`AGENTBAY_CDP_PORT`、`AGENTBAY_VNC_PORT` 修改，但必须同时修改镜像中的 `socat` 监听端口。

## 接入步骤

1. 构建 `sandbox/` 镜像，推送到镜像仓库，并在 AgentBay 控制台注册为自定义镜像。
2. 从 AgentBay 控制台获取 API Key 和自定义镜像 ID。
3. 在后端 `.env` 中配置：

```env
SANDBOX_PROVIDER=agentbay
AGENTBAY_API_KEY=<从密钥管理器注入>
AGENTBAY_IMAGE_ID=<控制台中的自定义镜像ID>
AGENTBAY_DEPLOYMENT_ID=<所有 backend/worker 副本共享的稳定部署名>
AGENTBAY_QUOTA_CONFIG_VERSION=1
# 可选：留空时使用 SDK 默认地域
#AGENTBAY_REGION_ID=ap-southeast-1
SANDBOX_TTL_MINUTES=30
```

后端使用 `wuying-agentbay-sdk`。更新依赖后，可直接在本地启动 backend；创建会话时日志应只显示 AgentBay session ID 以及“网关链接已解析”，不会显示 API/CDP/VNC 完整链接。

## 首次启用与旧版本迁移

AgentBay session 是计费资源，因此 MongoDB 中的单文档成本账本是创建与释放配额的唯一真源。全新部署只有在“Mongo 没有任何 `sandbox_id` 且 AgentBay 账号全量 session 也为空”时才会自动初始化；只要发现旧会话或无法确认 inventory，就会保持 `reconciling` 并拒绝继续创建。

从旧版升级时，先运行只读审计：

```bash
cd backend
uv run python scripts/reconcile_agentbay_quota.py
```

它会分页扫描整个 AgentBay 账号、逐个 exact lookup，并与 Mongo session owner 对照。孤儿 provider、Mongo 中已精确不存在的指针、重复归属、查询不确定或超过配额都会停止，且不会删除或修改任何资源。确认数量后再显式写入：

```bash
uv run python scripts/reconcile_agentbay_quota.py --apply
```

工具会先关闭创建闸门，再完整复扫，最后用 revision CAS 原子替换 inventory。已经 ready 但内容不同的账本还需要人工复核后额外传入 `--replace-ready-ledger`。普通配置变更不能静默覆盖账本；所有副本必须保持相同的 deployment ID、config version 和配额。

## 生命周期与回退

- `SANDBOX_TTL_MINUTES` 会转换为 AgentBay 的空闲释放秒数。
- API 进程必须先持有 session 的可续租分布式 lease，再原子占用 Mongo 配额、携带稳定哈希 labels 创建 provider、先写账本 provider ID、再写 Session 指针，最后才解析网关链接并创建 task。
- 网关链接解析失败、Session 投影失败或调用被取消时，不会丢弃已经计费的资源：账本和 operation labels 保留精确恢复入口，重试会复用而不是重复创建。
- 从旧版本升级而来的 Session 可能已有 `sandbox_id` 但没有 `sandbox_provider`。只有配额账本中该逻辑 Session 的 `PROVISIONED` reservation 与同一个 provider ID 精确匹配时，后端才会自动补写 `agentbay` 标记；缺失、阶段不对或 ID 不同都会 fail closed，并要求先 reconciliation。
- Worker 只允许 exact-get 已持久化的 sandbox。若 provider 已精确不存在，Worker 不会偷偷创建替代品；API 在 lease 内替换 operation、终止旧任务流，并用新 sandbox 参数创建新 task。
- 删除 Manus 会话时，后端先请求 Agent task 停止并等待终态，再由 provisioner 删除 AgentBay session；SDK 的“删除成功”之后还会独立 exact-get 确认不存在，随后清 Session 指针并用 operation/provider CAS 释放 Mongo 配额。任一步不确定都会保留数据库恢复入口。
- 不要直接把运行中的部署从 `agentbay` 切到 `docker`。回退前应保持 `SANDBOX_PROVIDER=agentbay`，先停止接收新的会话/对话并 drain 所有进行中的任务，再按上文执行 reconciliation dry-run。必须先人工处理孤儿、重复归属和查询不确定；若审计确认的是旧版本 owner，则执行 `--apply` 把精确归属写入账本，后端随后才能在正常清理流程中安全补写 marker。
- 重试正常的会话删除流程，直到每个 AgentBay provider 都已由 exact lookup 确认不存在、对应配额释放也已确认，然后再次 dry-run 核对最终空 inventory。配额释放失败时，Session 会故意保留 `sandbox_provider=agentbay`，这是尚待清理的恢复标记，不能手工清除。只有审计确认 Mongo owner 与 live provider 都已归零、账本状态一致，且所有 Session 都不再带有 AgentBay 归属标记后，才能把所有 backend/worker 副本一起切换为 `SANDBOX_PROVIDER=docker`。切换本身无需修改前端或工具协议。
- AgentBay 网关 URL 带签名参数，是 bearer capability。适配器会在创建 SDK client 前关闭其 console/file logging，应用自身也只记录 session ID。不要把完整 signed link 写入日志、异常、trace、指标 label、截图或工单。

若使用 `TASK_BACKEND=celery`，worker 必须和 backend 使用相同的 AgentBay、MongoDB 与 Redis 配置，但 Worker 没有创建/释放计费 session 的权限。
