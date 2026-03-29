# AI Agent Integration Guide — Governance Service

This guide is for **AI agents and developers** integrating with the aming-claw
auto-chain workflow. It covers project registration, config format, API usage,
and role-based permissions.

## Role-Specific Guides

Start with the guide for your role — each is scoped to only what that role needs:

| Role | Guide | Responsibility |
|------|-------|---------------|
| **Dev** | [guide-dev-agent.md](guide-dev-agent.md) | Write code, fix bugs, write memory |
| **Tester / QA** | [guide-tester-qa.md](guide-tester-qa.md) | Run tests, mark T2-pass / QA-pass |
| **Coordinator** | [guide-coordinator.md](guide-coordinator.md) | Orchestrate, assign roles, release gate |

The rest of this file documents the full system for reference.

---

## Quick Start: Register a New Project

### 1. Create `.aming-claw.yaml` in your project root

```yaml
version: 1
project:
  id: "my-project"           # kebab-case ONLY (enforced)
  name: "My Project"
  language: "javascript"      # javascript | python | go

testing:
  unit_command: "npm run test:all"
  e2e_command: "npm run test:e2e"

build:
  command: "npm run build"
  release_checks:             # run before merge (exit 0 = pass)
    - "node scripts/pre-dist.js"

deploy:
  strategy: "electron"        # docker | electron | systemd | process | none
  service_rules:              # file pattern → service mapping
    - patterns: ["server/**"]
      services: ["backend"]
    - patterns: ["client/src/**"]
      services: ["frontend"]
  smoke_test:
    - { name: "backend", type: "http", url: "http://localhost:3000/api/health" }

governance:
  enabled: true
  test_tool_label: "jest"
```

### 2. Register via API

```bash
curl -X POST http://localhost:40000/api/projects/register \
  -H "Content-Type: application/json" \
  -H "X-Gov-Token: <coordinator-token>" \
  -d '{"workspace_path": "/path/to/my-project"}'
```

Returns: `{project_id, config_hash, registered: true, test_command, deploy_strategy}`

### 3. Submit tasks

```bash
# Via governance API (task create) — no X-Gov-Token required
curl -X POST http://localhost:40006/api/task/create \
  -H "Content-Type: application/json" \
  -d '{"message":"Fix the login bug","project_id":"my-project"}'

# Via Telegram
# Send message — telegram_gateway (port 40010) routes to governance server
```

> **Note:** Task create/claim/complete operations no longer require `X-Gov-Token`. Tokens are only used for permission-sensitive operations such as role management and verify-update.

### 4. Query config

```bash
# Resolved config
GET /api/projects/my-project/config

# Dry-run: what services affected by these files?
POST /api/projects/my-project/explain
  {"changed_files": ["server/auth.js", "client/src/Login.tsx"]}
# Returns: affected_services=["backend","frontend"], test_cmd="npm run test:all"
```

---

## Auto-Chain Flow

Every task follows this pipeline automatically. `auto_chain.py` (`agent/governance/auto_chain.py`) wires task completion to next-stage task creation. When a task completes with `succeeded` status, `task_registry.complete_task()` automatically calls `auto_chain.on_task_completed()`, advancing through the following chain:

```
Message → Governance API → PM → Dev → Checkpoint Gate → Tester → QA → Merge → Deploy
```

> Note: The old coordinator.py / backends.py and other modules have been fully removed. Task routing is now handled through the governance server (port 40006) task_registry.

### Auto-Chain Trigger

When you complete a task with type=pm/dev/test/qa/merge, auto_chain automatically creates the next stage's task. A gate check is performed before each stage transition:

| Current Stage (task_type) | Gate Check | Auto-creates After Passing |
|--------------------------|------------|---------------------------|
| `pm` | Post-PM: PRD contains target_files, verification, acceptance_criteria | `dev` task |
| `dev` | Checkpoint: files changed, no out-of-scope modifications | `test` task |
| `test` | T2 Pass: all tests pass | `qa` task |
| `qa` | QA Pass: recommendation is qa_pass | `merge` task |
| `merge` | Release: trust merge result | Auto-triggers `deploy_chain.run_deploy()` |

