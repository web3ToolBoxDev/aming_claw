# PRD v6: Minimal Context + Self-Service APIs + Memory + Multi-Project Version Gate

**Author:** Observer (Claude Code session)
**Date:** 2026-03-27
**Status:** Pending Review (v6.2 — adds version-update protection + role query strategy)
**Priority:** P0 — Foundation infrastructure

---

## 1. Problem Statement

Three critical gaps in the current system:

1. **No context injection** — AI roles launch with minimal prompt. Coordinator doesn't know audit logs are in SQLite, tells users to check log files. Dev doesn't see past decisions or node status.

2. **No memory persistence** — Task completion results are lost. Next session starts from scratch.

3. **No workflow enforcement** — Manual commits bypass auto-chain with zero checks.

### Design Principle: Minimal Base + Self-Service

**Rejected approach (v5):** Full context assembly at startup (ContextAssembler injects everything).
- Wastes tokens on irrelevant context
- Coordinator handling "hello" doesn't need git diff and node list
- Rigid budget allocation per role

**Adopted approach (v6):** Minimal base context + AI self-service via APIs.
- ~500 token base context at startup (snapshot)
- AI queries APIs on demand when it needs more info
- AI decides what's relevant, not hardcoded budgets

---

## 2. Architecture Overview

```
Session Start
  │
  ▼
Layer 1: Base Context Snapshot (auto-injected, ~500 token)
  GET /api/context-snapshot/{pid}?role=coordinator
  │
  ├── task_summary      (current task goal)
  ├── role_instructions (what you can/cannot do)
  ├── project_state     (version, dirty, node counts)
  ├── recent_memories   (3 most relevant entries)
  ├── constraints       (key rules)
  ├── snapshot_at       (ISO timestamp — consistency anchor)
  └── project_version   (chain_version — integrity reference)
  │
  ▼
Claude CLI starts with base context in system prompt
  │
  ▼
Layer 2: Self-Service APIs (on-demand, AI calls curl)
  │
  ├── Project State API    GET /api/health, /api/version-check/{pid}
  ├── Task / Node API      GET /api/task/{pid}/list, /api/wf/{pid}/summary
  ├── Memory API           GET /api/mem/{pid}/query?module=X
  ├── Runtime / Audit API  GET /api/audit/{pid}/log?limit=N
  └── Git / Code API       GET /git-status (MCP :40020)
  │
  Each response includes:
    generated_at:      "2026-03-27T15:30:00Z"
    project_version:   "1a0965d"
  │
  AI can detect stale data and re-fetch if needed
```

### Context Consistency Model

**Problem:** If AI calls multiple APIs at different times, data may be inconsistent (task list from 3s ago, nodes from 10s ago, memory from 1min ago).

**Solution:** Two-tier consistency:

| Tier | Mechanism | Guarantee |
|------|-----------|-----------|
| **Base context (Layer 1)** | Single `/api/context-snapshot` call | Point-in-time snapshot, all data from same moment |
| **On-demand (Layer 2)** | Each API returns `generated_at` + `project_version` | AI can detect staleness and re-fetch |

Base context is the "ground truth anchor" at session start. On-demand APIs are for drilling deeper, and AI is told to check timestamps if consistency matters.

---

## 3. Changes

### 3.1 Multi-Project Version Gate

#### 3.1.1 Database Schema

**File:** `agent/governance/db.py`

```sql
CREATE TABLE IF NOT EXISTS project_version (
    project_id    TEXT PRIMARY KEY,
    chain_version TEXT NOT NULL,     -- git short hash from last auto-merge
    updated_at    TEXT NOT NULL,     -- ISO 8601
    updated_by    TEXT NOT NULL      -- "auto-chain" | "init" | "register"
);
```

#### 3.1.2 Project Init / Register

**File:** `agent/governance/project_service.py`

On `POST /api/init` and `POST /api/projects/register`:
- INSERT into `project_version` with current git HEAD as `chain_version`
- `updated_by` = "init" or "register"

#### 3.1.3 MCP Server Git Status Endpoint

**File:** `agent/mcp/server.py`

MCP server runs on host (has git). Exposes HTTP :40020:

```
GET /git-status
{
    "head": "1a0965d",
    "dirty": true,
    "dirty_files": ["agent/gateway.py"],
    "generated_at": "2026-03-27T15:30:00Z"
}
```

