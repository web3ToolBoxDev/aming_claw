# Aming Claw

> AI Workflow Governance Platform — Monitor and take over AI task execution through the Observer pattern

## Your AI Supervisor

Any Claude Code session can act as an **Observer** to supervise your project:

```
You (Observer)
  │
  ├── Submit tasks via Telegram or API
  ├── Monitor task status via Governance API
  ├── Watch Task / Node transitions in real time
  └── Intervene anytime: claim, pause, takeover
```

**The Observer doesn't write code — it makes decisions.** It submits work, watches the auto-chain execute, and steps in only when things go wrong.

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         Observer (You)            │
                    │   Claude Code / API / Telegram    │
                    └──────────┬──────────────────┬────┘
                               │                  │
                    ┌──────────▼──────┐   ┌───────▼────────┐
                    │ Telegram Gateway │   │ Governance API │
                    │   :40010         │   │   :40006       │
                    └──────────┬──────┘   └───────┬────────┘
                               │                  │
                    ┌──────────▼──────────────────▼────────┐
                    │          Redis (:40079)               │
                    │  Pub/Sub · Streams · Context · Route  │
                    └──────────┬───────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐ ┌──────▼───────┐ ┌──────▼───────┐
    │ Task Registry   │ │ Executor API │ │ Executor GW  │
    │ (SQLite)        │ │   :40100     │ │   :8090      │
    │ governance.db   │ │ Monitor/Ctrl │ │ Execution    │
    └────────────────┘ └──────────────┘ └──────────────┘
```

### Core Services

| Service | Port | Responsibility |
|---------|------|----------------|
| **Governance Server** | 40006 | Task registry, workflow state machine, node management, audit log |
| **Telegram Gateway** | 40010 | Telegram message routing, project binding, event notifications |
| **Executor Gateway** | 8090 | Actual AI task execution (code changes, tests, screenshots) |
| **Executor API** | 40100 | Task monitoring, session management, manual intervention |

### Data Storage

| Layer | Storage | Purpose |
|-------|---------|---------|
| **Task Queue** | SQLite (governance.db) + filesystem | ACID transactions, task lifecycle |
| **Message Routing** | Redis Streams | Telegram message queue, project isolation |
| **Context** | Redis Hash (24h TTL) → SQLite archive | AI session input/output |
| **Notifications** | Redis Pub/Sub + Outbox | Real-time events + guaranteed delivery |
| **Audit** | JSONL (append-only) + SQLite index | Tamper-proof operation log |

---

## Observer Workflow

### 1. Submit Tasks via Telegram

```
User sends message in Telegram
         │
         ▼
  Gateway (:40010) classifies intent
         │
         ├── Command → handle directly (/menu, /bind, /status)
         └── Task    → POST /api/task/{project}/create
                              │
                              ▼
                     Task Registry (SQLite)
                        status: queued
```

Supported gateway commands:

| Command | Description |
|---------|-------------|
| `/menu` | Main menu (project switching, status) |
| `/bind <token>` | Bind to a governance project |
| `/unbind` | Unbind current project |
| `/status` | View current project task status |
| `/projects` | List all projects |
| `/health` | Check service health |

### 2. Submit Tasks via Governance API

```bash
# Create a task (no token required for task operations)
curl -X POST http://localhost:40006/api/task/aming-claw/create \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Fix the /config endpoint bug in server.py",
    "type": "task",
    "priority": 1,
    "metadata": {"category": "bugfix"}
  }'

# Response
{
  "task_id": "task-1774543022-0e1bf4",
  "status": "created",
  "project_id": "aming-claw"
}
```

### 3. Monitor Task Transitions

After creation, the Observer can track the full lifecycle in real time:

```
CREATE ──► QUEUED ──► CLAIMED ──► RUNNING ──► SUCCEEDED
                                     │
                                     └──► FAILED ──► RETRY (max 3)
                                                       └──► DESIGN_MISMATCH (needs human)
```

#### Monitoring APIs

```bash
# List tasks
curl http://localhost:40006/api/task/aming-claw/list

