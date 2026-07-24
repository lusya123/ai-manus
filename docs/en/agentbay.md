# Alibaba Cloud Wuying AgentBay Sandboxes

AI Manus uses local Docker sandboxes by default. With `SANDBOX_PROVIDER=agentbay`, the backend creates Alibaba Cloud Wuying AgentBay sessions on demand while keeping the same `Sandbox` interface for shell, file, browser, and VNC operations.

## Port bridging

The AgentBay gateway exposes ports in the `30100-30199` range by default. The custom sandbox image runs `socat` forwards for three services (see `sandbox/supervisord.conf`):

| AgentBay gateway port | Service in the image | Purpose |
|---|---|---|
| `30150` → `8080` | Sandbox FastAPI | Shell, file, Supervisor, and web-app proxy APIs |
| `30151` → `8222` | Chrome CDP | Playwright / browser-use automation |
| `30152` → `5901` | websockify | Live VNC view in the frontend |

You can change these through `AGENTBAY_API_PORT`, `AGENTBAY_CDP_PORT`, and `AGENTBAY_VNC_PORT`, but the values must also match the `socat` listeners baked into the image.

## Setup

1. Build the `sandbox/` image, push it to a registry, and register it as a custom image in the AgentBay console.
2. Obtain an API key and the custom image ID from the AgentBay console.
3. Configure the backend `.env`:

```env
SANDBOX_PROVIDER=agentbay
AGENTBAY_API_KEY=<inject-from-secret-manager>
AGENTBAY_IMAGE_ID=<custom-image-id-from-console>
AGENTBAY_DEPLOYMENT_ID=<stable-name-shared-by-all-backend-and-worker-replicas>
AGENTBAY_QUOTA_CONFIG_VERSION=1
# Optional; omit to use the SDK default region
#AGENTBAY_REGION_ID=ap-southeast-1
SANDBOX_TTL_MINUTES=30
```

The backend uses `wuying-agentbay-sdk`. After syncing dependencies, start the backend locally. Session creation logs should contain only the AgentBay session ID and a notice that gateway links were resolved, never the complete API/CDP/VNC links.

## First enablement and legacy migration

AgentBay sessions are billable, so a single MongoDB cost-ledger document is the sole authority for reservation and release. A fresh deployment initializes itself only when Mongo has no `sandbox_id` ownership and the complete AgentBay account inventory is empty. Any legacy or uncertain inventory leaves the ledger `reconciling` and provisioning fails closed.

When upgrading an existing deployment, run the read-only audit first:

```bash
cd backend
uv run python scripts/reconcile_agentbay_quota.py
```

It paginates the complete AgentBay account, performs exact lookups, and compares every provider with its Mongo session owner. Provider orphans, exact-missing Mongo pointers, duplicate ownership, inconclusive queries, or quota overflow stop the audit without deleting or changing resources. After reviewing the counts, explicitly apply the inventory:

```bash
uv run python scripts/reconcile_agentbay_quota.py --apply
```

The tool closes the provisioning gate, rescans, and atomically replaces inventory with a revision CAS. Replacing a differing already-ready ledger additionally requires `--replace-ready-ledger` after manual review. Ordinary configuration changes never overwrite ledger ownership; every replica must share the same deployment ID, config version, and caps.

## Lifecycle and rollback

- `SANDBOX_TTL_MINUTES` is converted to AgentBay's idle-release timeout in seconds.
- The API must hold the session's renewable distributed lease, atomically reserve Mongo capacity, allocate with stable hashed labels, persist the provider ID to the ledger, project it to Session, and only then resolve gateway links and create a task.
- Link-resolution failure, Session projection failure, and cancellation retain exact recovery through the ledger and operation labels. A retry reuses the provider instead of allocating another billable session.
- A Session upgraded from an older release may have `sandbox_id` without `sandbox_provider`. The backend backfills the `agentbay` marker only when that logical Session has a `PROVISIONED` ledger reservation with the exact same provider ID. A missing reservation, wrong phase, or different ID fails closed and requires reconciliation first.
- Workers are get-only. An exact-missing provider never causes a worker-side replacement; the API replaces the operation under its lease, retires the old task stream, and creates a new task with the new sandbox parameters.
- Deletion waits for task terminal acknowledgement, then the provisioner deletes the provider. Because the SDK's success result is not authoritative enough, an independent exact lookup must confirm absence before Session pointers are cleared and Mongo capacity is released with operation/provider CAS. Any uncertain step retains the database recovery path.
- Do not switch a live deployment directly from `agentbay` to `docker`. Keep `SANDBOX_PROVIDER=agentbay`, stop accepting new sessions/turns, drain every in-flight task, and then run the reconciliation dry-run described above. Resolve every orphan, duplicate owner, and inconclusive lookup first. If the audit verifies owners created by an older release, run `--apply` to establish their exact ledger ownership so the normal cleanup path can safely backfill their markers.
- Retry normal session deletion until exact lookup confirms every AgentBay provider is absent and every quota release is confirmed, then run a final dry-run against the empty inventory. If quota release fails, Session deliberately retains `sandbox_provider=agentbay` as a pending-cleanup recovery marker; never clear it manually. Switch all backend and worker replicas together to `SANDBOX_PROVIDER=docker` only after the audit reports zero Mongo owners and zero live providers, the ledger is consistent, and no Session retains AgentBay ownership. The frontend and tool protocols do not need to change.
- AgentBay gateway URLs contain signed bearer capabilities. The adapter disables SDK console/file logging before creating the client, and application logs contain only session IDs. Never put a full signed link in logs, exceptions, traces, metric labels, screenshots, or tickets.

With `TASK_BACKEND=celery`, workers must share the backend's AgentBay, MongoDB, and Redis settings, but workers never have authority to allocate or release billable sessions.