Also exposed as MCP tool `version_check` for Observer.

#### 3.1.4 Governance Version Check API

**File:** `agent/governance/server.py`

```
GET /api/version-check/{project_id}
{
    "ok": false,
    "project_id": "aming-claw",
    "head": "1a0965d",
    "chain_version": "9226e4d",
    "dirty": true,
    "dirty_files": ["agent/gateway.py"],
    "commits_since_chain": 12,
    "message": "12 manual commits, 1 uncommitted file",
    "generated_at": "2026-03-27T15:30:00Z",
    "project_version": "9226e4d"
}
```

Logic:
1. Read `chain_version` from `project_version` table
2. Call MCP `GET http://host.docker.internal:40020/git-status`
3. Compare and return

Fail-open: if MCP unreachable, return `{"ok": true, "message": "git status unavailable"}`.

#### 3.1.5 Gateway Version Gate

**File:** `agent/telegram_gateway/gateway.py` — `handle_task_dispatch()` entry

```python
def handle_task_dispatch(chat_id, text, route):
    project_id = route.get("project_id", "")
    try:
        check = gov_api("GET", f"/api/version-check/{project_id}")
        if not check.get("ok"):
            lines = ["⚠️ Workflow gate blocked:"]
            if check.get("commits_since_chain"):
                lines.append(f"  {check['commits_since_chain']} manual commits")
                lines.append(f"  HEAD={check['head']}  CHAIN={check['chain_version']}")
            if check.get("dirty_files"):
                lines.append(f"  {len(check['dirty_files'])} uncommitted files")
            lines.append("\nRun auto-chain to sync.")
            send_text(chat_id, "\n".join(lines))
            return
    except Exception:
        pass  # fail-open
    # ... create coordinator task
```

#### 3.1.6 Merge Updates Version

**File:** `agent/executor_worker.py` — `_execute_merge()` after success

```python
self._api("POST", f"/api/version-update/{self.project_id}", {
    "chain_version": new_hash,
    "updated_by": "auto-chain",
})
```

**File:** `agent/governance/server.py` — `POST /api/version-update/{pid}`

Adopts "lightweight internal protection + lifecycle validation + audit trail" strategy for personal projects.

##### Request Format

```json
{
    "project_id": "aming-claw",
    "task_id": "task-xxx",
    "old_version": "abc123",
    "new_version": "def456",
    "chain_stage": "merge",
    "updated_by": "auto-chain"
}
```

##### Protection Rules (5-step server-side validation)

```
Step 1: Internal Token Check
  Header: X-Internal-Token must match VERSION_UPDATE_TOKEN env var
  Purpose: prevent agent/script/temp code from "casually calling"
  Reject reason: INVALID_TOKEN

Step 2: Field Completeness
  Required: project_id, task_id, old_version, new_version, chain_stage, updated_by
  Reject reason: MISSING_FIELDS

Step 3: Lifecycle Validation
  chain_stage must == "merge"
  Task status must be "completed" or "merged"
  updated_by must be "auto-chain" or "merge-service"
  Reject reason: INVALID_CHAIN_STAGE | TASK_NOT_COMPLETED | INVALID_UPDATED_BY

Step 4: Version Consistency
  old_version must == DB.project_version.chain_version (prevent stale update)
  new_version must == git HEAD (prevent arbitrary version)
  Reject reason: OLD_VERSION_MISMATCH | NEW_VERSION_NOT_GIT_HEAD

Step 5: Update + Audit
  Success: UPDATE project_version, write success audit
  Failure: write reject audit with reason
```

##### Reject Reason Enum

```python
VERSION_UPDATE_REJECT = {
    "INVALID_TOKEN",
    "MISSING_FIELDS",
    "INVALID_CHAIN_STAGE",
    "TASK_NOT_COMPLETED",
    "OLD_VERSION_MISMATCH",
    "NEW_VERSION_NOT_GIT_HEAD",
    "INVALID_UPDATED_BY",
    "PROJECT_NOT_FOUND",
    "INTERNAL_ERROR",
}
```

##### Audit Record (every call, success or failure)

```json
{
    "event": "version.update_attempt",
    "project_id": "aming-claw",
    "task_id": "task-xxx",
    "old_version": "abc123",
    "new_version": "def456",
    "chain_stage": "merge",
    "updated_by": "auto-chain",
    "caller_role": "merge-service",
    "result": "success | rejected",
    "reject_reason": "OLD_VERSION_MISMATCH",
    "timestamp": "2026-03-27T15:30:00Z"
}
```