# Runtime status (active + queued + pending notifications)
curl http://localhost:40006/api/runtime/aming-claw

# Node status summary
curl http://localhost:40006/api/wf/aming-claw/summary
# => {"pending": 1, "testing": 108, "waived": 70}
```

### 4. Take Over a Task

When the auto-chain fails or needs human intervention, the Observer can take over directly:

```bash
# Claim a queued task
curl -X POST http://localhost:40006/api/task/aming-claw/claim \
  -H "Content-Type: application/json" \
  -d '{"worker_id": "observer-claude-code"}'

# Response: fence_token prevents concurrent conflicts
{
  "task": [
    {"task_id": "task-xxx", "prompt": "...", "attempt_num": 1},
    "fence-xxx-yyy"
  ]
}

# Report progress
curl -X POST http://localhost:40006/api/task/aming-claw/progress \
  -H "Content-Type: application/json" \
  -d '{"task_id": "task-xxx", "progress": {"step": "coding", "files_changed": 3}}'

# Mark complete (auto-chain triggers next stage)
curl -X POST http://localhost:40006/api/task/aming-claw/complete \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task-xxx",
    "status": "succeeded",
    "result": {"changed_files": ["agent/server.py"], "test_results": {"passed": 200, "failed": 0}}
  }'
```

### 5. Update Node Status

After a task completes, the Observer updates workflow node verification status:

```bash
# Update individual or batch node status
curl -X POST http://localhost:40006/api/wf/aming-claw/verify-update \
  -H "Content-Type: application/json" \
  -d '{
    "nodes": ["L8.5", "L14.7"],
    "status": "testing",
    "evidence": {
      "type": "test_report",
      "producer": "observer-claude-code",
      "tool": "pytest",
      "summary": {"passed": 200, "failed": 0}
    }
  }'

# Batch baseline
curl -X POST http://localhost:40006/api/wf/aming-claw/baseline \
  -H "Content-Type: application/json" \
  -d '{
    "nodes": {"L1.1": "waived", "L1.2": "waived"},
    "reason": "Old modules removed"
  }'
```

---

## Auto-Chain

Tasks flow automatically through a multi-role verification chain with gate validation between each stage. Implemented in `agent/governance/auto_chain.py`.

```
Coordinator → PM → Dev → Gate → Tester → QA → Merge → Deploy
                    │            │         │        │
                    │ Post-PM    │ Check-   │ T2     │ QA
                    │ Gate       │ point    │ Pass   │ Pass
                    │            │ Gate     │ Gate   │ Gate
                    ▼            ▼         ▼        ▼
              target_files?  files ok?  0 fail?  qa_pass?
              criteria?      no drift?
```

When any gate fails, the chain stops and publishes a `gate.blocked` event. The Observer can inspect the reason and intervene.

### Roles

| Role | Responsibility | Permissions |
|------|---------------|-------------|
| **PM** | Analyze requirements, output PRD with target_files + acceptance_criteria + verification | Read-only, propose_node |
| **Coordinator** | Dispatch PM/Dev tasks, validate PRD, route messages | All dispatch APIs |
| **Dev** | Write code in isolated git worktree | Read, Write, Edit, Bash |
| **Tester** | Run unit tests, report results | Bash (pytest) |
| **QA** | Review code change quality | Read, Bash |
| **Observer** | Monitor, take over, manual intervention | All APIs (read + claim/complete) |

### Gate Validation (between each stage)

| Gate | Checks | Blocks if |
|------|--------|-----------|
| **Post-PM Gate** | PRD has `target_files`, `verification`, `acceptance_criteria` | Any mandatory field missing |
| **Checkpoint Gate** | Files changed, no unrelated modifications outside target_files | No changes or unauthorized files |
| **T2 Pass Gate** | Test report has 0 failures | Any test failure |
| **QA Pass Gate** | Recommendation is `qa_pass` or `qa_pass_with_fallback` | QA rejects |
| **Release Gate** | Merge succeeded | Merge failure |

After merge, `deploy_chain.run_deploy()` auto-detects affected services and restarts them (Docker or local process fallback).

### Auto-Chain Response Format

When completing a task, the response includes chain status:

```json
{
  "task_id": "task-xxx",
  "status": "succeeded",
  "auto_chain": {
    "task_id": "task-yyy",       // next stage task (auto-created)
    "project_id": "aming-claw",
    "type": "test"               // next stage type
  }
}

