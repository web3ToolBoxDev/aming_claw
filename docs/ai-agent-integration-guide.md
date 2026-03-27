# AI Agent Integration Guide — Governance Service

This guide is for **AI agents and developers** integrating with the aming-claw
auto-chain workflow. It covers project registration, config format, API usage,
and role-based permissions.

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
| Write memory | POST | `/api/mem/{pid}/write` | Write pattern/pitfall |

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
| `invariant` | Constraints that must not be violated |
| `ownership` | Who is responsible for which module |

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

## Changelog
- 2026-03-26: auto_chain.py implementation complete, full pipeline PM→Dev→Test→QA→Merge→Deploy auto-scheduling with gate validation
- 2026-03-26: Old Telegram bot system fully removed (bot_commands, coordinator, executor, and 20 other modules), unified on governance API