##### Design Rationale

This endpoint does NOT need enterprise-grade auth. It protects against:
- AI agent forgetting constraints and accidentally updating baseline
- Non-merge stage incorrectly advancing chain_version
- Debug scripts / temp code accidentally calling
- System complexity growing and boundary leaking

The internal token is just "door entry"; lifecycle validation is the real business gate.

#### 3.1.7 Anti-Tamper

| Attack | Defense |
|--------|---------|
| AI calls /api/version-update directly | 5-step validation rejects: token + lifecycle + version checks |
| AI commits to match chain_version | Commit changes HEAD → new mismatch |
| AI modifies dirty check | MCP server runs git directly, subprocess not interceptable |
| No VERSION file to edit | Version stored in governance.db, not filesystem |
| Script/temp code calls update | X-Internal-Token check blocks casual calls |
| Non-merge stage tries to update | chain_stage == "merge" check rejects |
| Stale update (race condition) | old_version == DB.chain_version prevents double-update |

---

### 3.2 Context Snapshot API (Layer 1 — Base Context)

#### 3.2.1 Snapshot Endpoint

**File:** `agent/governance/server.py`

```
GET /api/context-snapshot/{project_id}?role=coordinator&task_id=xxx
{
    "snapshot_at": "2026-03-27T15:30:00Z",
    "project_version": "9226e4d",

    "task": {
        "task_id": "task-xxx",
        "type": "coordinator",
        "prompt": "user message here",
        "attempt_num": 1
    },

    "project_state": {
        "version_ok": true,
        "chain_version": "9226e4d",
        "total_nodes": 109,
        "nodes_by_status": {"qa_pass": 109},
        "active_tasks": 2,
        "queued_tasks": 0
    },

    "recent_memories": [
        {"kind": "decision", "content": "Used worktree isolation for dev tasks", "created_at": "..."},
        {"kind": "pitfall", "content": "Docker gateway needs rebuild after code change", "created_at": "..."},
        {"kind": "test_result", "content": "26 tests passed", "created_at": "..."}
    ],

    "constraints": [
        "Do NOT tell users to check log files. Use /api/audit endpoint.",
        "All data is in governance.db and dbservice, not filesystem."
    ]
}
```

**Implementation:** Single DB transaction reads task + nodes + memories + version → consistent snapshot.

#### 3.2.2 Injection into AI Session

**File:** `agent/ai_lifecycle.py` — `_build_system_prompt()`

```python
def _build_system_prompt(self, role, prompt, context, project_id):
    from role_permissions import ROLE_PROMPTS
    role_prompt = ROLE_PROMPTS.get(role, "")

    # Fetch base context snapshot (single API call, consistent)
    snapshot = {}
    try:
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        task_id = context.get("task_id", "")
        resp = urllib.request.urlopen(
            f"{gov_url}/api/context-snapshot/{project_id}?role={role}&task_id={task_id}",
            timeout=5
        )
        snapshot = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("Context snapshot fetch failed: %s", e)

    snapshot_str = json.dumps(snapshot, ensure_ascii=False, indent=2) if snapshot else "{}"

    return (
        f"{role_prompt}\n\n"
        f"Project: {project_id}\n"
        f"Context Snapshot:\n{snapshot_str}\n\n"
        f"Task: {prompt}\n\n"
        f"For more details, query the APIs listed in your role instructions."
    )
```

---

### 3.3 Self-Service API Layer (Layer 2 — On-Demand)

No new APIs needed. Existing 58 endpoints already cover all categories. Change: add `generated_at` and `project_version` to key responses.

**File:** `agent/governance/server.py` — Modify response format for key endpoints:

| Endpoint | Add Fields |
|----------|-----------|
| `GET /api/task/{pid}/list` | `generated_at`, `project_version` |
| `GET /api/wf/{pid}/summary` | `generated_at`, `project_version` |
| `GET /api/mem/{pid}/query` | `generated_at`, `project_version` |
| `GET /api/audit/{pid}/log` | `generated_at`, `project_version` |
| `GET /api/runtime/{pid}` | `generated_at`, `project_version` |