When a gate fails, the chain auto-retries: creates a new task at the same stage with the gate reason injected into the prompt. The AI receives context like "Previous attempt blocked: Related docs not updated" and can fix the issue. Max retry depth controlled by `MAX_CHAIN_DEPTH` (default 10).

Response format:
```json
{"gate_blocked": true, "stage": "dev", "reason": "...", "retry_task_id": "task-xxx"}
```

Set `_no_retry: true` in metadata to disable auto-retry for a specific task.

### Stage Details

| Stage | What happens | Config used |
|-------|-------------|-------------|
| Governance | Routes message, creates task via task_registry | project_id routing |
| PM | Outputs PRD with target_files + `_verification` | — |
| Dev | Code changes in isolated git worktree | — |
| Checkpoint Gate | Fast check: files changed? syntax valid? | — |
| Tester | Runs `testing.unit_command` from config | `.aming-claw.yaml` |
| QA | Runs governance checks (if `_verification` says so) | `_verification` |
| Merge | Rebases dev branch onto main | — |
| Release Checks | Runs `build.release_checks` from config | `.aming-claw.yaml` |
| Deploy | Detects affected services, restarts them. When called from within governance server, `skip_services=["governance"]` prevents self-restart (governance must be restarted separately). | `deploy.service_rules` |
| Smoke Test | Checks health endpoints | `deploy.smoke_test` |

---

## Who Are You?

The governance service assigns each Agent a specific role. You can only perform operations permitted by your role.

| Role | Responsibility | What You Can Do | What You Cannot Do |
|------|---------------|-----------------|-------------------|
| **Coordinator** | Orchestrate workflows | Assign/revoke roles, create tasks, import graphs, rollback | Cannot modify code directly, cannot run tests |
| **Dev** | Write code | Mark failed→pending (after fix), write development memory | Cannot mark T2-pass, cannot mark QA-pass |
| **Tester** | Run T1+T2 tests | Mark pending→T2-pass | Cannot mark QA-pass, cannot assign roles |
| **QA** | Run E2E tests | Mark T2-pass→QA-pass | Cannot mark T2-pass, cannot assign roles |
| **Gatekeeper** | Release approval | Execute gate-check | Cannot modify code, cannot run tests |

**Rules are enforced by code, not suggestions.** Unauthorized operations will be rejected with 403 and logged in the audit trail.

---

## Integration Flow

### Step 1: Obtain Token

You cannot register yourself. Tokens are assigned to you by a human or the Coordinator.

```
Human runs init_project.py → obtains Coordinator Token
                              │
Coordinator Agent starts ←── Human injects Token
                              │
Coordinator calls /api/role/assign → obtains Tester/Dev/QA Tokens
                              │
Each Agent starts ←────────── Coordinator distributes Tokens
```

**As an Agent, you receive a Token at startup (via environment variable or initialization message). Keep this Token and include it with every API call.**

### Step 2: Include Token with Every API Call

```
Header: X-Gov-Token: gov-<your-token>
Header: Content-Type: application/json
```

The Token already contains your role information. You **must not and cannot** include the `role` field in the request body.

### Step 3: Maintain Heartbeat

Send a heartbeat every 60 seconds, otherwise your session will become stale (180s) then expired (600s).

```
POST http://localhost:30006/api/role/heartbeat
Header: X-Gov-Token: gov-<your-token>
Body: {"project_id": "<pid>", "status": "idle"}
```

---

## API Quick Reference

### Common to All Roles

| Operation | Method | Path | Description |
|-----------|--------|------|-------------|
| Heartbeat | POST | `/api/role/heartbeat` | Call every 60s |
| View summary | GET | `/api/wf/{pid}/summary` | Node count by status |
| View node | GET | `/api/wf/{pid}/node/{nid}` | Single node details |
| Impact analysis | GET | `/api/wf/{pid}/impact?files=a.js,b.js` | File change impact |
| Query memory | GET | `/api/mem/{pid}/query?module=X` | Query related development memory |
| Search memory | GET | `/api/mem/{pid}/search?q=X&top_k=5` | Full-text / semantic search |
| Write memory | POST | `/api/mem/{pid}/write` | Write pattern/pitfall |
| Promote memory | POST | `/api/mem/{pid}/promote` | Copy memory to global scope (cross-project) |
| Register pack | POST | `/api/mem/{pid}/register-pack` | Register domain kind definitions |
| Pre-flight check | GET | `/api/wf/{pid}/preflight-check` | System/version/graph/coverage/queue health |

