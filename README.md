# Aming Claw

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

### A Self-Evolving AI Development Tool

> **Want to integrate your project?** Read [`WORKFLOW.md`](WORKFLOW.md) — one file, everything you need.

### Your AI Supervisor

Any Claude Code session can supervise your project through the auto-chain workflow:
- **Observer mode**: Watch tasks flow through Dev → Gate → Tester → QA → Merge → Deploy
- **Submit tasks**: Via API, Telegram, or direct JSON queue
- **Intervene when needed**: `/takeover`, `/pause`, `/cancel` at any stage
- **Zero setup**: Just read WORKFLOW.md and add .aming-claw.yaml to your repo

The Observer pattern means your Claude session is a project supervisor — it submits work, monitors the pipeline, and only intervenes when the auto-chain can't self-heal.

---

Aming Claw is a self-evolving AI development tool that manages its own codebase through a multi-channel task pipeline. Rather than being a passive assistant, Aming Claw operates as an autonomous development team: a product manager writes requirements, a developer codes the solution, a tester validates the output, and a QA auditor reviews quality -- all powered by AI models, all orchestrated through a single Telegram chat.

The core idea is **iterative self-improvement**. Every task submitted through Telegram flows through a multi-stage AI pipeline, producing code changes that are reviewed by a human gatekeeper. Accepted changes are committed and archived. Rejected changes are rolled back, and the feedback loop starts again. Over time, the system improves itself -- updating its own code, fixing its own bugs, and extending its own capabilities.

### Key Capabilities

- **Multi-stage AI pipeline** -- Configurable stage sequences (plan, code, verify) or role-based pipelines (PM, Dev, Test, QA) where each stage builds on accumulated context from prior stages
- **Multiple AI backends** -- Anthropic Claude, OpenAI GPT, and Codex CLI as interchangeable backends with per-stage model binding
- **Human-in-the-loop acceptance gates** -- Every task result requires explicit human approval before changes are committed; optional TOTP-based 2FA for critical operations
- **Git checkpoint and rollback** -- Automatic pre-task checkpointing with full rollback on rejection, ensuring the codebase stays clean
- **Self-update via `/mgr_reinit`** -- The system can pull its own latest code and restart itself, closing the self-evolution loop
- **Workspace management** -- Multiple project workspaces with per-workspace task queuing and parallel dispatch
- **Internationalization (i18n)** -- Full English and Chinese language support throughout the Telegram UI, powered by a lightweight locale system (`agent/i18n.py` with `agent/locales/en.json` and `agent/locales/zh.json`)
- **Interactive Telegram menus** -- Hierarchical button-based UI for task management, system configuration, workspace selection, and security settings
- **Screenshot capabilities** -- Host-level screenshots via the executor gateway for visual verification
- **Automatic timeout and retry** -- Heartbeat monitoring, configurable timeouts, and automatic retries for noop/ack-only responses
- **Workflow governance** -- Standalone service (port 30006) enforcing role-based access control, evidence-driven state transitions, configurable gate policies, and audit logging for multi-agent development workflows

### How Self-Evolution Works

```
  Observer / User submits task (API, Telegram, CLI, or direct queue)
        |
        v
  [ AI Pipeline Executes ]
  PM -> Dev -> Test -> QA   (or plan -> code -> verify)
        |
        v
  [ Human Review ]
  Accept: changes committed, task archived
  Reject: git rollback, feedback incorporated, cycle restarts
        |
        v
  [ /mgr_reinit ]
  System pulls its own updates and restarts
        |
        v
  Aming Claw is now running its own improved code
```

The acceptance/rejection loop is the engine of self-evolution. Each rejected task carries feedback that informs the next attempt. Each accepted task permanently advances the codebase. The system can modify any file in its own repository -- including its pipeline logic, its backend integrations, its input channels (API, Telegram, CLI), and its observer interface.

---

### Architecture (v7 — Executor-Driven Auto-Chain)

```
Observer / User  (API · Telegram · CLI · direct queue)
     │ task / message
     ▼
Coordinator ──► PM (requirements) ──► Dev (worktree)
  (dispatcher)      │                      │
                    _verification       code changes
                    config                 │
                                           ▼
                              Checkpoint Gatekeeper (isolated, ~10s)
                                           │ pass
                                           ▼
                              Tester (1200+ unit tests, ~70s)
                                           │ pass
                                           ▼
                              QA (_verification honored)
                                           │ pass
                                           ▼
                              Merge (rebase onto main)
                                           │
                                           ▼
                              Deploy Chain (auto-detect + restart)
                                ├─ executor restart (signal file)
                                ├─ governance Docker rebuild
                                ├─ gateway Docker restart
                                └─ smoke test (all services)
```

**Key components:**

- **Executor** (`executor.py`): Central orchestrator. Picks up tasks, spawns AI
  sessions in git worktrees, runs auto-chain (gate → test → QA → merge → deploy).
- **Coordinator** (`coordinator.py`): Pure dispatcher. Routes Telegram messages
  to PM, creates dev_tasks. Never writes code.
- **Parallel Dispatcher** (`parallel_dispatcher.py`): Worker-pool model. Each
  workspace (toolBoxClient, aming_claw) gets its own thread. Sequential within
  workspace, parallel across workspaces.
- **Workspace Registry** (`workspace_registry.py`): Routes tasks by normalized
  `project_id` (e.g., `amingClaw` → `aming-claw` → aming_claw workspace).
- **Deploy Chain** (`deploy_chain.py`): Post-merge auto-deploy. Maps changed
  files to affected services, rebuilds Docker containers, runs smoke tests.
- **Service Manager** (`service_manager.py`): Watches executor process.
  Restarts on crash or signal file from deploy chain.
- **Redis Stream Audit**: Full round-trip AI prompt + result stored in Redis
  streams (`ai:prompt:{session_id}`) for debugging and replay.

**Shared storage:** `shared-volume/codex-tasks/`
  `pending/` → `processing/` → `results/` → `archive/` (task lifecycle)
  `state/` — workspace_registry.json, dispatcher state, manager signals
  `logs/` — per-task timeline.jsonl, pipeline_audit.jsonl

---

### Task Lifecycle

```
Observer / User submits task (API, Telegram, CLI, or direct queue)
       |
       v
  [ pending ]  -------- task file written to pending/
       |
       v  Executor picks up task
  [ processing ]  -----  heartbeat updated every 30s
       |
       +- AI returns ACK-only? --> retry (TASK_NOOP_RETRIES) --> [ failed ]
       |
       v  AI produces output
  [ pending_acceptance ]  ---  Telegram notification with Accept/Reject buttons
       |
       +---- User rejects (+ OTP if 2FA on) -------> [ rejected ]
       |                                                   |
       |                                       git rollback to checkpoint
       |                                       (stays in results/, iterable)
       |
       +---- User accepts (+ OTP if 2FA on) --> [ accepted ] --> [ archived ]
                                                     |
                                             git commit of changes
```

