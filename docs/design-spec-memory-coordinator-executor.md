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

Aligned with existing dbservice knowledgeStore + memorySchema + memoryRelations.

```sql
-- Primary memory storage
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL,
    entity_id TEXT DEFAULT '',
    kind TEXT NOT NULL,
    sub_kind TEXT DEFAULT '',
    module_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',          -- project_id or 'global' for cross-project
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    tags TEXT DEFAULT '[]',                  -- JSON array for flexible tagging
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',            -- active / superseded / inactive / archived / candidate
    superseded_by_memory_id TEXT DEFAULT NULL,
    -- Write classification (aligned with dbservice memorySchema)
    write_class TEXT DEFAULT 'explicit',     -- explicit / inferred / candidate / transient
    durability TEXT DEFAULT 'durable',       -- permanent / durable / session / transient
    source_type TEXT DEFAULT 'system_extracted', -- user_explicit / assistant_inferred / system_extracted / imported
    confidence REAL DEFAULT 1.0,             -- 0.0-1.0, for inferred/candidate memories
    -- Conflict handling
    conflict_policy TEXT DEFAULT 'replace',  -- replace / append / append_set / temporal_replace / merge_object
    -- Lifecycle
    ttl INTEGER DEFAULT 0,                   -- 0 = no expiry, >0 = seconds until auto-archive
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Index tracking (for async semantic index retry)
    index_status TEXT DEFAULT 'pending',     -- pending / indexed / failed
    index_error TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_ref ON memories(ref_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_entity ON memories(entity_id);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_module ON memories(scope, module_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(scope, kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_index ON memories(index_status);

-- FTS5 for local keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, module_id, kind, summary,
    content=memories, content_rowid=rowid
);

-- FTS5 sync triggers (INSERT + UPDATE + DELETE)
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, module_id, kind, summary)
    VALUES (new.rowid, new.content, new.module_id, new.kind, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE OF content, summary, status ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.rowid;
    INSERT INTO memories_fts(rowid, content, module_id, kind, summary)
    VALUES (new.rowid, new.content, new.module_id, new.kind, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.rowid;
END;

-- Note: when status changes to 'superseded'/'archived'/'inactive',
-- the UPDATE trigger re-inserts into FTS. Search queries MUST filter
-- by status='active' to avoid returning stale results.
-- Recommended: FTS query wrapper always adds "AND status='active'".

-- Memory relations (graph structure between ref_ids)
-- Aligned with dbservice memoryRelations.js
CREATE TABLE IF NOT EXISTS memory_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_ref_id TEXT NOT NULL,
    relation TEXT NOT NULL,         -- e.g. 'DEPENDS_ON', 'SUPERSEDES', 'RELATED_TO', 'CAUSED_BY'
    to_ref_id TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON memory_relations(from_ref_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON memory_relations(to_ref_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON memory_relations(relation);

-- Memory events (append-only event log)
-- Aligned with dbservice memoryRelations.js memory_events
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,        -- e.g. 'created', 'superseded', 'promoted', 'archived'
    actor_id TEXT DEFAULT '',        -- who triggered: 'executor', 'coordinator', 'observer'
    detail TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evt_ref ON memory_events(ref_id);
CREATE INDEX IF NOT EXISTS idx_evt_type ON memory_events(event_type);
```

### 4.2 kind Enumeration (Fixed Set)

Aligned with dbservice DomainPack 'development' registration:

| kind | Durability | Conflict Policy | Description | dbservice equivalent |
|------|-----------|-----------------|-------------|---------------------|
| `fact` | permanent | replace | Verified factual statement | `knowledge` |
| `summary` | durable | replace | Aggregated summary | — (new) |
| `decision` | permanent | append | Design/implementation decision | `decision` |
| `failure_pattern` | permanent | append | Known failure with root cause | `pitfall` |
| `task_result` | durable | replace | Outcome of completed task | `task_state` |
| `task_snapshot` | session | replace | Point-in-time task state | `task_state` |
| `module_note` | durable | replace | Module-specific knowledge | `knowledge` |
| `rule` | permanent | append_set | System constraint or policy | `constraint` |
| `audit_event` | permanent | append | Significant system event | — (new) |
| `architecture` | permanent | replace | Architecture decisions | `architecture` |
| `pattern` | permanent | replace | Code patterns | `pattern` |