// Or if gate blocks:
{
  "task_id": "task-xxx",
  "status": "succeeded",
  "auto_chain": {
    "gate_blocked": true,
    "stage": "pm",
    "reason": "PRD missing mandatory fields: ['target_files']"
  }
}
```

### Verification Status Flow

```
PENDING → TESTING → T2_PASS → QA_PASS
   ↑         ↓          ↓         ↓
   └── FAILED ←──────────┘─────────┘

Special: PENDING → WAIVED (coordinator/observer only)
Forbidden: PENDING → QA_PASS (cannot skip T2 testing)
```

---

## Quick Start

### 1. Start Services

```bash
# Docker (recommended)
docker compose -f docker-compose.governance.yml up -d

# Or Windows local
.\setup.ps1          # One-time: download Python + install deps
copy .env.example .env
.\start.ps1          # Start all services
```

### 2. Create a Project (one command, no password)

```bash
curl -X POST http://localhost:40006/api/init \
  -H "Content-Type: application/json" \
  -d '{"project_id": "my-project"}'

# => {"project": {"project_id": "my-project", "status": "active"}}
```

That's it. No tokens, no passwords. Start submitting tasks immediately:

```bash
curl -X POST http://localhost:40006/api/task/my-project/create \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Fix the login bug", "type": "dev"}'
```

### 3. Bind Telegram (optional)

```
# In Telegram, send to @your_bot:
/bind my-project

# Now any message you send creates a task automatically
```

### 4. Register an External Project (optional)

Any project can join by adding `.aming-claw.yaml`:

```bash
curl -X POST http://localhost:40006/api/projects/register \
  -H "Content-Type: application/json" \
  -d '{"workspace_path": "/path/to/your/project"}'
```

See [AI Agent Integration Guide](docs/ai-agent-integration-guide.md) for the full config format.

---

## API Reference

### Project Management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/init` | Create project (no password needed) |
| GET | `/api/project/list` | List all projects |
| POST | `/api/projects/register` | Register workspace |

### Task Management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/task/{pid}/create` | Create task |
| POST | `/api/task/{pid}/claim` | Claim task (returns fence_token) |
| POST | `/api/task/{pid}/progress` | Report progress |
| POST | `/api/task/{pid}/complete` | Mark complete (triggers auto-chain) |
| GET | `/api/task/{pid}/list` | List tasks |
| POST | `/api/task/{pid}/recover` | Recover failed task |

### Workflow / Nodes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/wf/{pid}/verify-update` | Update node verification status |
| POST | `/api/wf/{pid}/baseline` | Batch set node status (coordinator only) |
| POST | `/api/wf/{pid}/release-gate` | Pre-release gate check |
| GET | `/api/wf/{pid}/summary` | Node status summary |
| GET | `/api/wf/{pid}/node/{nid}` | Single node details |
| GET | `/api/wf/{pid}/export` | Export as JSON/Mermaid |
| GET | `/api/wf/{pid}/impact?files=...` | File change impact analysis |
| POST | `/api/wf/{pid}/rollback` | Rollback to snapshot version |
| POST | `/api/wf/{pid}/node-delete` | Delete nodes from graph + DB |

### Roles & Sessions

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/role/assign` | Assign role token |
| POST | `/api/role/heartbeat` | Session heartbeat |
| GET | `/api/role/verify` | Verify token |

### Context & Memory

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/context/{pid}/save` | Save session context |
| GET | `/api/context/{pid}/load` | Load session context |
| POST | `/api/mem/{pid}/write` | Write development memory |
| GET | `/api/mem/{pid}/query` | Query memories |

