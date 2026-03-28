# Design Specification: AI Agent Memory + Coordinator + Executor

## 1. Purpose

This specification defines the engineering standards for three core capabilities:

- Memory backend abstraction and data consistency
- ref_id semantic anchor mechanism
- Coordinator task awareness and conflict governance
- Executor lifecycle and recovery

The goal is not just "make it work" but ensure:
- Replaceable memory backends
- Stable semantic recall + relationship queries
- Explainable task conflict governance
- Recoverable executor operation

---

## 2. Design Principles

### 2.1 Single Responsibility

| Module | Responsibility | NOT responsible for |
|--------|---------------|-------------------|
| mem0 / semantic layer | Semantic recall, fuzzy search, related object discovery | State truth, decisions |
| SQLite / relational layer | Complete data, relationships, versions, status, audit | Fuzzy matching |
| Coordinator | Intent analysis, task governance, routing | Code execution |
| Executor | Task execution, status reporting, crash recovery | Business decisions |

### 2.2 Relational Data is Source of Truth

**SQLite structured relational data is the source of truth.**

Semantic layers (mem0, FTS5, embedding service) only help FIND related objects — they cannot serve as final state judgment.

- Task current status → SQLite authoritative
- Whether an object is superseded → SQLite authoritative
- Latest decision for a module → SQLite aggregate authoritative

### 2.3 Semantic Recall and Object Read Must Be Separated

**Prohibited:** AI directly depends on semantic layer's text fragments for final judgment.

```
✅ query → semantic recall → ref_id list → SQLite fetch full object → AI decide
❌ query → vector snippets → AI guesses complete state
```

### 2.4 Degrade Before Interrupt

When semantic backend, index, or cloud service is unavailable:

- mem0 unavailable → fall back to local FTS5 / keyword search
- Knowledge index write fails → primary record preserved, async index retry
- Executor restart fails → enter open circuit + alert, not silent failure

---

## 3. Core Object Definitions

### 3.1 memory_id

Primary key of a single memory record.

**Rules:**
- Every new memory write generates a new memory_id
- memory_id is never reused
- Used for: audit, version tracking, single record delete/recover

### 3.2 ref_id

**ref_id is a stable semantic anchor used by the semantic layer to map recall results back to complete relational entities in SQLite.**

This is the most important definition in this specification.

**Rules:**
- ref_id is NOT a single memory's primary key
- ref_id is NOT a temporary tokenization result
- ref_id must be stable, repeatedly referenceable, fixed granularity
- Multiple memory records can share the same ref_id
- Semantic layer's core return should be ref_id lists, not long text fragments

**Valid ref_id targets (pick one granularity per object, keep consistent):**
- A task entity
- A node entity
- A design decision unit
- A failure pattern unit
- A documentation knowledge unit

**Prohibited:**
- Same ref_id means "task" today, "step" tomorrow, "summary" next week
- Same business object frequently generates new ref_ids
- Using ref_id as a one-time search token

### 3.3 entity_id

Existing business object primary keys:

| Entity | ID |
|--------|-----|
| Task | task_id |
| Node | node_id |
| Document | doc_id |
| Decision | decision_id |

**Relationship:**

```
entity_id  ←→  ref_id (usually 1:1, stable mapping)
ref_id     ←→  memory_id (1:N, version chain)
```

---

## 4. Memory Data Model

### 4.1 Schema

```sql
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL,
    entity_id TEXT DEFAULT '',
    kind TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',  -- active / superseded / inactive / archived
    superseded_by_memory_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    index_status TEXT DEFAULT 'pending',  -- pending / indexed / failed
    index_error TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_ref ON memories(ref_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_entity ON memories(entity_id);
CREATE INDEX IF NOT EXISTS idx_mem_module ON memories(project_id, module_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(project_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_index ON memories(index_status);

-- FTS5 for local keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, module_id, kind, summary,
    content=memories, content_rowid=rowid
);
```