**Durability levels** (from dbservice memorySchema):
- `permanent` — never auto-expires, explicit archive only
- `durable` — long-lived, survives session restarts
- `session` — cleared when session ends
- `transient` — auto-expires after ttl seconds

**Conflict policies** (from dbservice memorySchema):
- `replace` — new content overwrites old
- `append` — new content appended to old
- `append_set` — dedup merge (comma-separated unique values)
- `temporal_replace` — newer timestamp wins
- `merge_object` — JSON deep merge

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

### 4.4 Scope: Multi-Project Isolation and Sharing

Aligned with dbservice `scope` field.

**Scope values:**

| scope | Meaning | Visibility |
|-------|---------|-----------|
| `aming-claw` | Project-specific memory | Only this project's coordinator/agents |
| `global` | Cross-project shared memory | All projects |
| `toolbox` | Another project's memory | Only toolbox project |

**Default write scope:** `scope = project_id` (project-private).

**Sharing mechanism — Promote API:**

```
POST /api/mem/{pid}/promote
{
    "memory_id": "mem-012",
    "target_scope": "global",
    "reason": "Generic pitfall applicable to all projects"
}
```

Promote creates a **copy** with `scope=global`, original stays project-scoped.
Both share the same `ref_id` but different `memory_id`.

**Query scope resolution:**

```
Coordinator query for project "aming-claw":
  1. scope = "aming-claw" (project-specific) — priority
  2. scope = "global" (cross-project) — supplementary

  Combined, deduped by ref_id, project-scope wins on conflict.
```

**Reusable kinds (candidates for promote to global):**

| kind | Reusable? | Why |
|------|-----------|-----|
| `failure_pattern` | ✅ High | "SQLite WAL lock" applies to any project using SQLite |
| `architecture` | ✅ High | General patterns (e.g., "semantic recall + SQLite truth") |
| `pattern` | ✅ High | Code patterns work across projects |
| `rule` | ⚠️ Sometimes | Some rules are project-specific, some universal |
| `decision` | ⚠️ Sometimes | Project-specific decisions usually, but design philosophy can be shared |
| `task_result` | ❌ No | Tied to specific project task |
| `task_snapshot` | ❌ No | Tied to specific project state |
| `audit_event` | ❌ No | Tied to specific project timeline |

**DomainPack per project:**

Each project can register its own DomainPack via:
```
POST /api/mem/{pid}/register-pack
{
    "domain": "development",
    "types": {
        "architecture": { "durability": "permanent", "conflictPolicy": "replace" },
        "pitfall": { "durability": "permanent", "conflictPolicy": "append" },
        ...
    }
}
```

This maps to dbservice's `registerDomainPack`. Default 'development' pack auto-registered on project init.

### 4.5 Memory Relations (Graph)

Relations between ref_ids enable traversal queries:

```
Example relations:
  task-001 --PRODUCED--> decision:use-sqlite
  decision:use-sqlite --CAUSED_BY--> failure:json-file-corruption
  failure:wal-lock --RELATED_TO--> failure:db-timeout
  node:L22.2 --VERIFIED_BY--> task_result:test-247-pass
```

**Standard relation types:**

| Relation | Meaning | Example |
|----------|---------|---------|
| `PRODUCED` | Task created this knowledge | task → decision |
| `CAUSED_BY` | Root cause link | failure → another failure |
| `RELATED_TO` | Semantic relation | failure ↔ failure |
| `VERIFIED_BY` | Evidence link | node → test_result |
| `DEPENDS_ON` | Prerequisite | task → task |
| `SUPERSEDES` | Version replacement | decision_v2 → decision_v1 |