#### Status fields in `state/task_state/{task_id}/status.json`

| Field | Description |
|---|---|
| `task_id` | Unique ID (`task-<ts>-<hex>`) |
| `task_code` | Short human-readable alias (e.g. `AB1`) |
| `status` | Current state (see lifecycle above) |
| `started_at` | When executor began processing |
| `ended_at` | When task reached terminal state |
| `progress` | 0-100 progress hint from executor |
| `worker_id` | Hostname of executor that ran the task |
| `attempt` | Number of execution attempts |
| `heartbeat_at` | Last heartbeat timestamp (updated every 30s) |
| `completion_notified_at` | When Telegram notification was sent |

---

### Roles & Auto-Chain

Aming Claw uses a role-based auto-chain where each role is an isolated AI
session with specific permissions. Tasks flow automatically through the chain.

#### Roles

| Role | Responsibility | Tools |
|---|---|---|
| **Coordinator** | Dispatch only. Routes messages to PM, creates dev_tasks. Never writes code. | Read-only |
| **PM** | Analyze requirements, output PRD with target_files, acceptance_criteria, `_verification` config. | Read-only |
| **Dev** | Implement code in isolated git worktree. | Read, Write, Edit, Bash, Glob, Grep |
| **Checkpoint Gatekeeper** | Fast isolated check (~10s): files changed? syntax valid? no unrelated changes? | Diff only |
| **Tester** | Run unit tests (1200+). Pass/fail decides chain continuation. | Bash (pytest) |
| **QA** | Verify in real environment. Honors `_verification` config (skip governance when not needed). | Read, Bash |
| **Observer** | Human or automated watcher. Can `/takeover`, `/pause`, `/cancel`. | All |

#### Auto-Chain Flow

```
Message → Coordinator → PM → Dev → Gate → Tester → QA → Merge → Deploy
                                     ↑ fail: retry Dev (max 3)
                                              ↑ fail: stop chain, notify
```

- **`_verification` config**: PM decides what QA checks to run. Flows through
  entire chain. `governance_nodes: false` skips governance checks for simple tasks.
- **Idempotent triggers**: Each stage uses `parent_task_id:stage` as idempotency
  key. Safe to retry without duplicate tasks.
- **Deploy chain**: After merge, auto-detects affected services and restarts
  (executor signal, governance Docker rebuild, gateway restart).

---

### Quick Start

```powershell
# 1. Download Python 3.12 + install all dependencies (one-time)
.\setup.ps1

# 2. Configure your environment
copy .env.example .env
notepad .env          # fill in TELEGRAM_BOT_TOKEN_CODEX and EXECUTOR_API_TOKEN

# 3. Launch all services
.\start.ps1
```

> **Requirements:** Windows 10/11. No Python installation needed -- `setup.ps1` downloads an embedded Python 3.12 runtime into `runtime/python/`.

### Register a New Project

Any project can use the auto-chain by adding a `.aming-claw.yaml` config:

```bash
# 1. Create .aming-claw.yaml in your project root (see docs/ai-agent-integration-guide.md)
# 2. Register via API
curl -X POST http://localhost:40000/api/projects/register \
  -H "Content-Type: application/json" \
  -d '{"workspace_path": "/path/to/your/project"}'
# 3. Submit tasks
curl -X POST http://localhost:40100/coordinator/chat \
  -d '{"message":"Fix the bug","project_id":"your-project-id"}'
```

See [AI Agent Integration Guide](docs/ai-agent-integration-guide.md) for full config format.

---

### Two-Factor Authentication (2FA)

Aming Claw uses TOTP (RFC 6238) to protect irreversible operations. Task acceptance is a destructive action -- once accepted, the task is permanently archived and changes committed. Enabling 2FA ensures no accidental clicks can commit that action.

#### Setup

1. In Telegram, send `/auth_init` to the bot.
2. The bot replies with a base32 secret and an `otpauth://` URI. Scan it with any authenticator app (Google Authenticator, Authy, 1Password, etc.).
3. Enable strict acceptance in `.env`:
   ```
   TASK_STRICT_ACCEPTANCE=1
   ```
4. Restart services: `.\start.ps1 -Restart`

> **Note:** 2FA for acceptance is only enforced when **both** `TASK_STRICT_ACCEPTANCE=1` **and** the authenticator has been initialized via `/auth_init`. If either condition is missing, acceptance works without OTP.

#### TOTP Settings

| Variable | Default | Description |
|---|---|---|
| `AUTH_OTP_WINDOW` | `2` | Number of periods to accept on either side of current time |
| `AUTH_ALLOW_30_FALLBACK` | `1` | Also try 30-second TOTP if 60-second fails |
| `AUTH_AUTO_INIT` | `0` | Auto-initialize if no seed exists (not recommended for production) |

---

### Task Acceptance Flow

#### Without 2FA (`TASK_STRICT_ACCEPTANCE=0`)

```
Task completes
    |
    v
Telegram: "Task [AB1] complete. Awaiting acceptance."
          [Accept]  [Reject]  [Status]  [Events]
    |
    +-- Click [Accept]  ->  Task immediately accepted, changes committed, archived
    +-- Click [Reject]  ->  Git rollback to checkpoint; bot prompts: /reject AB1 <reason>
```

#### With 2FA enabled (`TASK_STRICT_ACCEPTANCE=1` + `/auth_init` done)

```
Task completes
    |
    v
Telegram: "Task [AB1] complete. Awaiting acceptance."
          [Accept]  [Reject]  [Status]  [Events]
    |
    +-- Click [Accept]
    |       |
    |       v
    |   Bot: "2FA required to accept task [AB1].
    |         Send: /accept AB1 <6-digit OTP>"
    |       |
    |       +-- OTP valid   ->  Task accepted, changes committed, archived
    |       +-- OTP invalid ->  "2FA failed: OTP invalid or expired."
    |
    +-- Click [Reject]
            |
            v
        Bot: "2FA required to reject task [AB1].
              Send: /reject AB1 <6-digit OTP> [reason]"
            |
            +-- OTP valid   ->  Git rollback; task stays in results/ for iteration
            +-- OTP invalid ->  "2FA failed: OTP invalid or expired."
```

---

### Command Reference

#### Task Commands

| Command | Description |
|---|---|
| `<any text>` | Create and queue a new task |
| `/status` | List all active tasks (pending + in-progress + awaiting acceptance) |
| `/status <ref>` | Show detailed status for a task by ID or short code |
| `/accept <ref> [OTP]` | Accept task result and archive it (OTP required if 2FA enabled) |
| `/reject <ref> [OTP] [reason]` | Reject task result with optional feedback (OTP required if 2FA enabled) |
| `/events <ref>` | Show recent events for a task |

