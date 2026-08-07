# AGENTS.md

> Canonical guide for AI coding agents working on the **AI Manus × Claw** codebase.

---

## Project Overview

AI Manus × Claw is a general-purpose AI Agent system with an integrated [OpenClaw](https://github.com/anthropics/openclaw) AI assistant, comprising five services:

| Service | Stack | Port (dev) | Entry Point |
|---|---|---|---|
| **Frontend** | Vue 3 + TypeScript, Vite 4, Tailwind CSS | 5173 | `frontend/src/main.ts` |
| **Backend** | Python 3.12, FastAPI, LangChain, Beanie/Motor | 8000 | `backend/app/main.py` |
| **Sandbox** | Python 3.10, FastAPI, Xvfb/Chrome/VNC | 8080 (API), 5900 (VNC) | `sandbox/app/main.py` |
| **Claw** | Node.js, OpenClaw Gateway, manus-claw plugin | 18788 | `claw/entrypoint.sh` |
| **Mockserver** | Python, FastAPI | 8090 | `mockserver/main.py` |

Infrastructure: **MongoDB 7.0**, **Redis 7.0**, **Docker** (sandbox & Claw orchestration).

---

## Directory Structure

```
ai-manus/
├── frontend/          # Vue 3 SPA (Vite, TypeScript, Tailwind)
├── backend/           # FastAPI backend (DDD layout)
│   └── app/
│       ├── domain/           # Models, services, tools, agents, repositories
│       ├── application/      # Application services (auth, agent, file, token, email, claw)
│       ├── infrastructure/   # External integrations (search, browser, sandbox, claw, DB, cache)
│       ├── interfaces/       # API routes, schemas, error handlers, dependencies
│       ├── core/             # Config (config.py)
│       └── main.py
├── sandbox/           # Sandbox service (shell, file, supervisor APIs)
├── claw/              # Claw service (OpenClaw Gateway + manus-claw plugin)
│   └── manus-claw/   # Node.js plugin bridging OpenClaw with Manus backend
├── mockserver/        # Mock LLM server for dev/testing
├── docs/              # Docsify documentation site
├── .cursor/skills/    # Cursor agent skills
├── dev.sh             # Shortcut: docker compose -f docker-compose-development.yml ...
├── run.sh             # Shortcut: docker compose -f docker-compose.yml ...
├── build.sh           # docker buildx bake
├── .env.example       # Environment variable template
├── docker-compose.yml                # Production compose
└── docker-compose-development.yml    # Development compose (hot-reload)
```

---

## Development Environment Setup

### Prerequisites

- **Docker 20.10+** and **Docker Compose**
- **uv** (Python package manager) — for running backend/sandbox outside Docker
- **Node.js / npm** — for running frontend outside Docker
- **Python 3.12+** (backend), **Python 3.10+** (sandbox)

### Quick Start (Docker Compose — Recommended)

```bash
cp .env.example .env
# Edit .env — set API_KEY and generate JWT_SECRET_KEY with: openssl rand -hex 32
./dev.sh up -d
```

This starts: frontend (5173), backend (8000), sandbox (8080), mockserver (8090), MongoDB (27017), Redis.

### Key `.env` Values for Development

| Variable | Recommended Value | Purpose |
|---|---|---|
| `AUTH_PROVIDER` | `none` | Skip authentication entirely |
| `API_BASE` | `http://mockserver:8090/v1` | Use mock LLM server |
| `API_KEY` | any non-empty string | Required — set to anything with mockserver |
| `JWT_SECRET_KEY` | output of `openssl rand -hex 32` | Required whenever authentication is enabled; also required in staging/production |
| `REGISTRATION_ENABLED` | `false` | Keep public account creation closed unless a test explicitly needs it |
| `BACKEND_REPLICA_COUNT` | `1` | `TASK_BACKEND=local` is process-local and supports only one backend process |
| `SEARCH_PROVIDER` | `bing_web` | No API key needed |
| `SANDBOX_ADDRESS` | `sandbox` | Use single dev sandbox container |
| `LOG_LEVEL` | `DEBUG` | Verbose logging |

### Running Services Individually (Without Docker)

**Backend:**
```bash
cd backend
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Requires running MongoDB and Redis. Requires `API_KEY` and, unless this is disposable local development with `AUTH_PROVIDER=none`, a strong `JWT_SECRET_KEY` env var (or a `.env` file in `backend/`).

### Security and Topology Invariants

- Never add a usable fixed production secret to documentation, tests, Compose examples, or `.env.example`. Generate JWT, BYOK, Claw HMAC, and local-auth secrets independently.
- Password auth uses random per-user PBKDF2-SHA256 salts and at least 600,000 rounds. `PASSWORD_SALT` and `PASSWORD_LEGACY_HASH_ROUNDS` are legacy-login migration inputs, not defaults for new accounts. Public registration defaults off; auth limits and revocation depend on Redis and fail closed.
- Redis security state must be durable and non-evicting. Preserve the bundled AOF `everysec`, `noeviction`, and named-volume settings, or provide equivalent persistence, backups, and high availability for an external Redis. A host crash can still lose roughly one second of AOF writes, so never treat Redis as a hard billing ledger or refresh/revocation state as an ephemeral cache.
- CORS accepts exact HTTP(S) origins only. Do not add `*`, credentialed CORS, or broad regex origins. Sub2API handoff credentials belong in a one-time-state-correlated URL fragment and must be verified before storage.
- Production BYOK uses `MODEL_CREDENTIAL_ENCRYPTION_KEYS`, not JWT-derived encryption. Use `backend/scripts/rotate_model_credential_keys.py` dry-run first. Pre-marker plaintext credentials require the complete temporary `LEGACY_SYSTEM_API_KEYS` history and `migrate_agent_credentials.py` dry-run before `--apply`.
- Claw runtime-key hashes use the independent `CLAW_API_KEY_HMAC_KEYS` keyring. Preserve its WebSocket input/attachment limits, distributed leases, upload limits, and bounded history.
- Upload changes must preserve the pre-parser HTTP body cap, verified per-file cap, and atomic per-user GridFS byte/file reservations. Keep Nginx and backend body limits aligned.
- `TASK_BACKEND=local` is valid for exactly one Python process. Set `BACKEND_REPLICA_COUNT` to the actual Uvicorn/Gunicorn/container replica count and use Celery whenever it is greater than one.
- AgentBay session IDs are cleanup handles for billable resources. Preserve them whenever deletion is unconfirmed, wait for task cancellation before destruction, and never log signed AgentBay gateway URLs.

**Frontend:**
```bash
cd frontend
npm install
BACKEND_URL=http://localhost:8000 npm run dev
```
The Vite config creates a proxy for `/api` when `BACKEND_URL` is set.

**Sandbox:** Typically Docker-only (requires Xvfb, Chrome, VNC, supervisord).

**Mockserver:**
```bash
cd mockserver
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8090 --reload
```

---

## Testing

### Backend Tests (pytest — integration-style)

Tests live in `backend/tests/` and hit a **running** backend at `http://localhost:8000`.

```bash
# Ensure backend + MongoDB + Redis are running
./dev.sh up -d mongodb redis backend

cd backend
uv run pytest                               # all tests
uv run pytest tests/test_auth_routes.py     # specific file
uv run pytest -m file_api                   # by marker
```

Key test files:
- `tests/test_auth_routes.py` — auth endpoints
- `tests/test_api_file.py` — file upload/download
- `tests/test_sandbox_file.py` — sandbox file operations

Config: `backend/pytest.ini` (`asyncio_mode = auto`, markers: `file_api`).

### Sandbox Tests (pytest)

```bash
./dev.sh up -d sandbox
cd sandbox
uv run pytest
```

### Frontend (Vitest)

```bash
cd frontend
npm run test          # Vitest unit tests (src/**/*.spec.ts)
npm run type-check    # vue-tsc type checking
npm run lint          # ESLint (flat config, eslint.config.js)
npm run build         # production build (catches TS + template errors)
```

For manual UI testing: start full dev stack (`./dev.sh up -d`), open `http://localhost:5173`.

### Mockserver

No tests. Verify with:
```bash
curl -X POST http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'
```

### Full-Stack Integration Test

1. `./dev.sh up -d` — start all services
2. Open `http://localhost:5173`
3. Login (or bypass with `AUTH_PROVIDER=none`)
4. Create session, send message — mockserver returns canned tool calls
5. Check logs: `./dev.sh logs -f backend`
6. Check VNC at `localhost:5902` for sandbox desktop

---

## Code Conventions

### Backend (Python)

- **DDD architecture**: `domain/` → `application/` → `infrastructure/` → `interfaces/`
- **FastAPI** with **Pydantic v2** models and settings
- **Beanie** ODM for MongoDB documents (`infrastructure/models/documents.py`)
- **Redis** for caching and message queues
- Dependency management: **uv** + `pyproject.toml` (PEP 621)
- No enforced linter/formatter (no Ruff, Black, or Flake8 configured)
- Async-first: use `async def` for route handlers and service methods

### Frontend (TypeScript / Vue)

- **Vue 3 Composition API** with `<script setup lang="ts">`
- **TypeScript** throughout
- **Tailwind CSS** for styling, **reka-ui** component library
- Path alias: `@/` → `src/`
- **vue-i18n** for internationalization (Chinese + English)
- Dependency management: **npm** + `package.json`
- **ESLint** configured (`npm run lint`); no Prettier
- **Vitest** + @vue/test-utils for unit tests (`npm run test`)

### Sandbox (Python)

- **FastAPI** service exposing shell, file, and supervisor APIs
- Runs inside Docker with **supervisord** managing Chrome, Xvfb, VNC, and the API
- Dependency management: **uv** + `pyproject.toml`

---

## CI/CD

Single GitHub Actions workflow: `.github/workflows/docker-build-and-push.yml`

- **Triggers**: push/PR to `main` and `develop`; tags `v*`
- **Builds**: matrix of `frontend`, `backend`, `sandbox` Docker images for `linux/amd64` and `linux/arm64`
- **Pushes** to Docker Hub on non-PR events (requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets)
- **No** automated test or lint steps in CI

---

## Cursor Cloud Specific Instructions

### Environment Setup

When running in a Cloud Agent environment:

1. Docker may not be available. If Docker commands fail, focus on running individual services or testing code changes without the full stack.
2. For backend work, install dependencies with `cd backend && uv sync`.
3. For frontend work, install dependencies with `cd frontend && npm install`.
4. Set `AUTH_PROVIDER=none` and `API_KEY=test` in `.env` to bypass auth and LLM requirements.

### Testing Strategy by Change Type

| Change Type | Testing Approach |
|---|---|
| Backend Python logic | `cd backend && uv run pytest` (needs running backend + MongoDB + Redis) |
| Backend API routes | `cd backend && uv run pytest` against running server |
| Frontend Vue/TS | `cd frontend && npm run test && npm run type-check && npm run lint && npm run build` |
| Frontend UI changes | Type-check + build + manual GUI testing via `computerUse` subagent |
| Sandbox changes | `cd sandbox && uv run pytest` |
| Config / env changes | Verify with `./dev.sh up -d` and check service logs |
| Documentation / README | No testing needed |

### Debugging the Backend

The dev compose starts the backend with **debugpy** on port `5678`. Attach a remote Python debugger for step-through debugging.

### Resetting State

- Development uses the `ai-manus-dev` Compose project, `manus-network-dev`, and `manus-mongodb-data-dev`, all separate from production. Wipe only that dev project with `./dev.sh down -v`.
- Mockserver tracks response index; restart to reset: `./dev.sh restart mockserver`.

---

## Skills

| Skill File | When to Use |
|---|---|
| `.cursor/skills/starter.md` | Setting up, running, or testing any part of the codebase. Contains detailed API reference, env var tables, and testing workflows. |
| `.cursor/skills/replicate-manus-ui/SKILL.md` | Align frontend UI with manus.im (mine official JS/DOM; Computer / sidebar / Library / Project / Search / chat chrome parity). |
| `.cursor/skills/update-docs/SKILL.md` | Sync compose/env embeds + README demos via `.cursor/skills/update-docs/update_doc.sh` (not docs/demo.md scenarios). |
| `.cursor/skills/demo-videos/SKILL.md` | Recording/uploading README demo MP4s (`tmp/videos` + `gh image` + `docs/demos.yml`; never commit binaries; publish only after user confirmation). |
| `.cursor/skills/release/SKILL.md` | Cutting `vX.Y.Z` GitHub releases (bilingual notes like v2.4.0/v2.5.0; no demo-videos-* releases). |
| `.cursor/skills/debug-claw/SKILL.md` | Debugging OpenClaw / Claw chat, history, uploads, WebSocket, containers. |

Personal (not in repo): `~/.cursor/skills/telegram-screenshots/SKILL.md` — UI screenshots → Telegram Bot MCP, not OpenClaw.