**Query via relations:**

```
GET /api/mem/{pid}/expand?ref_id=task-001&depth=2
→ Returns task-001 and all ref_ids within 2 hops
→ Coordinator can see: "task-001 produced decision X which was caused by failure Y"
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

### 7.0 Intent Classification (Pre-Coordinator Gate)

Not every user message needs a Coordinator AI session. Before creating any coordinator task, gateway must classify intent using code logic (0 tokens):

| Intent | Detection | Action | Token Cost |
|--------|-----------|--------|-----------|
| **greeting** | Keywords: 你好, hi, hello, thanks, 谢谢, ok | Direct reply, no task | 0 |
| **status_query** | Keywords: 状态, status, 进度, 节点, 多少 | Gateway queries API, replies | 0 |
| **command** | Starts with `/` | Existing handler | 0 |
| **dangerous** | Keywords: delete, rollback, 删除, deploy | Confirmation flow | 0 |
| **task_intent** | Keywords: 帮我, 修, 写, 改, fix, add, build | Create coordinator task | ~500-2000 |
| **ambiguous** | No keyword match | Create coordinator task (safe default) | ~500-2000 |

**Principle:** Only create a coordinator task when AI judgment is genuinely needed. Greetings, status checks, and commands should never spawn a Claude CLI session.

**Gateway implementation location:** `handle_message()` in gateway.py, before `handle_task_dispatch()`.

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

Every task MUST include these fields. Coordinator is responsible for extracting them
from user message before creating subtasks. Missing mandatory fields = gate rejection.

```json
{
  "task_id": "task-xxx",
  "title": "Add hello function",
  "intent_summary": "User wants to add a hello world function to utils.py",
  "target_modules": ["agent/utils.py"],
  "target_files": ["agent/utils.py"],
  "operation_type": "add",
  "risk_level": "low",
  "depends_on": [],
  "source_message_hash": "sha256:...",
  "status": "queued"
}
```

**Field requirements:**

| Field | Required | Extracted By | Fallback |
|-------|----------|-------------|----------|
| `intent_summary` | **Mandatory** | Coordinator AI | = original prompt |
| `target_files` | **Mandatory** | Coordinator AI or PM | gate blocks if empty |
| `target_modules` | Recommended | Derived from target_files | directory of target_files |
| `operation_type` | **Mandatory** | Coordinator rule engine | keyword match: add/modify/delete/refactor/test |
| `source_message_hash` | **Mandatory** | Gateway (auto-generated) | sha256 of user message text |
| `risk_level` | Recommended | Coordinator AI | "medium" default |
| `depends_on` | Optional | Coordinator if detected | [] |

**operation_type extraction rules (code logic in gateway/coordinator):**

```python
OP_KEYWORDS = {
    "add":      ["添加", "新增", "创建", "实现", "add", "create", "implement", "new"],
    "modify":   ["修改", "更新", "优化", "改", "update", "modify", "optimize", "improve"],
    "delete":   ["删除", "移除", "去掉", "delete", "remove", "drop"],
    "refactor": ["重构", "重写", "迁移", "refactor", "rewrite", "migrate"],
    "test":     ["测试", "验证", "检查", "test", "verify", "check"],
}
```

**Validation gate:** Post-PM gate MUST reject tasks with empty `target_files` or `intent_summary`.

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

## 12. Verification Script

Each invariant must be provable. Implementation MUST include automated verification:

```python
# scripts/verify_spec.py — Run after each phase to prove invariants hold

def test_memory_version_chain():
    """Same ref_id, multiple writes → query returns only latest active."""
    write(ref_id="test:vc", content="v1", version=1)
    write(ref_id="test:vc", content="v2", version=2, supersedes="v1_memory_id")
    results = query(ref_id="test:vc")
    assert len(results) == 1
    assert results[0]["content"] == "v2"
    assert results[0]["status"] == "active"