### Audit

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/audit/{pid}/log` | Audit log |
| GET | `/api/audit/{pid}/violations` | Violation records |
| GET | `/api/health` | Service health check |

---

## File Structure

```
aming_claw/
├── agent/
│   ├── governance/              # Governance service (29 modules)
│   │   ├── server.py            # HTTP server + routing (40006)
│   │   ├── auto_chain.py        # Auto-chain dispatcher (PM→Dev→Test→QA→Merge→Deploy)
│   │   ├── task_registry.py     # Task lifecycle management
│   │   ├── state_service.py     # Node state transitions
│   │   ├── role_service.py      # Roles + sessions + tokens
│   │   ├── graph.py             # DAG definition (NetworkX)
│   │   ├── gatekeeper.py        # Pre-release gate checks
│   │   ├── audit_service.py     # Audit logging
│   │   ├── evidence.py          # Structured evidence
│   │   ├── memory_service.py    # Development memory
│   │   └── ...                  # db, models, enums, errors, etc.
│   ├── telegram_gateway/        # Telegram gateway (5 modules)
│   │   ├── gateway.py           # HTTP + Telegram polling (40010)
│   │   ├── gov_event_listener.py # Events → Telegram notifications
│   │   ├── chat_proxy.py        # Message proxy
│   │   └── message_worker.py    # Async message processing
│   ├── executor_api.py          # Monitoring API (40100)
│   ├── ai_lifecycle.py          # AI session lifecycle
│   ├── context_assembler.py     # Context assembly
│   ├── pipeline_config.py       # Pipeline configuration
│   ├── project_config.py        # Project config parser
│   ├── deploy_chain.py          # Deploy chain
│   ├── utils.py                 # Shared utilities
│   └── tests/                   # 200+ test cases
├── executor-gateway/            # Execution gateway (FastAPI, 8090)
│   ├── app/main.py
│   ├── config/actions.yaml
│   └── executors/               # Execution scripts
├── docs/                        # Architecture docs (19 files)
├── shared-volume/codex-tasks/   # Runtime data
│   ├── pending/                 # Awaiting execution
│   ├── processing/              # In progress
│   ├── results/                 # Completed
│   ├── archive/                 # Archived
│   └── state/governance/        # Governance databases (per-project)
├── docker-compose.governance.yml
├── Dockerfile.governance
├── Dockerfile.telegram-gateway
└── scripts/                     # Start/restart scripts
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | | Telegram bot token |
| `EXECUTOR_API_TOKEN` | | Executor-gateway auth secret |
| `GOVERNANCE_PORT` | `40006` | Governance service port |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `SHARED_VOLUME_PATH` | `./shared-volume` | Task data root directory |
| `WORKSPACE_PATH` | cwd | Workspace path |
| `EXECUTOR_BASE_URL` | `http://127.0.0.1:8090` | Executor-gateway URL |

### Docker Deployment

```bash
# Start all services (governance + gateway + redis)
docker compose -f docker-compose.governance.yml up -d

# View logs
docker compose -f docker-compose.governance.yml logs -f governance
```

---

## Troubleshooting

**Task stays queued, nobody claims it**
- Ensure a coordinator process is polling `POST /api/task/{pid}/claim`
- The Observer can claim and execute manually

**Gateway can't connect to Telegram**
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Ensure only one process is polling the Telegram API

**Node status update rejected**
- Check role permissions: only tester can do `pending→testing→t2_pass`, only qa can do `t2_pass→qa_pass`
- Coordinator can use `baseline` to bypass permission checks

**Governance service not responding**
- `curl http://localhost:40006/api/health`
- Check that the SQLite database file is writable

---

## Changelog

- **2026-03-26**: Auto-chain fully wired (`auto_chain.py`). PM → Dev → Test → QA → Merge → Deploy runs end-to-end with gate validation between stages. Deploy chain triggers service restart automatically. Task create/claim/complete no longer require token.
- **2026-03-26**: Old Telegram bot system fully removed (bot_commands, coordinator, executor, and 17 other modules). Unified on Governance API. Observer pattern is now the primary interaction model.