Implementation: wrapper function adds these fields from DB:
```python
def _with_meta(response: dict, project_id: str) -> dict:
    response["generated_at"] = utc_now()
    ver = conn.execute("SELECT chain_version FROM project_version WHERE project_id=?", (project_id,)).fetchone()
    response["project_version"] = ver[0] if ver else "unknown"
    return response
```

#### 3.3.1 API Categories in ROLE_PROMPT

**File:** `agent/role_permissions.py` — All roles get this reference:

```
When you need more information, query these APIs using curl:

1. Project State
   GET /api/health                          — Service health, version, PID
   GET /api/version-check/{pid}             — Version gate status, dirty files

2. Task / Node
   GET /api/task/{pid}/list                 — All tasks with status
   GET /api/wf/{pid}/summary               — Node status counts
   GET /api/wf/{pid}/node/{nid}            — Single node details
   GET /api/wf/{pid}/export?format=json    — Full graph
   GET /api/wf/{pid}/impact?files=a.py     — Impact analysis

3. Memory
   GET /api/mem/{pid}/query                 — All memories
   GET /api/mem/{pid}/query?module=X        — Module-specific
   GET /api/mem/{pid}/query?kind=pitfall    — By type

4. Runtime / Audit
   GET /api/audit/{pid}/log?limit=10        — Recent audit entries
   GET /api/runtime/{pid}                   — Running tasks, queue depth

5. Git / Code (host only, via MCP :40020)
   GET http://localhost:40020/git-status    — HEAD, dirty files

Each response includes generated_at and project_version.
If data seems stale, re-fetch.

IMPORTANT: All data is in governance.db (SQLite) and dbservice.
Do NOT suggest checking log files or filesystem directories.
```

**Coordinator gets additional:**
```
You are the Coordinator. You classify user intent and decide action:
- Question about project → query APIs, reply with data
- Feature request → create PM task
- Bug report → create PM task
- Test request → create test task
- Status check → query APIs, reply with summary

Respond with exactly one JSON: {"action": "reply"|"create_task", ...}
```

#### 3.3.2 Role Query Priority Strategy

Each role follows a stable "what to check first, what to check later" order. This reduces missed context, wasteful API calls, and irrelevant information injection.

##### Universal Rules

```
1. Always read base snapshot (Layer 1) before querying Layer 2
2. Only query APIs relevant to your role's responsibility
3. If base context is sufficient, do NOT expand queries
4. Only do deeper drill-down on: info gaps, context conflicts, blockers, version anomalies, relevant failure patterns
5. Prefer summaries first, details only when needed
6. Do NOT continuously query APIs "just in case"
```

##### Per-Role Priority

**Coordinator** — Dispatches tasks, identifies blockers, ensures flow progress. Does NOT analyze code details.

| Priority | Query | When |
|----------|-------|------|
| P0 (always) | context-snapshot, project_state, task/node summary | Every session |
| P1 (conditional) | runtime/audit summary | Task is stuck |
| P1 (conditional) | version-check | Suspect state anomaly |
| P1 (conditional) | recent decisions memory | Need historical context for dispatch |
| P2 (rarely) | git diff, code details, test result full text | Almost never |

**PM** — Clarifies requirements, outputs PRD, defines acceptance criteria. Does NOT implement.

| Priority | Query | When |
|----------|-------|------|
| P0 (always) | context-snapshot, task details, existing constraints | Every session |
| P0 (always) | recent decisions memory | Every session |
| P1 (conditional) | related module memory | Continuing from existing feature |
| P1 (conditional) | failure_pattern memory | Requirement has failed before |
| P2 (rarely) | git diff, runtime log details, low-level test data | Almost never |

**Dev** — Implements changes, modifies code. Most context-hungry role.

| Priority | Query | When |
|----------|-------|------|
| P0 (always) | context-snapshot, task details, acceptance criteria | Every session |
| P0 (always) | related module decisions memory, project_state | Every session |
| P1 (recommended) | git summary / changed files | Before writing code |
| P1 (recommended) | recent failure_pattern memory | Same module had issues |
| P1 (recommended) | upstream node artifacts | Continuing from another role's output |
| P2 (conditional) | task node details, recent task history, version-check | When implementation unclear |

**Dev rule:** Check acceptance criteria before writing code. Check history before refactoring. Check failure patterns for same module.

**Tester** — Verifies functionality against acceptance criteria. Discovers defects and regressions.