#### Archive Commands

| Command | Description |
|---|---|
| `/archive` | List recent archived tasks |
| `/archive_show <archive_id>` | Show details of an archived task |
| `/archive_search <keyword>` | Search archive by keyword |

#### 2FA Commands

| Command | Description |
|---|---|
| `/auth_init` | Initialize TOTP authenticator (generates secret + QR URI) |
| `/auth_status` | Show current 2FA configuration |
| `/auth_debug <OTP>` | Debug OTP verification (ops-only) |

#### Management Commands

| Command | Description |
|---|---|
| `/mgr_reinit <OTP>` | Git pull + restart all services (self-update) |
| `/mgr_restart <OTP>` | Restart all services |
| `/mgr_status` | Show service manager status |

#### Ops Commands (require OTP + whitelist)

| Command | Description |
|---|---|
| `/ops_restart <OTP>` | Restart all services |
| `/ops_set_workspace <path\|default> <OTP>` | Switch working directory |
| `/ops_set_workspace_pick <n> <OTP>` | Pick from candidate workspaces |

#### Interactive Menu

Send `/menu` to open the interactive button-based dashboard with sub-menus for:
- Task management (create, status, accept/reject)
- System configuration (backend, pipeline, model selection)
- Workspace management (add/remove/default/queue status)
- Security settings (2FA setup, auth status)
- Ops controls (restart, reinit)

---

### Configuration Reference

Copy `.env.example` to `.env` and fill in the required values.

#### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN_CODEX` | Telegram bot token (dedicated bot recommended) |
| `EXECUTOR_API_TOKEN` | Shared secret for executor-gateway authentication |

#### Execution

| Variable | Default | Description |
|---|---|---|
| `CODEX_BIN` | `codex.cmd` | Codex CLI binary name |
| `CODEX_WORKSPACE` | cwd | Directory the AI operates in |
| `CODEX_TIMEOUT_SEC` | `900` | Max seconds for a single execution run |
| `CODEX_TIMEOUT_RETRIES` | `1` | Retry count on timeout |
| `CODEX_MODEL` | | Override model (leave blank for default) |
| `CODEX_DANGEROUS` | `1` | Pass `--dangerously-auto-approve` to Codex CLI |

#### AI Backend & Pipeline

| Variable | Default | Description |
|---|---|---|
| `AGENT_BACKEND` | `pipeline` | Active backend: `codex`, `claude`, or `pipeline` |
| `PIPELINE_DEFAULT_PROVIDER` | `anthropic` | Default provider for pipeline stages |
| `PIPELINE_ROLE_PM_MODEL` | | Override model for PM role |
| `PIPELINE_ROLE_DEV_MODEL` | | Override model for Dev role |
| `PIPELINE_ROLE_TEST_MODEL` | | Override model for Test role |
| `PIPELINE_ROLE_QA_MODEL` | | Override model for QA role |

#### Acceptance & 2FA

| Variable | Default | Description |
|---|---|---|
| `TASK_STRICT_ACCEPTANCE` | `1` | Require explicit `/accept` before archiving |
| `AUTH_OTP_WINDOW` | `2` | OTP time window tolerance (periods) |
| `AUTH_ALLOW_30_FALLBACK` | `1` | Try 30s TOTP period as fallback |
| `AUTH_AUTO_INIT` | `0` | Auto-initialize 2FA seed on first run |

#### Timeouts & Polling

| Variable | Default | Description |
|---|---|---|
| `TASK_TIMEOUT_SEC` | `1800` | Seconds before a stuck task is marked `timeout` |
| `EXECUTOR_HEARTBEAT_SEC` | `30` | Heartbeat interval in seconds |
| `TASK_NOOP_RETRIES` | `1` | Retries when AI returns acknowledgement-only output |
| `COORDINATOR_POLL_INTERVAL_SEC` | `1` | Telegram update polling interval |
| `EXECUTOR_POLL_SEC` | `1` | Task queue polling interval |

#### Storage Paths

| Variable | Default | Description |
|---|---|---|
| `SHARED_VOLUME_PATH` | `<cwd>/shared-volume` | Root of task storage |
| `WORKSPACE_PATH` | cwd | Host workspace path for executor-gateway |
| `EXECUTOR_BASE_URL` | `http://127.0.0.1:8090` | Gateway URL used by coordinator |

#### Internationalization

| Variable | Default | Description |
|---|---|---|
| `LANGUAGE` | `zh` | UI language: `zh` (Chinese) or `en` (English) |

---

### Workflow Governance Service

A standalone service that enforces development workflow rules through APIs, preventing AI Agents from bypassing verification steps, assuming wrong roles, or submitting unchecked state changes.

**Core principle:** Rules live in code, enforced by APIs — not by prompts that AI can ignore.

#### Three-Layer Architecture

| Layer | Storage | Content | Mutability |
|-------|---------|---------|------------|
| Graph Definition | JSON + NetworkX | Node definitions, deps edges, gate policies | Read-mostly |
| Runtime State | SQLite (governance.db) | Node status, sessions, tasks, versions | High-frequency |
| Event Log | JSONL + SQLite index | Who changed what, when, with what evidence | Append-only |

#### Key Features

- **Role-based access control** — Agents register with a Principal + Session model, receive tokens, and can only perform transitions allowed for their role
- **Configurable gate policies** — Gates support `min_status`, `release_only`, and `waivable` policies instead of hardcoded "must be pass"
- **Structured evidence** — State transitions require typed evidence objects (test_report, e2e_report, error_log, commit_ref) with checksums and artifact URIs
- **Idempotency** — All write APIs accept `Idempotency-Key` headers for safe retries
- **Project isolation** — Each project gets its own database, graph, and audit trail
- **Impact analysis** — Given file changes, computes affected nodes, minimum verification path, and recommended test files
- **Snapshot & rollback** — Version-based snapshots with full state rollback
- **Redis caching** — Dual-write (SQLite truth + Redis cache) with automatic degradation when Redis is unavailable
- **Event bus** — Internal pub/sub for `node.status_changed`, `gate.satisfied`, `role.expired`, etc.

#### Verify Status Flow

```
  PENDING → TESTING → T2_PASS → QA_PASS
     ↑         ↓          ↓         ↓
     └── FAILED ←──────────┘─────────┘

  Special: PENDING → WAIVED (coordinator only)
  Forbidden: PENDING → QA_PASS (cannot skip T2)
```

#### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/bootstrap` | One-shot: register project + import graph + register coordinator |
| POST | `/api/role/register` | Register agent session (principal + project + role) |
| POST | `/api/role/heartbeat` | Session heartbeat |
| POST | `/api/wf/{pid}/verify-update` | Update node verify status (auth + evidence + gate check) |
| POST | `/api/wf/{pid}/release-gate` | Release gate (scope-aware, profile support) |
| GET | `/api/wf/{pid}/summary` | Status summary by category |
| GET | `/api/wf/{pid}/node/{nid}` | Single node state + definition |
| GET | `/api/wf/{pid}/impact?files=...` | File change impact analysis |
| GET | `/api/wf/{pid}/export?format=mermaid` | Export as Mermaid/JSON/Markdown |
| POST | `/api/wf/{pid}/rollback` | Rollback to snapshot version |
| POST | `/api/mem/{pid}/write` | Write structured memory |
| GET | `/api/mem/{pid}/query` | Query memories by module/kind/node |
| GET | `/api/audit/{pid}/log` | Query audit log |
| GET | `/api/audit/{pid}/violations` | Query failed operations |

#### Docker Deployment

```bash
# Start governance service + Redis
docker compose -f docker-compose.governance.yml up -d

# Bootstrap a project
curl -X POST http://localhost:30006/api/bootstrap \
  -d '{"project_id":"my-project","graph_source":"/workspace/acceptance-graph.md",
       "coordinator":{"principal_id":"coord","admin_secret":"..."}}'
```

#### Governance Configuration

| Variable | Default | Description |
|---|---|---|
| `GOVERNANCE_PORT` | `30006` | HTTP server port |
| `GOVERNANCE_ADMIN_SECRET` | | Required for coordinator registration |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `GOVERNANCE_SESSION_TTL_HOURS` | `24` | Session token lifetime |
| `GOVERNANCE_HEARTBEAT_INTERVAL_SEC` | `60` | Expected heartbeat interval |
| `GOVERNANCE_STALE_TIMEOUT_SEC` | `180` | Seconds before session marked stale |
| `GOVERNANCE_EXPIRE_TIMEOUT_SEC` | `600` | Seconds before session expires |

> Full architecture details: [`docs/workflow-governance-architecture-v2.md`](docs/workflow-governance-architecture-v2.md)

---

### File Structure

```
aming_claw/
+-- setup.ps1                  # One-time: download Python + install deps
+-- start.ps1                  # Launch all services
+-- init.ps1                   # Initialize environment
+-- .env.example               # Configuration template
+-- Dockerfile.governance      # Docker image for governance service
+-- docker-compose.governance.yml  # Governance + Redis deployment
+-- agent/
|   +-- coordinator.py         # Telegram bot + task lifecycle management
|   +-- executor.py            # Task runner (invokes AI backends)
|   +-- bot_commands.py        # Command handlers and routing
|   +-- config.py              # Runtime backend/pipeline configuration
|   +-- task_state.py          # State machine, events, heartbeat
|   +-- task_accept.py         # Post-acceptance test runner
|   +-- task_retry.py          # Retry logic for failed tasks
|   +-- backends.py            # AI backend integrations (Claude, Codex, OpenAI)
|   +-- deploy_chain.py        # Post-merge auto-deploy orchestration
|   +-- model_registry.py      # Fetch available models from Anthropic/OpenAI APIs
|   +-- auth.py                # TOTP-based 2FA implementation
|   +-- workspace.py           # Workspace resolution and switching
|   +-- workspace_registry.py  # Multi-workspace registration
|   +-- workspace_queue.py     # Per-workspace FIFO task queue
|   +-- parallel_dispatcher.py # Parallel task dispatch across workspaces
|   +-- git_rollback.py        # Git checkpoint, rollback, and commit
|   +-- service_manager.py     # Process monitoring, reinit, restart
|   +-- interactive_menu.py    # Telegram button-based UI
|   +-- i18n.py                # Internationalization engine
|   +-- utils.py               # Shared utilities (atomic JSON, Telegram API)
|   +-- governance/            # Workflow Governance Service (port 30006)
|   |   +-- __init__.py        # Package marker
|   |   +-- server.py          # HTTP server + routing + middleware
|   |   +-- enums.py           # VerifyStatus, VerifyLevel, Role enums
|   |   +-- errors.py          # Unified exception hierarchy (14 error types)
|   |   +-- db.py              # SQLite schema (7 tables) + WAL + migrations
|   |   +-- models.py          # Evidence, GateRequirement, NodeDef dataclasses
|   |   +-- permissions.py     # State machine + scope checking
|   |   +-- evidence.py        # Structured evidence validation
|   |   +-- gate_policy.py     # Configurable gate strategy engine
|   |   +-- graph.py           # NetworkX DAG + markdown import + Mermaid export
|   |   +-- state_service.py   # verify_update, release_gate, snapshot, rollback
|   |   +-- role_service.py    # Principal + Session + heartbeat + token auth
|   |   +-- impact_analyzer.py # Policy-based file change impact analysis
|   |   +-- project_service.py # Project registration + isolation + bootstrap
|   |   +-- memory_service.py  # Structured dev knowledge base
|   |   +-- audit_service.py   # JSONL append-only + SQLite query index
|   |   +-- event_bus.py       # Internal event subscription
|   |   +-- idempotency.py     # Idempotency key management
|   |   +-- redis_client.py    # Redis client with SQLite fallback
|   |   +-- client.py          # GovernanceClient SDK (retry + degradation)
|   +-- locales/
|   |   +-- zh.json            # Chinese translations
|   |   +-- en.json            # English translations
|   +-- tests/                 # 1000+ test cases covering all modules
|   +-- requirements.txt
+-- docs/
|   +-- workflow-governance-design.md          # Original governance design doc
|   +-- workflow-governance-architecture-v2.md # Full architecture spec (v2)
+-- executor-gateway/
|   +-- app/main.py            # FastAPI service for screenshots & file ops
|   +-- requirements.txt
+-- scripts/
|   +-- _get_python.ps1        # Returns bundled python.exe path
|   +-- start-coordinator.ps1
|   +-- start-executor.ps1
|   +-- start-gateway.ps1
|   +-- start-manager.ps1
|   +-- restart-all.ps1
|   +-- restart-agent.ps1
|   +-- restart-from-telegram.ps1
|   +-- reload-after-executor-change.ps1
+-- runtime/
    +-- python/                # Downloaded by setup.ps1, excluded from git
```

---

### Troubleshooting

**Bot does not respond to messages**
- Check `TELEGRAM_BOT_TOKEN_CODEX` is set correctly in `.env`
- Ensure the coordinator service is running (check the coordinator terminal window)
- Verify the service manager is running: `.\scripts\start-manager.ps1`