### 4.2 kind Enumeration (Fixed Set)

| kind | Description | Example |
|------|-------------|---------|
| `fact` | Verified factual statement | "governance.db uses WAL mode" |
| `summary` | Aggregated summary of multiple events | "Session completed 3 auto-merges" |
| `decision` | Design or implementation decision | "Use SQLite as source of truth" |
| `failure_pattern` | Known failure mode with root cause | "Direct sqlite3 access causes WAL lock" |
| `task_result` | Outcome of a completed task | "247 tests passed, 0 failed" |
| `task_snapshot` | Point-in-time task state capture | "Task-xxx at QA stage, pending review" |
| `module_note` | Module-specific knowledge | "auto_chain.py has 5 gate functions" |
| `rule` | System constraint or policy | "Observer must not bypass auto-chain" |
| `audit_event` | Significant system event | "Version gate blocked 3 tasks" |

**Prohibited:** Free-text kind values. All kind values must be from this list.

### 4.3 Version Chain

**Rules:**
- Modifying existing knowledge creates a new memory_id, not overwrite
- New version links to old via `superseded_by_memory_id`
- Default query returns "latest active version per ref_id"

```
ref_id: "decision:memory-backend"
  ├── memory_id: mem-001 (v1, status=superseded)
  ├── memory_id: mem-005 (v2, status=superseded)
  └── memory_id: mem-012 (v3, status=active)  ← Coordinator sees this
```

---

## 5. Memory Write Rules

### 5.1 Write Order

```
1. Generate memory_id (UUID)
2. Determine ref_id (stable anchor — create or reuse)
3. Write SQLite memories table (MUST succeed)
4. Update FTS5 index (MUST succeed — same transaction)
5. Write mem0 / knowledge store (MAY fail — async retry)
6. Update index_status
```

### 5.2 Consistency Rules

**Strong consistency (must succeed or rollback):**
- memory_id generation
- SQLite primary record
- FTS5 index update
- Basic field validation

**Weak consistency (may fail, must be trackable + recoverable):**
- mem0 upsert → `index_status = failed`, `index_error = "..."`
- Vector index → async retry job
- Knowledge store upsert → best-effort with tracking

### 5.3 ref_id Creation vs Reuse

**Create new ref_id when:**
- First time creating a new business object
- New knowledge unit with clearly different granularity

**Reuse existing ref_id when:**
- Same task's subsequent status updates
- Same design decision's revision
- Same module note's supplement
- Same failure pattern's new case summary

**Prohibited:**
- New ref_id just because text content changed
- Every write() defaults to new ref_id

---

## 6. Search / Recall Rules

### 6.1 Responsibilities

| Layer | Does | Does NOT |
|-------|------|----------|
| Semantic (mem0/FTS5) | Query expansion, fuzzy match, return ref_id list | Return authoritative state |
| SQLite | Aggregate by ref_id, return latest version, include status/relations | Do fuzzy matching |

### 6.2 Unified Return Structure

All search backends must return:

```json
{
  "ref_id": "decision:memory-backend-routing",
  "score": 0.82,
  "score_type": "vector",       // or "bm25" for FTS5
  "search_mode": "semantic",    // or "lexical"
  "matched_text": "Use SQLite as source of truth...",
  "metadata": {
    "kind": "decision",
    "module_id": "memory",
    "entity_id": "decision-001",
    "status": "active",
    "version": 3
  }
}
```

### 6.3 Backend Difference Must Be Explicit

Coordinator must know whether a match is semantic or keyword:

```
score_type = bm25    → keyword match, may miss synonyms
score_type = vector  → semantic match, may have false positives
```

### 6.4 Aggregation Before Coordinator

Raw search results must be aggregated before feeding to Coordinator:

1. Group by ref_id
2. Only take active/latest version
3. Include summary/status/related entities
4. Sort by relevance score

---

## 7. Coordinator Task Governance