| Priority | Query | When |
|----------|-------|------|
| P0 (always) | context-snapshot, acceptance criteria, recent changed files | Every session |
| P1 (recommended) | failure_pattern memory | Regression risk |
| P1 (recommended) | related decisions memory | Understanding intent |
| P1 (recommended) | upstream artifacts / implementation summary | Knowing what was built |
| P2 (conditional) | runtime/audit summary, version-check | Inconsistency detected |

**Tester rule:** Test against acceptance criteria, not just happy path. Prioritize recent changes. Regress on historical failure patterns.

**QA** — Final verification, process compliance, merge readiness. Focuses on readiness, not re-doing dev/test.

| Priority | Query | When |
|----------|-------|------|
| P0 (always) | context-snapshot, task completion summary, test results | Every session |
| P0 (always) | project_state / version-check | Every session (merge readiness) |
| P1 (recommended) | recent decisions memory, audit summary | Understanding trajectory |
| P1 (recommended) | changed files summary | Scope verification |
| P2 (conditional) | runtime / blockers, git HEAD vs workflow state | Pre-merge anomaly |

**QA rule:** Not just "does it work" but "is the process legal." Must confirm version state before merge approval.

##### Summary Table

| Role | P0 (always) | P1 (conditional) | P2 (rarely) | Never query first |
|------|------------|-------------------|-------------|-------------------|
| **Coordinator** | snapshot / node summary | project_state / blockers | audit / version-check | Code details |
| **PM** | task details / constraints | decisions memory | failure patterns | git diff |
| **Dev** | acceptance / module memory | changed files / failure patterns | node artifacts / version-check | Unrelated audit |
| **Tester** | acceptance / changed files | failure patterns | implementation summary / audit | Bulk history |
| **QA** | completion summary / test results | project_state / version-check | audit / decisions | Code implementation |

This strategy is injected into each role's ROLE_PROMPT as a "Query Guidelines" section.

---

### 3.4 Structured Memory Write on Completion

Current memory_service.py supports free-form entries. This PRD adds a structured schema so memories are queryable knowledge, not log copies.

#### 3.4.1 Memory Entry Schema

```json
{
    "module": "agent/governance/auto_chain.py",
    "kind": "decision | pitfall | test_result | architecture | failure_pattern",
    "content": "Human-readable summary",

    "structured": {
        "decision_type": "bugfix | feature | refactor | config",
        "related_files": ["agent/auto_chain.py", "docs/ai-agent-integration-guide.md"],
        "root_cause": "checkpoint_gate only checked changed_files list, not git diff",
        "validation_status": "tested | untested | partial",
        "failure_pattern": "Gate bypass via fake changed_files",
        "followup_needed": false,
        "task_id": "task-1774567042-b3a09c",
        "chain_stage": "dev"
    }
}
```

Not all fields required. Each task type populates what it knows:

| Field | dev | test | qa |
|-------|-----|------|-----|
| `decision_type` | ✅ inferred from prompt | — | — |
| `related_files` | ✅ from changed_files | ✅ from changed_files | — |
| `root_cause` | ✅ if bugfix | — | ✅ if reject |
| `validation_status` | "untested" | "tested" | — |
| `failure_pattern` | ✅ if gate blocked before | ✅ if tests failed | — |
| `followup_needed` | ✅ if gate blocked | — | ✅ if conditional pass |
| `task_id` | ✅ always | ✅ always | ✅ always |
| `chain_stage` | "dev" | "test" | "qa" |

#### 3.4.2 Executor Memory Write

**File:** `agent/executor_worker.py`