**Tasks stuck in `processing`**
- The coordinator auto-marks tasks as `timeout` after `TASK_TIMEOUT_SEC` seconds without a heartbeat
- Check executor terminal for errors; verify `CODEX_BIN` is accessible
- Try increasing `CODEX_TIMEOUT_SEC` for long-running tasks

**"2FA failed: OTP invalid or expired"**
- Ensure your device clock is synchronized (NTP)
- Increase `AUTH_OTP_WINDOW` (e.g. `AUTH_OTP_WINDOW=3`) to tolerate clock skew
- Run `/auth_debug <OTP>` to see detailed verification info

**AI returns acknowledgement without acting**
- This is a model behavior issue. The pipeline detects ACK-only messages and retries once (`TASK_NOOP_RETRIES=1`)
- If it persists, the task is marked `failed`; retry by sending the task again
- Consider switching to a different model via the interactive menu

**Git rollback fails on rejection**
- Ensure the workspace is a valid git repository with at least one commit
- Check that the pre-task checkpoint was created (look for `[aming-claw checkpoint]` commits)

**Language not switching**
- Set `LANGUAGE=en` or `LANGUAGE=zh` in `.env` and restart services
- Or use the interactive menu to switch language at runtime

---

---

<a name="中文"></a>
## 中文

### 自我迭代的AI开发工具

### 你的 AI 督导

任何 Claude Code session 都可以通过自动链工作流对你的项目进行督导：
- **Observer 模式**：观察任务在 Dev → Gate → Tester → QA → Merge → Deploy 中流转
- **提交任务**：通过 API、Telegram 或直接写入 JSON 队列
- **随时介入**：在任意阶段使用 `/takeover`、`/pause`、`/cancel`
- **零配置**：只需读取 WORKFLOW.md 并在你的仓库中添加 .aming-claw.yaml

Observer 模式意味着你的 Claude session 是项目督导——它提交工作、监控流水线，只在自动链无法自我修复时才介入。

---

Aming Claw 是一个自我迭代的AI开发工具，通过多渠道任务流水线管理自己的代码库。它不是一个被动的助手，而是一个自主运作的开发团队：产品经理撰写需求、开发者编写代码、测试者验证输出、QA审计质量——全部由AI模型驱动，全部通过统一的任务管道进行协调。

核心理念是**迭代式自我进化**。每个提交的任务都会经过多阶段AI流水线，产生代码变更，由人类把关者审查。接受的变更被提交并归档。拒绝的变更被回滚，反馈循环重新开始。随着时间推移，系统不断改进自身——更新自己的代码，修复自己的缺陷，扩展自己的能力。

### 核心能力

- **多阶段AI流水线** -- 可配置的阶段序列（计划、编码、验证）或基于角色的流水线（PM、Dev、Test、QA），每个阶段基于前序阶段的累积上下文构建
- **多AI后端** -- Anthropic Claude、OpenAI GPT 和 Codex CLI 作为可互换的后端，支持每阶段独立绑定模型
- **人在回路的验收门** -- 每个任务结果都需要明确的人工批准才能提交变更；关键操作可选 TOTP 双因素认证
- **Git 检查点与回滚** -- 任务执行前自动创建检查点，拒绝时完全回滚，确保代码库始终干净
- **通过 `/mgr_reinit` 自我更新** -- 系统可以拉取自己的最新代码并重启，形成自我进化闭环
- **工作区管理** -- 多项目工作区，支持每工作区任务队列和并行调度
- **国际化 (i18n)** -- 完整的中英文界面支持，由轻量级多语言系统驱动（`agent/i18n.py` 配合 `agent/locales/en.json` 和 `agent/locales/zh.json`）
- **交互式 Telegram 菜单** -- 层级化按钮界面，用于任务管理、系统配置、工作区选择和安全设置
- **截图能力** -- 通过执行器网关在宿主机上截屏，用于可视化验证
- **自动超时与重试** -- 心跳监控、可配置超时时间、对空回复/确认式响应自动重试
- **工作流治理** -- 独立服务（端口 30006），强制执行角色访问控制、证据驱动的状态转换、可配置 gate 策略和审计日志，用于多 Agent 协作开发工作流

### 自我进化机制

```
  Observer / 用户提交任务（API、Telegram、CLI 或直接队列）
        |
        v
  [ AI 流水线执行 ]
  PM -> Dev -> Test -> QA   (或 plan -> code -> verify)
        |
        v
  [ 人工审查 ]
  接受: 变更提交, 任务归档
  拒绝: git 回滚, 纳入反馈, 循环重启
        |
        v
  [ /mgr_reinit ]
  系统拉取自身更新并重启
        |
        v
  Aming Claw 已在运行自己改进后的代码
```

验收/拒绝循环是自我进化的引擎。每次拒绝都附带反馈信息，为下一次尝试提供改进方向。每次接受都永久地推进代码库。系统可以修改自身仓库中的任何文件——包括流水线逻辑、后端集成、输入渠道（API、Telegram、CLI）以及 Observer 接口。

---

### 架构

```
Observer / 用户  (API · Telegram · CLI · 直接队列)
     | 提交任务 / 发送消息
     v
+-----------------+
|   协调器        |  coordinator.py -- 多渠道输入、命令处理、
|  Coordinator    |  验收门控、超时检测、归档
+--------+--------+
         | 将任务文件写入 shared-volume/codex-tasks/pending/
         v
+-----------------+
|   执行器        |  executor.py -- 提取待处理任务、调用AI后端、
|   Executor      |  流式输出、写入结果、更新心跳
+--------+--------+
         | 可选: 通过 HTTP 截图/文件操作
         v
+-----------------+
| 执行器网关      |  executor-gateway/ (FastAPI) -- 截图与文件操作的
| Executor Gateway|  REST API，端口 8090
+-----------------+

服务管理器 (service_manager.py):
  - 监控协调器 + 执行器进程（崩溃自动重启）
  - 处理 /mgr_reinit (git pull + 重启) 和 /mgr_restart 信号
  - 将实时状态写入 state/manager_status.json

并行调度器 (parallel_dispatcher.py):
  - 工作池模型：每个注册的工作区拥有独立的工作线程
  - 任务根据显式指定或默认分配路由到工作区
  - 工作区内顺序执行，跨工作区并行执行

共享存储: shared-volume/codex-tasks/
  pending/      -- 任务队列（JSON 文件）
  processing/   -- 正在执行的任务
  results/      -- 已完成待验收的任务
  archive/      -- 已接受的任务（永久存储）
  state/        -- 每个任务的 status.json、events.jsonl
  logs/         -- 运行日志
  acceptance/   -- 验收文档和测试用例
```

---

### 任务生命周期