### 7.1 Coordinator Input Requirements

Before making any task decision, Coordinator must have:

| Input | Source | Required |
|-------|--------|----------|
| User request | Telegram message | ✅ |
| Memory recall results | `/api/mem/search` → ref_id → SQLite fetch | ✅ |
| Active task list | `/api/task/list?status=queued,claimed` | ✅ |
| Recent completed tasks | `/api/task/list` (last 5 succeeded) | Recommended |
| Current module state | `/api/wf/summary` | Recommended |
| Task dependencies | metadata.depends_on | If available |

### 7.2 Task Metadata Standard

Every task should include:

```json
{
  "task_id": "task-xxx",
  "title": "Add hello function",
  "intent_summary": "User wants to add a hello world function to utils.py",
  "target_modules": ["agent/utils.py"],
  "target_files": ["agent/utils.py"],
  "operation_type": "add",         // add / modify / delete / refactor / test
  "risk_level": "low",             // low / medium / high
  "depends_on": [],
  "source_message_hash": "sha256:...",
  "status": "queued"
}
```

### 7.3 Conflict Detection: Rules First, AI Assists

**Rule layer (code logic, 0 tokens):**

| Rule | Condition | Decision |
|------|-----------|----------|
| Same file + opposite operation | delete vs add/update on same file | `conflict` |
| Same module + concurrent refactor | two refactoring tasks on same module | `conflict` |
| Duplicate task within window | same intent_summary hash within 1 hour | `duplicate` |
| Upstream dependency not done | depends_on task not succeeded | `queue` |
| Same failure + no fix | failure_pattern.followup_needed and same module | `block` or `retry` |

**AI layer (only after rules):**
- Synthesize rule results into natural language
- Generate user-facing explanation
- Suggest resolution options

### 7.4 Coordinator Decision Enumeration (Fixed Set)

| Decision | Meaning | Action |
|----------|---------|--------|
| `new` | No conflicts, create task | `create_task` |
| `duplicate` | Similar task already done | `reply` asking if redo |
| `conflict` | Contradicting task in queue | `reply` with options |
| `queue` | Same module busy, queue behind | `create_task` with lower priority |
| `retry` | Past failure, retry with context | `create_task` with failure info |
| `merge` | Can be combined with existing task | `reply` suggesting merge |
| `block` | Cannot proceed, needs resolution | `reply` explaining blocker |

---

## 8. Executor Lifecycle

### 8.1 Core Responsibilities

| Does | Does NOT |
|------|----------|
| Claim tasks | Make product decisions |
| Execute via Claude CLI | Override Coordinator decisions |
| Maintain heartbeat | Directly access other projects |
| Report status | Write to files outside worktree |
| Crash recovery | Run without PID lock |

### 8.2 Single Instance Rule

One active executor per project/queue. Enforced by:

1. **File lock** (OS-level, primary)
2. **PID file** (supplementary info)
3. Kill old process only if: same project + health check failed + timeout exceeded

### 8.3 Heartbeat

After claiming a task, executor must periodically update:

| Field | Update Interval |
|-------|----------------|
| `heartbeat_at` | Every 30 seconds |
| `claimed_by` | On claim |
| `claimed_at` | On claim |

**Timeout detection:** If `heartbeat_at` > 120 seconds old → task is considered stuck.

### 8.4 Crash Recovery

**On executor startup, scan for orphaned tasks:**

```python
def _recover_stuck_tasks(self):
    """Find claimed tasks with stale heartbeat and recover them."""
    stuck = self._api("GET", f"/api/task/{pid}/list?status=claimed")
    for task in stuck["tasks"]:
        if task["heartbeat_at"] and is_stale(task["heartbeat_at"], timeout=120):
            # Option A: reset to queued (will be re-claimed)
            self._api("POST", f"/api/task/{pid}/recover", {
                "task_id": task["task_id"],
                "action": "requeue",
                "reason": "executor_crash_recovery"
            })
            # Write audit event
```