def test_fts_excludes_superseded():
    """FTS search must not return superseded/archived memories."""
    write(ref_id="test:fts", content="hello world", status="active")
    write(ref_id="test:fts2", content="hello earth", status="superseded")
    results = search("hello")
    assert all(r["status"] == "active" for r in results)

def test_semantic_fallback():
    """mem0 unavailable → automatic fallback to FTS5."""
    # Simulate: DBSERVICE_URL points to dead host
    os.environ["DBSERVICE_URL"] = "http://localhost:99999"
    results = search("test query")  # Should not raise, should use FTS5
    assert results is not None  # May be empty but not error
    os.environ.pop("DBSERVICE_URL")

def test_executor_crash_recovery():
    """Claimed task with stale heartbeat → requeued on executor startup."""
    # Create task, mark as claimed with old heartbeat
    task = create_task(prompt="test")
    claim_task(task["task_id"])
    set_heartbeat(task["task_id"], stale=True)  # 5 min ago
    # Restart executor
    executor.recover_stuck_tasks()
    status = get_task(task["task_id"])["status"]
    assert status in ("queued", "failed")  # Not still "claimed"

def test_conflict_rule_same_file_opposite_op():
    """Same file + opposite operation → rule engine returns 'conflict'."""
    # Task in queue: add hello to utils.py
    create_task(prompt="add hello", target_files=["utils.py"], operation_type="add")
    # New request: delete hello from utils.py
    decision = rule_engine.check(
        target_files=["utils.py"], operation_type="delete")
    assert decision == "conflict"

def test_duplicate_detection():
    """Same intent within 1 hour → rule engine returns 'duplicate'."""
    create_task(prompt="add hello function", source_message_hash="abc123")
    decision = rule_engine.check(source_message_hash="abc123")
    assert decision == "duplicate"

def test_status_query_no_coordinator():
    """Status query ('当前状态') must NOT create coordinator task."""
    initial_count = count_tasks(type="coordinator")
    gateway.handle_message(chat_id=123, text="当前状态怎么样")
    after_count = count_tasks(type="coordinator")
    assert after_count == initial_count  # No new coordinator task

def test_scope_isolation():
    """Project A memory not visible to Project B query."""
    write(scope="project-a", content="secret")
    results = query(scope="project-b")
    assert not any("secret" in r["content"] for r in results)

def test_scope_global_sharing():
    """Promoted memory visible to all projects."""
    write(scope="project-a", content="universal pitfall", ref_id="pit:1")
    promote(memory_id="...", target_scope="global")
    results = query(scope="project-b")  # Should include global
    # query resolver should merge project-b + global
    assert any("universal pitfall" in r["content"] for r in results)

def test_ref_id_stability():
    """Same task's updates reuse ref_id, don't create new ones."""
    write(ref_id="task:001", content="started")
    write(ref_id="task:001", content="completed")
    all_refs = set(r["ref_id"] for r in query_all())
    assert all_refs.count("task:001") == 1  # ref_id not duplicated
```

**When to run:** After each Phase implementation, before marking Phase complete.
**CI integration:** Add to `pytest` test suite as `test_spec_invariants.py`.

---

## 13. Summary

The core principle compressed into one sentence:

**Use ref_id to connect semantic recall with relational truth, use a rule layer to harden task governance, use a recoverable Executor to stabilize system operation.**

Six invariants that determine system stability:

1. ref_id granularity must be stable
2. SQLite must be source of truth
3. Derived index failures must be compensable
4. Task conflict detection must be rules-first
5. Executor crash must trigger task recovery
6. Task metadata (intent_summary, target_files, operation_type) must be mandatory

Each invariant has a corresponding verification test in §12.

These invariants, once fixed, move the system from "good idea" to "real platform skeleton."