```
Observer / 用户提交任务（API、Telegram、CLI 或直接队列）
       |
       v
  [ pending 待处理 ]  -------- 任务文件写入 pending/
       |
       v  执行器提取任务
  [ processing 处理中 ]  -----  每 30 秒更新心跳
       |
       +- AI 返回仅确认? --> 重试 (TASK_NOOP_RETRIES) --> [ failed 失败 ]
       |
       v  AI 产生输出
  [ pending_acceptance 待验收 ]  ---  Telegram 通知，附带接受/拒绝按钮
       |
       +---- 用户拒绝 (+ OTP 若启用 2FA) -------> [ rejected 已拒绝 ]
       |                                                   |
       |                                         git 回滚到检查点
       |                                         (保留在 results/，可迭代)
       |
       +---- 用户接受 (+ OTP 若启用 2FA) --> [ accepted 已接受 ] --> [ archived 已归档 ]
                                                     |
                                               git 提交变更
```

#### `state/task_state/{task_id}/status.json` 中的状态字段

| 字段 | 说明 |
|---|---|
| `task_id` | 唯一ID (`task-<ts>-<hex>`) |
| `task_code` | 简短可读别名 (如 `AB1`) |
| `status` | 当前状态 (见上方生命周期) |
| `started_at` | 执行器开始处理的时间 |
| `ended_at` | 任务到达终态的时间 |
| `progress` | 0-100 进度提示 |
| `worker_id` | 执行任务的执行器主机名 |
| `attempt` | 执行尝试次数 |
| `heartbeat_at` | 最近一次心跳时间戳 (每 30 秒更新) |
| `completion_notified_at` | Telegram 通知发送时间 |

---

### 角色与自动链 (Roles & Auto-Chain)

Aming Claw 使用角色隔离的自动链，每个角色是独立的 AI session。

| 角色 | 职责 | 工具权限 |
|---|---|---|
| **Coordinator** | 纯调度：路由消息到 PM，创建 dev_task | 只读 |
| **PM** | 需求分析，输出 PRD + target_files + `_verification` 配置 | 只读 |
| **Dev** | 在 git worktree 中实现代码 | Read, Write, Edit, Bash |
| **Checkpoint Gatekeeper** | 快速隔离检查（~10s）：文件是否变更？语法？ | Diff only |
| **Tester** | 运行单元测试（1200+） | Bash (pytest) |
| **QA** | 真实环境验证，遵循 `_verification` 配置 | Read, Bash |
| **Observer** | 人工或自动监控，可 /takeover, /pause, /cancel | 全部 |

自动链流程：`消息 → Coordinator → PM → Dev → Gate → Tester → QA → Merge → Deploy`

---

### 快速开始

```powershell
# 1. 下载 Python 3.12 + 安装所有依赖（首次运行）
.\setup.ps1

# 2. 配置环境
copy .env.example .env
notepad .env          # 填入 TELEGRAM_BOT_TOKEN_CODEX 和 EXECUTOR_API_TOKEN

# 3. 启动所有服务
.\start.ps1
```

> **系统要求：** Windows 10/11。无需预装 Python —— `setup.ps1` 会自动下载嵌入式 Python 3.12 运行时到 `runtime/python/`。

---

### 双因素认证 (2FA)

Aming Claw 使用 TOTP (RFC 6238) 保护不可逆操作。任务接受是破坏性操作——一旦接受，任务将永久归档且变更被提交。启用 2FA 可防止误操作。

#### 设置步骤

1. 在 Telegram 中向机器人发送 `/auth_init`。
2. 机器人会回复 base32 密钥和 `otpauth://` URI。用任何认证器应用扫描（Google Authenticator、Authy、1Password 等）。
3. 在 `.env` 中启用严格验收：
   ```
   TASK_STRICT_ACCEPTANCE=1
   ```
4. 重启服务：`.\start.ps1 -Restart`

> **注意：** 只有当 `TASK_STRICT_ACCEPTANCE=1` **且** 已通过 `/auth_init` 初始化认证器时，验收才会要求 OTP。如果缺少任一条件，验收将不需要 OTP。

#### TOTP 设置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTH_OTP_WINDOW` | `2` | 当前时间两侧接受的时间窗口数 |
| `AUTH_ALLOW_30_FALLBACK` | `1` | 60秒周期失败时回退到30秒 TOTP |
| `AUTH_AUTO_INIT` | `0` | 首次运行时自动初始化 2FA 种子（不建议在生产环境使用） |

---

### 命令参考

#### 任务命令

| 命令 | 说明 |
|---|---|
| `<任意文本>` | 创建并排队新任务 |
| `/status` | 列出所有活跃任务（待处理 + 进行中 + 待验收） |
| `/status <ref>` | 按 ID 或短码显示任务详细状态 |
| `/accept <ref> [OTP]` | 接受任务结果并归档（启用 2FA 时需要 OTP） |
| `/reject <ref> [OTP] [原因]` | 拒绝任务结果，附带可选反馈（启用 2FA 时需要 OTP） |
| `/events <ref>` | 显示任务的最近事件 |

#### 归档命令

| 命令 | 说明 |
|---|---|
| `/archive` | 列出最近归档的任务 |
| `/archive_show <archive_id>` | 显示归档任务详情 |
| `/archive_search <关键词>` | 按关键词搜索归档 |

#### 2FA 命令

| 命令 | 说明 |
|---|---|
| `/auth_init` | 初始化 TOTP 认证器（生成密钥 + QR URI） |
| `/auth_status` | 显示当前 2FA 配置状态 |
| `/auth_debug <OTP>` | 调试 OTP 验证（仅运维） |

#### 管理命令

| 命令 | 说明 |
|---|---|
| `/mgr_reinit <OTP>` | Git pull + 重启所有服务（自我更新） |
| `/mgr_restart <OTP>` | 重启所有服务 |
| `/mgr_status` | 显示服务管理器状态 |

#### 运维命令（需要 OTP + 白名单）

| 命令 | 说明 |
|---|---|
| `/ops_restart <OTP>` | 重启所有服务 |
| `/ops_set_workspace <路径\|default> <OTP>` | 切换工作目录 |
| `/ops_set_workspace_pick <n> <OTP>` | 从候选工作区中选择 |

#### 交互式菜单

发送 `/menu` 打开交互式按钮仪表板，包含以下子菜单：
- 任务管理（创建、状态、接受/拒绝）
- 系统配置（后端、流水线、模型选择）
- 工作区管理（添加/删除/默认/队列状态）
- 安全设置（2FA 设置、认证状态）
- 运维控制（重启、重新初始化）

---

### 配置参考

将 `.env.example` 复制为 `.env` 并填入必填值。

#### 必填项