### Coordinator Only

| Operation | Method | Path | Description |
|-----------|--------|------|-------------|
| Assign role | POST | `/api/role/assign` | Issue token to other Agents |
| Revoke role | POST | `/api/role/revoke` | Revoke an Agent's session |
| View team | GET | `/api/role/{pid}/sessions` | All active sessions |
| Import graph | POST | `/api/wf/{pid}/import-graph` | Import acceptance graph from markdown |
| Update status | POST | `/api/wf/{pid}/verify-update` | Submit status change on behalf of other roles |
| Release gate | POST | `/api/wf/{pid}/release-gate` | Check if release is allowed |
| Rollback | POST | `/api/wf/{pid}/rollback` | Rollback to snapshot version |
| Export graph | GET | `/api/wf/{pid}/export?format=mermaid` | Export visualization graph |

### Tester

| Operation | Method | Path | Body |
|-----------|--------|------|------|
| Mark T2-pass | POST | `/api/wf/{pid}/verify-update` | See examples below |
| Mark failed | POST | `/api/wf/{pid}/verify-update` | See examples below |

### QA

| Operation | Method | Path | Body |
|-----------|--------|------|------|
| Mark QA-pass | POST | `/api/wf/{pid}/verify-update` | See examples below |
| Mark failed | POST | `/api/wf/{pid}/verify-update` | See examples below |

### Dev

| Operation | Method | Path | Body |
|-----------|--------|------|------|
| Restore to pending after fix | POST | `/api/wf/{pid}/verify-update` | See examples below |
| Mark failed | POST | `/api/wf/{pid}/verify-update` | See examples below |

---

## verify-update Request Examples

### Tester: pending → T2-pass

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<tester-token>
Header: Idempotency-Key: tester-001-L0.1-t2-20260322

{
  "nodes": ["L0.1", "L0.2"],
  "status": "t2_pass",
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {
      "passed": 162,
      "failed": 0,
      "exit_code": 0
    },
    "artifact_uri": "logs/test-run-20260322.json"
  }
}
```

### QA: T2-pass → QA-pass

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<qa-token>

{
  "nodes": ["L0.1"],
  "status": "qa_pass",
  "evidence": {
    "type": "e2e_report",
    "tool": "playwright",
    "summary": {
      "passed": 14,
      "failed": 0
    },
    "artifact_uri": "test/main-flow.spec.js"
  }
}
```

### Dev: failed → pending (after fix)

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<dev-token>

{
  "nodes": ["L3.7"],
  "status": "pending",
  "evidence": {
    "type": "commit_ref",
    "tool": "git",
    "summary": {
      "commit_hash": "a1b2c3d4e5f6a7b8"
    }
  }
}
```

### Any Role: Mark failed

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<any-token>

{
  "nodes": ["L3.7"],
  "status": "failed",
  "evidence": {
    "type": "error_log",
    "summary": {
      "error": "Search timeout after 180s, no results returned"
    },
    "artifact_uri": "logs/error-20260322.log"
  }
}
```

---

## Evidence Requirements

Every status change must include structured evidence, otherwise it will be rejected with 400.

| Transition | Evidence Type | Required Fields |
|------------|--------------|-----------------|
| pending → t2_pass | `test_report` | `summary.passed > 0`, `summary.exit_code == 0` |
| t2_pass → qa_pass | `e2e_report` | `summary.passed > 0` |
| * → failed | `error_log` | `summary.error` or `artifact_uri` |
| failed → pending | `commit_ref` | `summary.commit_hash` (7-40 hex chars) |
| pending → waived | `manual_review` | No structural requirements (coordinator only) |

**Complete Evidence object fields:**

```json
{
  "type": "test_report",        // Required: evidence type
  "tool": "pytest",             // Optional: tool name
  "summary": {},                // Required: key data
  "artifact_uri": "path/...",   // Optional: full report path
  "checksum": "sha256:..."      // Optional: checksum
}
```

---

## State Transition Diagram