**Prohibited:**
- Only restart process without handling orphaned task states
- Let tasks permanently stuck in `claimed`

### 8.5 Circuit Breaker

```
Thresholds:
  max_restarts = 5
  restart_window = 300 seconds

States:
  CLOSED → normal operation
  OPEN → restart limit reached, stop auto-restart, send alert
  HALF_OPEN → after cooldown, try one restart

Transitions:
  crash + restarts < max → CLOSED (restart)
  crash + restarts >= max → OPEN (alert, stop)
  stable for restart_window → reset restart count
```

### 8.6 Pycache Cleanup

Clear `__pycache__` at these points:

| When | Why |
|------|-----|
| Before every executor restart | Prevent stale bytecode after merge |
| After deploy_chain merge | New code needs fresh compilation |
| On `executor_scale(0 → 1)` | Clean start |

### 8.7 Status Exposure

```json
{
  "pid": 12345,
  "running": true,
  "uptime_s": 3600,
  "active_tasks": 1,
  "queued_tasks": 3,
  "restart_count": 2,
  "last_crash_at": "2026-03-28T01:00:00Z",
  "health": "healthy",          // healthy / degraded / crash_loop
  "circuit_breaker": "closed",  // closed / open / half_open
  "pycache_cleared": true
}
```

---

## 9. Deletion / Supersede Rules

### 9.1 Prefer Soft Delete

Memory and knowledge objects should prefer:
- `status = inactive`
- `status = archived`
- `status = superseded`

Over physical deletion.

### 9.2 Search Filtering

Default search results must prioritize:
- `status = active`
- Latest version per ref_id
- Non-archived

Old versions available in audit view only, not in Coordinator main context.

---

## 10. Observability

### 10.1 Memory Metrics

| Metric | Type |
|--------|------|
| `memory.write.success` | Counter |
| `memory.write.failure` | Counter |
| `memory.index.pending` | Gauge |
| `memory.index.failed` | Gauge |
| `memory.search.latency_ms` | Histogram |
| `memory.backend.available` | Boolean |

### 10.2 Coordinator Metrics

| Metric | Type |
|--------|------|
| `coordinator.decision.{type}` | Counter (new/duplicate/conflict/...) |
| `coordinator.rule_hit_rate` | Percentage |
| `coordinator.ai_override_rate` | Percentage |

### 10.3 Executor Metrics

| Metric | Type |
|--------|------|
| `executor.uptime_s` | Gauge |
| `executor.restart_count` | Counter |
| `executor.crash_count` | Counter |
| `executor.recovery_count` | Counter |
| `executor.stuck_tasks` | Gauge |
| `executor.circuit_breaker` | Enum |

---

## 11. Implementation Order

| Phase | Scope | Depends On |
|-------|-------|------------|
| **Phase 1** | Executor lifecycle + heartbeat + task recovery | None |
| **Phase 2** | SQLite memory schema + local FTS5 + index_status | None |
| **Phase 3** | ref_id lifecycle + entity mapping + latest-version query | Phase 2 |
| **Phase 4** | Task metadata + rule-based conflict precheck | Phase 1 |
| **Phase 5** | Coordinator awareness + AI decision layer | Phase 3, 4 |
| **Phase 6** | Docker mem0 backend / semantic backend | Phase 2 |
| **Phase 7** | Cloud backend stub | Phase 2 |

Phase 1 and 2 are independent and can run in parallel.

---

## 12. Summary

The core principle compressed into one sentence:

**Use ref_id to connect semantic recall with relational truth, use a rule layer to harden task governance, use a recoverable Executor to stabilize system operation.**

Five invariants that determine system stability:

1. ref_id granularity must be stable
2. SQLite must be source of truth
3. Derived index failures must be compensable
4. Task conflict detection must be rules-first
5. Executor crash must trigger task recovery

These invariants, once fixed, move the system from "good idea" to "real platform skeleton."