| 变量 | 说明 |
|---|---|
| `TELEGRAM_BOT_TOKEN_CODEX` | Telegram 机器人令牌（建议使用专用机器人） |
| `EXECUTOR_API_TOKEN` | 执行器网关认证的共享密钥 |

#### 执行配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CODEX_BIN` | `codex.cmd` | Codex CLI 可执行文件名 |
| `CODEX_WORKSPACE` | 当前目录 | AI 操作的目录 |
| `CODEX_TIMEOUT_SEC` | `900` | 单次执行运行的最大秒数 |
| `CODEX_TIMEOUT_RETRIES` | `1` | 超时重试次数 |
| `CODEX_MODEL` | | 覆盖模型（留空使用默认值） |
| `CODEX_DANGEROUS` | `1` | 向 Codex CLI 传递 `--dangerously-auto-approve` |

#### AI 后端与流水线

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_BACKEND` | `pipeline` | 活跃后端：`codex`、`claude` 或 `pipeline` |
| `PIPELINE_DEFAULT_PROVIDER` | `anthropic` | 流水线阶段的默认供应商 |
| `PIPELINE_ROLE_PM_MODEL` | | 覆盖 PM 角色的模型 |
| `PIPELINE_ROLE_DEV_MODEL` | | 覆盖 Dev 角色的模型 |
| `PIPELINE_ROLE_TEST_MODEL` | | 覆盖 Test 角色的模型 |
| `PIPELINE_ROLE_QA_MODEL` | | 覆盖 QA 角色的模型 |

#### 验收与 2FA

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TASK_STRICT_ACCEPTANCE` | `1` | 归档前需要明确 `/accept` |
| `AUTH_OTP_WINDOW` | `2` | OTP 时间窗口容差（周期数） |
| `AUTH_ALLOW_30_FALLBACK` | `1` | 回退尝试 30 秒 TOTP 周期 |
| `AUTH_AUTO_INIT` | `0` | 首次运行时自动初始化 2FA 种子 |

#### 超时与轮询

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TASK_TIMEOUT_SEC` | `1800` | 卡住的任务标记为 `timeout` 的秒数 |
| `EXECUTOR_HEARTBEAT_SEC` | `30` | 心跳间隔秒数 |
| `TASK_NOOP_RETRIES` | `1` | AI 返回仅确认输出时的重试次数 |
| `COORDINATOR_POLL_INTERVAL_SEC` | `1` | Telegram 更新轮询间隔 |
| `EXECUTOR_POLL_SEC` | `1` | 任务队列轮询间隔 |

#### 存储路径

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SHARED_VOLUME_PATH` | `<cwd>/shared-volume` | 任务存储根目录 |
| `WORKSPACE_PATH` | 当前目录 | 执行器网关的宿主工作区路径 |
| `EXECUTOR_BASE_URL` | `http://127.0.0.1:8090` | 协调器使用的网关URL |

#### 国际化

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LANGUAGE` | `zh` | 界面语言：`zh`（中文）或 `en`（英文） |

---

### 工作流治理服务

独立的治理服务，通过 API 强制执行开发工作流规则，防止 AI Agent 绕过验证步骤、越权操作或提交未经检查的状态变更。

**核心原则：** 规则写在代码里，由 API 强制执行——不靠 prompt 约束 AI 自律。

#### 三层架构

| 层 | 存储 | 内容 | 可变性 |
|----|------|------|--------|
| 图定义层 | JSON + NetworkX | 节点定义、依赖边、gate 策略 | 极少变更 |
| 运行态层 | SQLite (governance.db) | 节点状态、会话、任务、版本号 | 高频变更 |
| 事件流层 | JSONL + SQLite 索引 | 谁在什么时候对什么节点做了什么变更 | 只追加 |

#### 核心能力

- **角色访问控制** — Agent 通过 Principal + Session 模型注册，获取 token，只能执行其角色允许的状态转换
- **可配置 Gate 策略** — Gate 支持 `min_status`、`release_only` 和 `waivable` 策略，而非硬编码"必须 pass"
- **结构化证据** — 状态转换需要类型化证据对象（test_report、e2e_report、error_log、commit_ref），含校验和及制品 URI
- **幂等性** — 所有写入 API 支持 `Idempotency-Key` 头，安全重试
- **项目隔离** — 每个项目拥有独立的数据库、图和审计记录
- **影响分析** — 给定文件变更，计算受影响节点、最小验证路径和推荐测试文件
- **快照与回滚** — 基于版本的快照，支持完整状态回滚
- **Redis 缓存** — 双写（SQLite 真相源 + Redis 热缓存），Redis 不可用时自动降级
- **事件总线** — 内部发布/订阅，支持 `node.status_changed`、`gate.satisfied`、`role.expired` 等事件

#### 验收状态流转

```
  PENDING → TESTING → T2_PASS → QA_PASS
     ↑         ↓          ↓         ↓
     └── FAILED ←──────────┘─────────┘

  特殊: PENDING → WAIVED (仅 coordinator)
  禁止: PENDING → QA_PASS (不可跳过 T2)
```

#### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/bootstrap` | 一键初始化：注册项目 + 导入图 + 注册 coordinator |
| POST | `/api/role/register` | 注册 Agent 会话（principal + 项目 + 角色） |
| POST | `/api/role/heartbeat` | 会话心跳 |
| POST | `/api/wf/{pid}/verify-update` | 更新节点验收状态（鉴权 + 证据 + gate 检查） |
| POST | `/api/wf/{pid}/release-gate` | 发布门禁（支持 scope 和 profile） |
| GET | `/api/wf/{pid}/summary` | 按状态分类的统计摘要 |
| GET | `/api/wf/{pid}/node/{nid}` | 单节点状态 + 定义 |
| GET | `/api/wf/{pid}/impact?files=...` | 文件变更影响分析 |
| GET | `/api/wf/{pid}/export?format=mermaid` | 导出为 Mermaid/JSON/Markdown |
| POST | `/api/wf/{pid}/rollback` | 回滚到快照版本 |
| POST | `/api/mem/{pid}/write` | 写入结构化记忆 |
| GET | `/api/mem/{pid}/query` | 按模块/类型/节点查询记忆 |
| GET | `/api/audit/{pid}/log` | 查询审计日志 |
| GET | `/api/audit/{pid}/violations` | 查询失败操作 |

#### Docker 部署

```bash
# 启动治理服务 + Redis
docker compose -f docker-compose.governance.yml up -d

# 初始化项目
curl -X POST http://localhost:30006/api/bootstrap \
  -d '{"project_id":"my-project","graph_source":"/workspace/acceptance-graph.md",
       "coordinator":{"principal_id":"coord","admin_secret":"..."}}'
```