```
  PENDING ──→ TESTING ──→ T2_PASS ──→ QA_PASS
    │  ↑         │           │           │
    │  │         ↓           ↓           ↓
    │  └───── FAILED ←───────┘───────────┘
    │
    └──→ WAIVED (coordinator only)

  Forbidden path: PENDING → QA_PASS (cannot skip T2)
```

---

## Gate Mechanism

Some nodes have gate prerequisites. If a gate node has not met requirements, your verify-update will be rejected with 403.

```json
// 403 response example
{
  "error": "gate_unsatisfied",
  "message": "Gate prerequisites not met for L1.1",
  "details": {
    "node_id": "L1.1",
    "unsatisfied_gates": [
      {"node_id": "L0.2", "reason": "L0.2 requires qa_pass, got pending"}
    ]
  }
}
```

**What you should do:** First ensure upstream gate nodes pass verification, then verify downstream nodes. Work in topological order.

---

## Scope Restrictions

When registering, the Coordinator may have set a scope for you (e.g., `["L0.*", "L1.*"]`). Operating on nodes outside your scope will be rejected with 403.

```json
// 403 response example
{
  "error": "scope_violation",
  "message": "Node 'L3.1' is outside session scope ['L0.*', 'L1.*']"
}
```

**What you should do:** Only operate on nodes within your scope. If you need to operate on nodes outside your scope, contact the Coordinator to expand your scope or have another Agent handle it.

---

## Idempotency

All write operations support the `Idempotency-Key` header. Safe to retry after network timeout.

```
Header: Idempotency-Key: tester-001-L0.1-t2-20260322
```

- A second request with the same key returns the cached result without re-execution
- Key validity: 24 hours
- Recommended format: `{principal}-{node}-{action}-{date}`

---

## Development Memory

After completing a task, write your experience for other Agents to reference.

### Write Memory

```json
POST /api/mem/my-app/write
Header: X-Gov-Token: gov-<your-token>

{
  "module_id": "stateService",
  "kind": "pitfall",
  "content": "cp command unreliable in Windows worktree, use cat > instead",
  "applies_when": "Windows environment + git worktree",
  "related_nodes": ["L5.1", "L5.2"]
}
```

### Query Memory (before claiming a task)

```
GET /api/mem/my-app/query?module=stateService
GET /api/mem/my-app/query?kind=pitfall
GET /api/mem/my-app/query?node=L5.1
```

**Memory kind types:**

| Kind | Purpose |
|------|---------|
| `pattern` | Design patterns, architectural decisions |
| `pitfall` | Lessons learned, known issues |
| `workaround` | Temporary solutions |
| `decision` | Why A was chosen over B |
| `task_result` | Merge outcome summary (auto-written on merge) |
| `invariant` | Constraints that must not be violated |
| `ownership` | Who is responsible for which module |

### Promote Memory (Cross-Project Sharing)

```json
POST /api/mem/my-app/promote
{"memory_id": "mem-012", "target_scope": "global", "reason": "Applicable to all projects"}
```

Creates a copy with `scope=global` (original stays project-scoped). Promotable kinds: `failure_pattern`, `architecture`, `pattern`, `rule`, `decision`, `knowledge`.

### Register Domain Pack

```json
POST /api/mem/my-app/register-pack
{"domain": "development", "types": {"architecture": {"durability": "permanent", "conflictPolicy": "replace"}}}
```

---

## Pre-flight Self-Check

Run before starting a chain or investigating issues:

```
GET /api/wf/my-app/preflight-check
GET /api/wf/my-app/preflight-check?auto_fix=true
```

Returns 5 independent checks:

| Check | What it validates |
|-------|-------------------|
| `system` | DB accessible, required tables exist |
| `version` | chain_version == git_head, sync freshness |
| `graph` | No orphan pending nodes without active tasks |
| `coverage` | All governance/*.py files in CODE_DOC_MAP |
| `queue` | No stuck claimed tasks (>30min), no circular retries |

With `auto_fix=true`: waives orphan nodes, marks stuck tasks as failed.

---

## Impact Analysis (Required Before Every Task)

Before claiming a task, query which nodes will be affected by the files you plan to modify:

```
GET /api/wf/my-app/impact?files=server/services/stateService.js,config.js
```

The response tells you:
- `direct_hit`: Directly affected nodes
- `verification_order`: Verification order in topological sort
- `test_files`: Test files that need to be run
- `max_verify`: Maximum verification level required
- `skipped`: Nodes skipped due to unmet gate prerequisites

---

## Error Handling

| HTTP Status | Error Code | What You Should Do |
|-------------|-----------|-------------------|
| 400 `invalid_request` | Malformed request | Check required fields |
| 400 `invalid_evidence` | Evidence not qualified | Check evidence type and summary fields |
| 400 `node_not_found` | Node does not exist | Check node ID |
| 401 `auth_required` | No token provided | Add X-Gov-Token header |
| 401 `token_expired` | Token expired | Contact Coordinator for a new token |
| 403 `permission_denied` | Role unauthorized | You cannot perform this operation; this is a correct rejection |
| 403 `scope_violation` | Out of scope | Operate on nodes within your scope |
| 403 `gate_unsatisfied` | Upstream not passed | Complete upstream node verification first |
| 403 `forbidden_transition` | Forbidden transition | Follow the correct path (cannot skip T2) |
| 409 `conflict` | Concurrent conflict | Retry with Idempotency-Key |
| 503 `role_unavailable` | Required role missing | Wait for the corresponding role Agent to come online |

**Key principle: 403 is not a bug; it is the system protecting workflow correctness. Do not attempt to bypass it.**

---

## Coordinator-Only Operations

### Assign Role

```json
POST /api/role/assign
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "principal_id": "tester-001",
  "role": "tester",
  "scope": ["L0.*", "L1.*", "L2.*"]
}
```

The response contains the Agent's token. You need to pass the token to the corresponding Agent.

### Revoke Role

```json
POST /api/role/revoke
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "session_id": "ses-xxx"
}
```

### View Team Status

```
GET /api/role/my-app/sessions
```

### Pre-Release Check

```json
POST /api/wf/my-app/release-gate

{
  "scope": ["L3.*", "L4.*"],
  "profile": "browser-core"
}
```

200 = ready to release, 403 = blockers exist (returns checklist).

---

## Typical Workflows

### Dev Fixing a Bug

```
1. GET  /api/mem/{pid}/query?node=L3.7        ← Query related memory
2. GET  /api/wf/{pid}/impact?files=xxx.js     ← Impact analysis
3. (Write code, make commit)
4. POST /api/wf/{pid}/verify-update            ← Mark failed→pending
   Body: {nodes:["L3.7"], status:"pending",
          evidence:{type:"commit_ref", summary:{commit_hash:"abc123"}}}
5. POST /api/mem/{pid}/write                   ← Write fix experience
   Body: {module_id:"searchPipeline", kind:"pitfall", ...}
```

### Tester Verifying a Task

```
1. GET  /api/wf/{pid}/summary                  ← See which nodes are pending
2. (Run tests)
3. POST /api/wf/{pid}/verify-update            ← Mark T2-pass
   Body: {nodes:["L0.1","L0.2"], status:"t2_pass",
          evidence:{type:"test_report", summary:{passed:162, failed:0, exit_code:0}}}
```

### Coordinator Orchestrating a Release

```
1. GET  /api/role/{pid}/sessions               ← Confirm team is in place
2. GET  /api/wf/{pid}/summary                  ← Confirm status
3. POST /api/wf/{pid}/release-gate             ← Release gate check
   Body: {scope:["L3.*","L4.*"]}
4. If 403 → view blockers → assign corresponding role to handle
5. If 200 → ready to release
```

---

## When the Governance Service Is Unreachable

| API Type | Behavior |
|----------|----------|
| verify-update | **Block and wait** (bounded retry, max 120s) — status changes cannot be bypassed |
| release-gate | **Block and wait** — release gate cannot be skipped |
| mem/write | Cache locally, push after service recovers |
| mem/query | Return empty, do not block work |

**Never mark node status on your own when the governance service is unreachable.**

## Chain Context (Phase 8)

The auto-chain now maintains event-sourced runtime context for each task chain.

### How It Works
- `ChainContextStore` subscribes to EventBus events (`task.created`, `task.completed`, `gate.blocked`, `task.retry`, `task.failed`)
- Each event updates in-memory state AND appends to `chain_events` DB table (append-only)
- On crash recovery, events are replayed from DB to rebuild in-memory state

### Context Snapshot API
`GET /api/context-snapshot/{project_id}?task_id=XXX&role=dev` now includes a `task_chain` field:
- Shows all stages in the chain, filtered by role visibility
- dev sees PM + dev stages; test sees dev + test; coordinator sees all
- `result_core` fields filtered per role (dev gets target_files/requirements, test gets changed_files, etc.)

### Retry Prompt Recovery
When a gate blocks and creates a retry task, the retry prompt now recovers the original prompt from chain context instead of relying solely on metadata (which was often empty). Fallback chain: metadata → ChainContextStore → result summary.

### Chain Lifecycle
- Chain starts when root task is created (root_task_id = first task_id)
- Retry tasks inherit root_task_id from the original task
- Chain states: running → blocked → retrying → completed/failed
- After merge completes, chain is archived (memory released, DB data preserved for audit)

### DB Schema
```sql
CREATE TABLE chain_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id  TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ts            TEXT NOT NULL
);
```

## SQLite Independent Connection Pattern

### Problem

The governance server maintains a long-lived shared SQLite connection per request.
When a high-frequency writer (e.g. the executor git-sync loop) holds a WAL write
lock, a concurrent `version-update` or `version-sync` call on the shared connection
can encounter `SQLITE_BUSY` immediately — the default busy-timeout of 10 000 ms
adds unacceptable latency to the HTTP worker thread.

### Solution — `independent_connection()` + `_retry_on_busy()`

`db.py` exposes a lightweight helper:

```python
from agent.governance.db import independent_connection

conn = independent_connection(project_id, busy_timeout=5000)
# … execute writes …
conn.close()
```

Key properties:
- Opens a **brand-new** connection (not from any pool).
- `busy_timeout=5000` (5 s) — tighter than the shared-connection 10 s default.
- Does **not** call `_ensure_schema` — database is assumed to be fully migrated.
- Caller is responsible for `conn.close()`.

For write paths that may race (version-update, version-sync), wrap the DB call
with `_retry_on_busy()` defined in `server.py`:

```python
# 3 attempts with 0.5 s → 1 s → 2 s back-off
_retry_on_busy(_do_write_fn)
```

`_retry_on_busy` catches `sqlite3.OperationalError: database is locked` and
retries up to 3 times before re-raising.  The inner write function should be
idempotent (use `INSERT OR REPLACE` / `ON CONFLICT … DO UPDATE` semantics).

### Usage in server.py

Both `handle_version_update` and `handle_version_sync` follow this pattern:

```python
def _do_write():
    conn = independent_connection(pid)
    try:
        conn.execute("INSERT OR REPLACE INTO project_version …", (…,))
        conn.commit()
    finally:
        conn.close()

_retry_on_busy(_do_write)
```

---

## Changelog
- 2026-03-28: Batch 1 flow fixes — R1: test/QA gate fail creates dev retry (降级重跑) instead of same-stage escalate; R2: _build_qa_prompt requires exactly qa_pass or reject; M3: dev success writes pattern memory; S1: session_context skips empty session_summary when decisions=0 and messages=0
- 2026-03-28: P1-P3 optimization — memory injection all task types; index_status tracking + flush-index; conflict_policy enforcement; TTL cleanup endpoint; orphan task recovery; role-split guides (guide-dev-agent.md, guide-tester-qa.md, guide-coordinator.md)
- 2026-03-28: Add independent_connection() + _retry_on_busy(); use in handle_version_update/handle_version_sync
- 2026-03-28: DB lock fix: auto_chain uses independent connection with guaranteed close
- 2026-03-28: M3-M6 Gate enhancements: skip_doc_check needs bootstrap_reason, release gate node warning, version-update chain link validation, QA dedup
- 2026-03-28: M1+M2 Task ownership validation + observer override audit in complete_task
- 2026-03-28: Fix version_check hash prefix comparison + DB connection leak in version-sync/update
- 2026-03-28: Phase 8 Chain Context — event-sourced chain runtime context, retry prompt recovery, context-snapshot API
- 2026-03-26: auto_chain.py implementation complete, full pipeline PM→Dev→Test→QA→Merge→Deploy auto-scheduling with gate validation
- 2026-03-26: Old Telegram bot system fully removed (bot_commands, coordinator, executor, and 20 other modules), unified on governance API