```python
def _write_memory(self, task_type, task_id, result, metadata):
    """Write structured memory after task completion."""
    changed = result.get("changed_files", [])
    summary = result.get("summary", "")

    if task_type == "dev" and (summary or changed):
        # Infer decision_type from keywords
        prompt = metadata.get("original_prompt", summary)
        if any(w in prompt.lower() for w in ("fix", "bug", "error")):
            decision_type = "bugfix"
        elif any(w in prompt.lower() for w in ("add", "new", "create", "implement")):
            decision_type = "feature"
        elif any(w in prompt.lower() for w in ("refactor", "clean", "rename")):
            decision_type = "refactor"
        else:
            decision_type = "config"

        gate_reason = metadata.get("previous_gate_reason", "")

        self._api("POST", f"/api/mem/{self.project_id}/write", {
            "module": changed[0] if changed else "general",
            "kind": "decision",
            "content": summary,
            "structured": {
                "decision_type": decision_type,
                "related_files": changed,
                "validation_status": "untested",
                "failure_pattern": gate_reason if gate_reason else None,
                "followup_needed": bool(gate_reason),
                "task_id": task_id,
                "chain_stage": "dev",
            },
        })

    elif task_type == "test":
        report = result.get("test_report", {})
        passed = report.get("passed", 0)
        failed = report.get("failed", 0)

        self._api("POST", f"/api/mem/{self.project_id}/write", {
            "module": "testing",
            "kind": "test_result" if failed == 0 else "failure_pattern",
            "content": f"{passed} passed, {failed} failed",
            "structured": {
                "related_files": changed,
                "validation_status": "tested" if failed == 0 else "failed",
                "failure_pattern": report.get("error_summary", "") if failed > 0 else None,
                "followup_needed": failed > 0,
                "task_id": task_id,
                "chain_stage": "test",
            },
        })

    elif task_type == "qa" and result.get("recommendation") == "reject":
        self._api("POST", f"/api/mem/{self.project_id}/write", {
            "module": changed[0] if changed else "general",
            "kind": "failure_pattern",
            "content": result.get("review_summary", "QA rejected"),
            "structured": {
                "root_cause": result.get("reject_reason", ""),
                "related_files": changed,
                "followup_needed": True,
                "task_id": task_id,
                "chain_stage": "qa",
            },
        })
```

#### 3.4.3 Memory Query Enhancement

Existing `GET /api/mem/{pid}/query` already supports `module`, `kind`, `node` filters.

Add support for structured field queries (future, not in this PRD):
```
GET /api/mem/{pid}/query?kind=failure_pattern&followup_needed=true
GET /api/mem/{pid}/query?decision_type=bugfix&module=auto_chain.py
```

For now, structured fields are stored as JSON in the entry and returned in full. Filtering is client-side (AI reads and filters in context).

#### 3.4.4 Memory in Context Snapshot

The `recent_memories` in `/api/context-snapshot` selects the 3 most relevant entries:

Priority scoring:
1. `followup_needed=true` → highest priority (unresolved issues)
2. `kind=failure_pattern` → high (avoid repeating mistakes)
3. `kind=decision` with matching module → medium (relevant experience)
4. `kind=test_result` → low (routine)

```python
def _select_relevant_memories(memories, task_prompt, limit=3):
    scored = []
    for m in memories:
        score = 0
        s = m.get("structured", {})
        if s.get("followup_needed"): score += 10
        if m.get("kind") == "failure_pattern": score += 5
        if m.get("kind") == "decision": score += 2
        # Boost if module matches any word in prompt
        if m.get("module", "") in task_prompt: score += 3
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]
```

---

## 4. File Change Summary

| File | Action | Changes |
|------|--------|---------|
| `agent/governance/db.py` | Modify | `project_version` table schema |
| `agent/governance/server.py` | Modify | `/api/version-check/{pid}`, `/api/version-update/{pid}`, `/api/context-snapshot/{pid}`, add `generated_at`+`project_version` to 5 endpoints |
| `agent/governance/project_service.py` | Modify | Init chain_version on create/register |
| `agent/mcp/server.py` | Modify | HTTP :40020 `/git-status` |
| `agent/mcp/tools.py` | Modify | `version_check` MCP tool |
| `agent/telegram_gateway/gateway.py` | Modify | Version gate at message entry |
| `agent/ai_lifecycle.py` | Modify | Fetch `/api/context-snapshot` for base context |
| `agent/role_permissions.py` | Modify | API reference in all ROLE_PROMPTS, coordinator knowledge |
| `agent/executor_worker.py` | Modify | Structured memory write + merge version update |
| `agent/governance/memory_service.py` | Modify | Accept `structured` field in write_memory, store as JSON |
| `Dockerfile.governance` | Verify | Ensure schema migration runs |
| `Dockerfile.telegram-gateway` | Verify | requests library available |

**NOT changed:** `agent/context_assembler.py` — kept for future use but not wired in v6.

---

## 5. Affected Nodes