#### 治理服务配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GOVERNANCE_PORT` | `30006` | HTTP 服务端口 |
| `GOVERNANCE_ADMIN_SECRET` | | Coordinator 注册所需密钥 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接 URL |
| `GOVERNANCE_SESSION_TTL_HOURS` | `24` | 会话 token 有效时长 |
| `GOVERNANCE_HEARTBEAT_INTERVAL_SEC` | `60` | 预期心跳间隔 |
| `GOVERNANCE_STALE_TIMEOUT_SEC` | `180` | 标记会话为 stale 的秒数 |
| `GOVERNANCE_EXPIRE_TIMEOUT_SEC` | `600` | 会话过期秒数 |

> 完整架构详见：[`docs/workflow-governance-architecture-v2.md`](docs/workflow-governance-architecture-v2.md)

---

### 文件结构

```
aming_claw/
+-- setup.ps1                  # 首次运行：下载 Python + 安装依赖
+-- start.ps1                  # 启动所有服务
+-- init.ps1                   # 初始化环境
+-- .env.example               # 配置模板
+-- Dockerfile.governance      # 治理服务 Docker 镜像
+-- docker-compose.governance.yml  # 治理服务 + Redis 部署
+-- agent/
|   +-- coordinator.py         # Telegram 机器人 + 任务生命周期管理
|   +-- executor.py            # 任务执行器（调用AI后端）
|   +-- bot_commands.py        # 命令处理器和路由
|   +-- config.py              # 运行时后端/流水线配置
|   +-- task_state.py          # 状态机、事件、心跳
|   +-- task_accept.py         # 验收后测试执行器
|   +-- task_retry.py          # 失败任务重试逻辑
|   +-- backends.py            # AI 后端集成（Claude、Codex、OpenAI）
|   +-- pipeline_config.py     # 按角色绑定供应商/模型
|   +-- model_registry.py      # 从 Anthropic/OpenAI API 获取可用模型
|   +-- auth.py                # 基于 TOTP 的双因素认证实现
|   +-- workspace.py           # 工作区解析和切换
|   +-- workspace_registry.py  # 多工作区注册
|   +-- workspace_queue.py     # 每工作区 FIFO 任务队列
|   +-- parallel_dispatcher.py # 跨工作区并行任务调度
|   +-- git_rollback.py        # Git 检查点、回滚和提交
|   +-- service_manager.py     # 进程监控、重新初始化、重启
|   +-- interactive_menu.py    # Telegram 按钮式界面
|   +-- i18n.py                # 国际化引擎
|   +-- utils.py               # 共享工具（原子JSON、Telegram API）
|   +-- governance/            # 工作流治理服务（端口 30006）
|   |   +-- __init__.py        # 包标记
|   |   +-- server.py          # HTTP 服务 + 路由 + 中间件
|   |   +-- enums.py           # VerifyStatus、VerifyLevel、Role 枚举
|   |   +-- errors.py          # 统一异常层级（14 种错误类型）
|   |   +-- db.py              # SQLite 建表（7 张表）+ WAL + 迁移
|   |   +-- models.py          # Evidence、GateRequirement、NodeDef 数据类
|   |   +-- permissions.py     # 状态机 + scope 检查
|   |   +-- evidence.py        # 结构化证据校验
|   |   +-- gate_policy.py     # 可配置 gate 策略引擎
|   |   +-- graph.py           # NetworkX DAG + Markdown 导入 + Mermaid 导出
|   |   +-- state_service.py   # verify_update、release_gate、快照、回滚
|   |   +-- role_service.py    # Principal + Session + 心跳 + token 鉴权
|   |   +-- impact_analyzer.py # 策略化文件变更影响分析
|   |   +-- project_service.py # 项目注册 + 隔离 + bootstrap
|   |   +-- memory_service.py  # 结构化开发知识库
|   |   +-- audit_service.py   # JSONL 追加 + SQLite 查询索引
|   |   +-- event_bus.py       # 内部事件订阅
|   |   +-- idempotency.py     # 幂等键管理
|   |   +-- redis_client.py    # Redis 客户端（含 SQLite 降级）
|   |   +-- client.py          # GovernanceClient SDK（重试 + 降级）
|   +-- locales/
|   |   +-- zh.json            # 中文翻译
|   |   +-- en.json            # 英文翻译
|   +-- tests/                 # 1000+ 测试用例覆盖所有模块
|   +-- requirements.txt
+-- docs/
|   +-- workflow-governance-design.md          # 治理服务原始设计文档
|   +-- workflow-governance-architecture-v2.md # 完整架构规格（v2）
+-- executor-gateway/
|   +-- app/main.py            # FastAPI 截图与文件操作服务
|   +-- requirements.txt
+-- scripts/
|   +-- _get_python.ps1        # 返回捆绑的 python.exe 路径
|   +-- start-coordinator.ps1
|   +-- start-executor.ps1
|   +-- start-gateway.ps1
|   +-- start-manager.ps1
|   +-- restart-all.ps1
|   +-- restart-agent.ps1
|   +-- restart-from-telegram.ps1
|   +-- reload-after-executor-change.ps1
+-- runtime/
    +-- python/                # setup.ps1 下载，已排除在 git 之外
```

---

### 故障排除

**机器人不响应消息**
- 检查 `.env` 中的 `TELEGRAM_BOT_TOKEN_CODEX` 是否正确设置
- 确保协调器服务正在运行（检查协调器终端窗口）
- 验证服务管理器正在运行：`.\scripts\start-manager.ps1`

**任务卡在 `processing` 状态**
- 协调器会在 `TASK_TIMEOUT_SEC` 秒无心跳后自动将任务标记为 `timeout`
- 检查执行器终端是否有错误；验证 `CODEX_BIN` 是否可访问
- 对于长时间运行的任务，尝试增大 `CODEX_TIMEOUT_SEC`

**"2FA failed: OTP invalid or expired"**
- 确保设备时钟已同步（NTP）
- 增大 `AUTH_OTP_WINDOW`（如 `AUTH_OTP_WINDOW=3`）以容忍时钟偏差
- 运行 `/auth_debug <OTP>` 查看详细验证信息

**AI 返回确认但未执行操作**
- 这是模型行为问题。流水线会检测仅确认消息并重试一次（`TASK_NOOP_RETRIES=1`）
- 如果持续出现，任务将被标记为 `failed`；重新发送任务即可重试
- 考虑通过交互式菜单切换到不同的模型

**拒绝时 Git 回滚失败**
- 确保工作区是一个有效的 git 仓库，且至少有一次提交
- 检查任务前检查点是否已创建（查找 `[aming-claw checkpoint]` 提交）

**语言未切换**
- 在 `.env` 中设置 `LANGUAGE=en` 或 `LANGUAGE=zh` 并重启服务
- 或通过交互式菜单在运行时切换语言