| Node ID | Title | Current | Action |
|---------|-------|---------|--------|
| L15.1 | AI Session Lifecycle | qa_pass | → testing (snapshot injection) |
| L22.2 | Memory Write | qa_pass | → testing (structured schema + executor writes) |
| L4.11 | Project Service | qa_pass | → testing (init version) |
| L4.15 | Governance Server | qa_pass | → testing (new endpoints) |
| L11.1 | Gateway Message Classifier | qa_pass | → testing (version gate) |

---

## 6. Documentation Updates

| Document | Section | Change |
|----------|---------|--------|
| `docs/architecture-v6-executor-driven.md` | Context Model | New: two-tier context (snapshot + self-service) |
| `docs/architecture-v6-executor-driven.md` | Version Gate | New: multi-project version, anti-tamper, consistency |
| `docs/ai-agent-integration-guide.md` | Role Context | Table: base context fields per role |
| `docs/ai-agent-integration-guide.md` | Self-Service APIs | 5-category API reference for agents |
| `docs/ai-agent-integration-guide.md` | Version Gate | Usage + fail-open + consistency model |
| `README.md` | Architecture | Add MCP :40020, context-snapshot endpoint |
| `README.md` | API Reference | Add version-check, version-update, context-snapshot |

---

## 7. Verification

| # | Scenario | Expected |
|---|----------|----------|
| 1 | Manual commit → Telegram message | "N manual commits", no task |
| 2 | Dirty working tree → Telegram message | "N uncommitted files", no task |
| 3 | Auto-merge → Telegram message | Normal coordinator flow |
| 4 | MCP down → Telegram message | Fail-open, proceeds |
| 5 | AI calls /api/version-update directly | Rejected: INVALID_TOKEN (no internal token) |
| 5a | Script calls with token but wrong stage | Rejected: INVALID_CHAIN_STAGE |
| 5b | Auto-chain merge calls with valid token+stage | Accepted, audit written |
| 5c | Stale old_version in request | Rejected: OLD_VERSION_MISMATCH |
| 6 | New project /api/init | project_version initialized |
| 7 | Coordinator "check audit" | Queries /api/audit, returns DB data |
| 8 | Dev session starts | Base snapshot in system prompt (~500 token) |
| 9 | Dev needs node details | Calls /api/wf/{pid}/node/{nid} on demand |
| 10 | Dev completes → query memory | New entry found |
| 11 | Two APIs called 5s apart | Both have generated_at + project_version |
| 12 | version_check MCP tool | Observer sees ok/dirty/commits |

---

## 8. Acceptance Criteria

- [ ] `project_version` table: per-project chain_version in governance.db
- [ ] `/api/init` and `/api/projects/register` initialize chain_version
- [ ] MCP :40020 `/git-status` returns HEAD + dirty
- [ ] `version_check` MCP tool available
- [ ] `/api/version-check/{pid}` combines DB + git
- [ ] `/api/version-update/{pid}` 5-step validation: internal token + fields + lifecycle + version + audit
- [ ] `/api/version-update/{pid}` rejects: INVALID_TOKEN, INVALID_CHAIN_STAGE, TASK_NOT_COMPLETED, OLD_VERSION_MISMATCH, NEW_VERSION_NOT_GIT_HEAD
- [ ] Every version-update call (success or reject) writes audit record
- [ ] Gateway blocks on version/dirty mismatch (0 token)
- [ ] Gateway fail-open on MCP unavailable
- [ ] `/api/context-snapshot/{pid}` returns consistent base context
- [ ] `generated_at` + `project_version` on 5 key API responses
- [ ] ai_lifecycle injects snapshot into system prompt
- [ ] All ROLE_PROMPTS include 5-category API reference
- [ ] All ROLE_PROMPTS include "Query Guidelines" with P0/P1/P2 priority per role
- [ ] Coordinator prompt includes classification instructions
- [ ] Dev/test write memory on completion
- [ ] Merge updates project_version
- [ ] 5 affected nodes updated
- [ ] 7 documentation sections updated

---

## 9. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| MCP crash blocks Telegram | High | Fail-open in gateway |
| Snapshot API slow | Medium | 5s timeout, proceed without if fails |
| Memory write fails | Low | Best-effort, log warning |
| AI ignores API timestamps | Low | Base snapshot guarantees startup consistency |
| Token budget for base context | Low | Snapshot is ~500 tokens, well within limits |
| Multi-project version divergence | Medium | Per-project independent, no cross-project dependency |
