# PRD: Pluggable Memory + Coordinator Task Awareness + Executor Lifecycle

> **Design Specification:** This PRD implements the standards defined in
> [design-spec-memory-coordinator-executor.md](design-spec-memory-coordinator-executor.md).
> All implementation must comply with the spec's invariants:
> 1. ref_id granularity must be stable (§3.2)
> 2. SQLite is source of truth, semantic layer is recall only (§2.2, §2.3)
> 3. Derived index failures must be compensable (§5.2)
> 4. Task conflict detection is rules-first, AI-assists (§7.3)
> 5. Executor crash must trigger task recovery (§8.4)

## 1. Overview

Three interconnected improvements to make the workflow system production-ready:

1. **Pluggable Memory Backend** — Switch between local/docker/cloud memory storage
2. **Coordinator Task Awareness** — Semantic search + queue conflict detection before creating tasks
3. **Executor Lifecycle Management** — Crash recovery, pycache cleanup, health monitoring

---

## 2. Pluggable Memory Backend

### 2.1 Problem

- memory_service.py writes JSON files (no search capability)
- dbservice (Docker) has mem0 semantic search but is tightly coupled
- Going Docker-less loses semantic search
- Future cloud service needs same interface
- Governance and gateway currently run inside Docker — need to support
  Docker-free deployment to lower adoption cost for other users

### 2.2 Architecture

Two-tier design: **LocalBackend** for zero-dependency mode (no Docker, no network),
**RemoteBackend** for any HTTP-based memory service (Docker dbservice, cloud, or
third-party). Nginx acts as the routing layer for remote backends — switching
from Docker to cloud is a nginx upstream change, zero code modification.

```
memory_service.py
    │
    ├── MemoryBackend (abstract interface)
    │       ├── write(entry) → store structured data + semantic index
    │       ├── search(query, top_k) → semantic search results
    │       ├── query(module, kind, ref_id) → structured query
    │       └── delete(memory_id) → remove
    │
    ├── LocalBackend (default, zero dependency, no Docker required)
    │       ├── SQLite `memories` table (structured, source of truth)
    │       └── SQLite FTS5 (full-text search, keyword matching)
    │       └── Runs in-process, no network calls
    │
    └── RemoteBackend (Docker dbservice / cloud / third-party)
            ├── SQLite `memories` table (structured, local cache)
            └── HTTP via nginx reverse proxy:
                │
                ├── nginx upstream: dbservice:40002 (Docker mode)
                │     ├── /store → mem0 Memory.add (vector index)
                │     ├── /search → mem0 Memory.search (semantic)
                │     └── /knowledge/upsert → knowledge store
                │
                └── nginx upstream: cloud-api (cloud mode, future)
                      └── same endpoint contract, different upstream
```

**Key design decision:** RemoteBackend code never changes when switching
Docker↔Cloud. Only nginx upstream configuration changes.

**Docker-free deployment path:** When governance and gateway move out of Docker
(Phase 10), the system runs with `MEMORY_BACKEND=local` by default. Users who
want semantic search can either:
1. Run dbservice standalone (`node dbservice/index.js`) + nginx
2. Use a cloud memory service (future Phase 9)

### 2.3 Configuration

```bash
# Environment variable selects backend
MEMORY_BACKEND=local    # Default: SQLite + FTS5, zero dependency, no Docker
MEMORY_BACKEND=remote   # HTTP-based backend via nginx (Docker dbservice or cloud)

# Remote backend config (nginx routes to actual upstream)
MEMORY_SERVICE_URL=http://localhost:40000/memory  # nginx reverse proxy entry point
                                                  # nginx decides upstream:
                                                  #   Docker → dbservice:40002
                                                  #   Cloud  → cloud-api.xxx:443

# Legacy direct access (still works, not recommended for new code)
# DBSERVICE_URL=http://dbservice:40002   # Docker internal DNS, bypasses nginx
```

**Nginx upstream switching (zero code change):**

```nginx
# Docker mode (current):
upstream memory_backend {
    server dbservice:40002;
}

# Cloud mode (future, just change upstream):
upstream memory_backend {
    server cloud-memory.aming-claw.com:443;
}

# Standalone mode (dbservice without Docker):
upstream memory_backend {
    server localhost:40002;
}

location /memory/ {
    proxy_pass http://memory_backend/;
    proxy_set_header Host $host;
    proxy_read_timeout 30s;
    proxy_connect_timeout 5s;
}
```

### 2.4 Interface

```python
# agent/governance/memory_backend.py (New)

class MemoryBackend:
    """Abstract interface for memory storage + search."""

    def write(self, project_id: str, entry: MemoryEntry) -> dict:
        """Store structured data + index for search."""
        raise NotImplementedError

    def search(self, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        """Semantic or full-text search. Returns ref_id list per spec §6.1.

        Flow (spec §2.3): query → recall → ref_id list → caller fetches full objects from SQLite.
        Returns: [{ref_id, score, score_type, search_mode, matched_text, metadata}]
        """
        raise NotImplementedError

    def query(self, project_id: str, module: str = None,
              kind: str = None, ref_id: str = None) -> list[dict]:
        """Structured query by field filters."""
        raise NotImplementedError

    def delete(self, project_id: str, memory_id: str) -> bool:
        raise NotImplementedError


class LocalBackend(MemoryBackend):
    """SQLite + FTS5. Zero external dependency, no Docker required."""

    def write(self, project_id, entry):
        # 1. INSERT into memories table
        # 2. FTS5 auto-synced via triggers (INSERT/UPDATE/DELETE)
        pass

    def search(self, project_id, query, top_k=5):
        # FTS5 MATCH query with rank ordering
        # SELECT *, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?
        pass


class RemoteBackend(MemoryBackend):
    """SQLite (local structured truth) + HTTP remote service (semantic search).

    Remote service accessed via MEMORY_SERVICE_URL (nginx reverse proxy).
    Backend-agnostic: nginx routes to Docker dbservice, cloud API, or
    standalone dbservice — RemoteBackend code never changes.
    Fallback: if remote unavailable, degrades to FTS5 (same as LocalBackend).
    """

    def __init__(self):
        self.url = os.environ.get("MEMORY_SERVICE_URL",
                                   "http://localhost:40000/memory")

    def write(self, project_id, entry):
        # 1. INSERT into memories table (local SQLite, same as LocalBackend)
        # 2. POST {self.url}/knowledge/upsert (remote semantic index)
        # Failure on step 2 is non-fatal: local data is truth, remote is recall
        pass

    def search(self, project_id, query, top_k=5):
        # POST {self.url}/search (remote semantic search)
        # Fallback to FTS5 if remote unavailable (graceful degradation)
        pass
```

### 2.5 DB Schema Addition

> Full schema in design-spec §4.1 (includes scope, durability, confidence, conflict_policy, relations, events).

```sql
-- Core memory table (see design-spec §4.1 for all fields)
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL,              -- stable semantic anchor (spec §3.2)
    entity_id TEXT DEFAULT '',
    kind TEXT NOT NULL,                -- fixed enum (spec §4.2)
    module_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',    -- project_id or 'global' (spec §4.4)
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    tags TEXT DEFAULT '[]',
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',
    durability TEXT DEFAULT 'durable', -- permanent/durable/session/transient
    confidence REAL DEFAULT 1.0,
    conflict_policy TEXT DEFAULT 'replace',
    superseded_by_memory_id TEXT DEFAULT NULL,
    index_status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Relations graph (spec §4.5)
CREATE TABLE IF NOT EXISTS memory_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_ref_id TEXT NOT NULL,
    relation TEXT NOT NULL,            -- PRODUCED/CAUSED_BY/RELATED_TO/VERIFIED_BY/DEPENDS_ON
    to_ref_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Event log (spec §4.5)
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,          -- created/superseded/promoted/archived
    actor_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- FTS5 full-text search (local backend)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, module_id, kind,
    content=memories, content_rowid=id
);

-- FTS5 triggers: INSERT + UPDATE + DELETE to keep index in sync.
-- Without UPDATE/DELETE triggers, archived/superseded/modified memories
-- produce stale search results over time.

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, module_id, kind)
    VALUES (new.id, new.content, new.module_id, new.kind);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, module_id, kind)
    VALUES ('delete', old.id, old.content, old.module_id, old.kind);
    INSERT INTO memories_fts(rowid, content, module_id, kind)
    VALUES (new.id, new.content, new.module_id, new.kind);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, module_id, kind)
    VALUES ('delete', old.id, old.content, old.module_id, old.kind);
END;
```

**FTS5 sync coverage:**

| Memory operation | FTS5 action | Trigger |
|-----------------|-------------|---------|
| INSERT (new memory) | Index new content | `memories_ai` |
| UPDATE (version bump, content edit, status change) | Delete old + index new | `memories_au` |
| DELETE (hard delete) | Remove from index | `memories_ad` |
| Supersede (status→inactive, superseded_by set) | Re-index with new status via UPDATE trigger | `memories_au` |
| Archive (status→archived) | Re-index via UPDATE trigger | `memories_au` |

**Note:** FTS5 `content=memories` external content tables require the
delete-then-reinsert pattern for updates (FTS5 limitation).
The `memories_au` trigger handles this correctly.

### 2.6 API Changes

```
# Existing (no change):
POST /api/mem/{pid}/write         — Write memory (uses selected backend)
GET  /api/mem/{pid}/query         — Structured query (always SQLite)

# New:
GET  /api/mem/{pid}/search?q=...&top_k=3  — Semantic/FTS search
  → Local: FTS5 MATCH
  → Docker: dbservice /search (mem0)
  → Cloud: Cloud API search
  → Returns ref_id list (spec §6.1), caller fetches full objects from SQLite

# New: Cross-project sharing (spec §4.4)
POST /api/mem/{pid}/promote            — Promote memory to global scope
  → Body: {memory_id, target_scope: "global", reason: "..."}
  → Creates copy with scope=global, logs memory_event

# New: Relation graph (spec §4.5)
POST /api/mem/{pid}/relate             — Create relation between ref_ids
  → Body: {from_ref_id, relation, to_ref_id}
GET  /api/mem/{pid}/expand?ref_id=X&depth=2  — Graph traversal from ref_id

# New: DomainPack registration (aligned with dbservice)
POST /api/mem/{pid}/register-pack      — Register project-specific kind types
  → Body: {domain: "development", types: {...}}
```

### 2.7 Files Changed

| File | Action | Description |
|------|--------|-------------|
| `agent/governance/memory_backend.py` | **New** | Backend interface + Local/Docker/Cloud implementations |
| `agent/governance/memory_service.py` | **Modify** | Use MemoryBackend instead of JSON file + direct dbservice |
| `agent/governance/db.py` | **Modify** | Add `memories` + `memories_fts` tables |
| `agent/governance/server.py` | **Modify** | Add `/api/mem/{pid}/search` endpoint |

---

## 3. Coordinator Task Awareness

### 3.1 Problem

- Coordinator creates duplicate tasks (no dedup)
- No conflict detection between queued tasks
- No awareness of past task results
- User gets no feedback about similar/conflicting tasks
- Every message creates a coordinator task, even greetings and status queries (wastes tokens)

### 3.2 Coordinator Decision Flow (Rules First, AI Assists — spec §7.3)

```
User message arrives
        │
        ▼
Step 0: INTENT CLASSIFICATION (gateway, code logic, 0 tokens)
  ├── greeting/chitchat → direct reply, NO task created
  │     keywords: 你好, hi, hello, thanks, 谢谢, ok, 好的
  │     → send_text(chat_id, "👋 Hello! Send a task or question.")
  │
  ├── status_query → gateway queries API directly, NO task created
  │     keywords: 状态, status, 进度, 节点, 多少, 几个, 列表
  │     → query /api/wf/summary or /api/task/list → format → send_text
  │
  ├── command → existing handler (/menu, /bind, /status, /health)
  │     → already handled before this flow
  │
  ├── dangerous → confirmation flow
  │     keywords: delete, rollback, 删除, 回滚, deploy
  │     → existing handle_dangerous
  │
  └── task_intent → create coordinator task → AI decides
        keywords: 帮我, 修, 写, 改, 添加, 创建, fix, add, implement, build
        or: no keyword match (default to coordinator for safety)
        │
        ▼
Step 1: GATHER (code logic, 0 tokens)
  ├── Memory search → ref_id list → SQLite fetch full objects (spec §2.3)
  ├── Active task queue (queued + claimed)
  └── Recent completed tasks (last 5)
        │
        ▼
Step 2: RULE ENGINE (code logic, 0 tokens)
  ├── Same file + opposite op? → decision: "conflict"
  ├── Same intent hash within 1h? → decision: "duplicate"
  ├── Same module busy? → decision: "queue"
  ├── Upstream not done? → decision: "block"
  ├── Past failure + followup_needed? → decision: "retry"
  └── No rules triggered → decision: "new"
        │
        ▼
Step 3: AI LAYER (only if rules say "new" or need explanation)
  ├── Inject: user message + rule decision + memory objects + queue
  └── AI outputs: natural language explanation + action JSON
        │
        ▼
Decisions (spec §7.4):
        │
        ├── DUPLICATE: similar memory + succeeded task exists
        │   → {action: "reply", text: "This was done in task-xxx (3h ago). Redo?"}
        │
        ├── CONFLICT: queue has contradicting task on same files
        │   → {action: "reply", text: "⚠️ Queue has 'add hello' but you want 'delete hello'.
        │      1️⃣ Queue behind current task
        │      2️⃣ Cancel queued task, do yours
        │      3️⃣ Merge both"}
        │
        ├── QUEUE: queue has task on same module, no contradiction
        │   → {action: "reply", text: "utils.py has a task running. Yours will queue after it."}
        │   → then: {action: "create_task", type: "pm", prompt: "...", priority: 2}
        │
        ├── RETRY: failure_pattern memory with followup_needed
        │   → {action: "create_task", type: "dev",
        │      prompt: "Retry: ... Previous failure: ... Avoid: ..."}
        │
        └── NEW: no conflicts, no duplicates
            → {action: "create_task", type: "pm", prompt: "..."}
```

### 3.3 Coordinator Prompt Injection

```python
# executor_worker.py _build_prompt (coordinator case):

elif task_type == "coordinator":
    chat_id = metadata.get("chat_id", "")
    user_message = prompt

    # 1. Semantic memory search (find similar past tasks)
    mem_results = self._api("GET",
        f"/api/mem/{self.project_id}/search?q={quote(user_message)}&top_k=3")
    memories = mem_results.get("results", [])

    # 2. Queue check (find active tasks)
    task_list = self._api("GET", f"/api/task/{self.project_id}/list")
    active_tasks = [t for t in task_list.get("tasks", [])
                    if t["status"] in ("queued", "claimed")
                    and t["type"] not in ("coordinator",)]

    parts.append(f"\nYou are a Coordinator. Analyze the user message and decide.")
    parts.append(f'\nUser message: "{user_message}"')

    if memories:
        parts.append(f"\nSimilar past memories ({len(memories)}):")
        for m in memories[:3]:
            parts.append(f'  - [{m.get("metadata",{}).get("kind","")}] '
                        f'{m.get("text","")[:80]} '
                        f'(ref: {m.get("metadata",{}).get("ref_id","")})')

    if active_tasks:
        parts.append(f"\nActive task queue ({len(active_tasks)}):")
        for t in active_tasks[:5]:
            parts.append(f'  - {t["task_id"][-12:]} [{t["type"]}] '
                        f'{t["status"]}: {t["prompt"][:60]}')

    parts.append("""
Decision rules:
- If similar memory exists AND task succeeded → ask before re-executing
- If queue has task on same files/module → warn about conflict or queue order
- If queue has contradicting operation → suggest cancellation
- If past failure_pattern exists for this → inject failure reason into new task
- For genuinely new requests → create_task

Respond with exactly one JSON:
  Reply: {"action": "reply", "text": "..."}
  New task: {"action": "create_task", "type": "pm"|"dev"|"test", "prompt": "..."}
  Cancel: {"action": "cancel_task", "task_id": "task-xxx", "reason": "..."}
""")
```

### 3.4 Coordinator Action Extensions

```json
// Existing actions:
{"action": "reply", "text": "..."}
{"action": "create_task", "type": "pm", "prompt": "..."}

// New actions:
{"action": "cancel_task", "task_id": "task-xxx", "reason": "Contradicts new request"}
{"action": "create_task", "type": "dev", "prompt": "...",
 "context": {"previous_failure": "...", "avoid": "..."}}
```

### 3.5 Files Changed

| File | Action | Description |
|------|--------|-------------|
| `agent/executor_worker.py` | **Modify** | Coordinator prompt: inject memory search + queue + rules |
| `agent/governance/server.py` | **Modify** | `/api/task/{pid}/cancel` endpoint (new) |

---

## 4. Executor Lifecycle Management

### 4.1 Problem

- Executor crashes → nobody restarts it → tasks queue forever
- After merge, pycache serves stale bytecode → `NameError` on fixed code
- ServiceManager only does start/stop, no crash monitoring
- Multiple zombie executor processes accumulate

### 4.2 ServiceManager Enhancements

```python
# agent/service_manager.py additions:

class ServiceManager:
    def __init__(self, ...):
        self._monitor_thread = None
        self._restart_count = 0
        self._max_restarts = 5        # Circuit breaker
        self._restart_window = 300     # Reset count after 5 min stable

    def start(self) -> bool:
        """Start executor + monitoring thread."""
        started = self._start_process()
        if started and self._monitor_thread is None:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
        return started

    def _monitor_loop(self):
        """Watch executor process, auto-restart on crash."""
        stable_since = time.monotonic()
        while self._running:
            time.sleep(10)
            if self._process and self._process.poll() is not None:
                exit_code = self._process.returncode
                log.warning("Executor crashed (exit=%d, restarts=%d/%d)",
                           exit_code, self._restart_count, self._max_restarts)

                # Circuit breaker
                if self._restart_count >= self._max_restarts:
                    log.error("Executor restart limit reached. Manual intervention needed.")
                    self._notify_observer("Executor crash loop detected")
                    continue

                # Clear pycache before restart
                self._clear_pycache()

                # Restart
                self._restart_count += 1
                self._start_process()
                stable_since = time.monotonic()

            # Reset restart count after stable window
            elif time.monotonic() - stable_since > self._restart_window:
                if self._restart_count > 0:
                    log.info("Executor stable for %ds, reset restart count", self._restart_window)
                    self._restart_count = 0
                    stable_since = time.monotonic()

    def _clear_pycache(self):
        """Remove __pycache__ dirs to prevent stale bytecode after merge."""
        import shutil
        workspace = self._workspace or os.path.dirname(os.path.dirname(__file__))
        count = 0
        for root, dirs, _ in os.walk(workspace):
            if "__pycache__" in dirs:
                shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
                count += 1
        if count:
            log.info("Cleared %d __pycache__ directories", count)

    def _notify_observer(self, message: str):
        """Send alert via Telegram."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("OBSERVER_CHAT_ID", "")
        if token and chat_id:
            # ... send telegram message
            pass
```

### 4.3 Pycache Cleanup Points

```
When to clear __pycache__:

  1. Executor crash → _monitor_loop → _clear_pycache → restart
  2. After merge commit → deploy_chain → _clear_pycache
  3. Executor startup → always clear as safety measure
  4. MCP executor_scale(0 → 1) → clear before start
```

### 4.4 PID Lock (aligned with spec §8.2)

Single-instance enforcement uses a layered approach per spec:

1. **File lock (primary)** — OS-level `fcntl.flock()` / `msvcrt.locking()`.
   Guarantees mutual exclusion even across unrelated processes.
2. **PID file (supplementary)** — Written after acquiring file lock.
   Used for status display and health check, not for exclusion.
3. **Kill old process** — Only permitted when ALL three conditions are met:
   - Same project_id
   - Health check on old PID failed (heartbeat stale > 120s)
   - Grace period exceeded (SIGTERM sent, waited 5s, still alive → SIGKILL)

```python
# executor_worker.py startup:

def _acquire_pid_lock(self):
    """Ensure only one executor per project (spec §8.2)."""
    lock_path = os.path.join(tempfile.gettempdir(),
                            f"aming-claw-executor-{self.project_id}.lock")
    pid_path = lock_path.replace(".lock", ".pid")

    # Step 1: Try file lock (non-blocking)
    self._lock_fd = open(lock_path, "w")
    try:
        _try_flock(self._lock_fd)  # platform-specific flock
    except BlockingIOError:
        # Lock held — check if old process is actually healthy
        old_pid = _read_pid(pid_path)
        if old_pid and _is_process_alive(old_pid):
            if not _is_heartbeat_stale(old_pid, self.project_id, timeout=120):
                raise RuntimeError(f"Healthy executor already running (PID {old_pid})")
            # Old process alive but stale heartbeat → kill
            log.warning("Stale executor PID %d, sending SIGTERM", old_pid)
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(5)
            if _is_process_alive(old_pid):
                os.kill(old_pid, signal.SIGKILL)
        _try_flock(self._lock_fd)  # retry after kill

    # Step 2: Write PID file (supplementary info)
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))
    return lock_path
```

**Prohibited:** Killing a healthy executor just because a PID file exists.

### 4.5 Heartbeat (spec §8.3)

Executor must update heartbeat while running a task:

```python
# executor_worker.py — in task execution loop:

def _heartbeat_loop(self, task_id):
    """Background thread: update heartbeat every 30s while task runs."""
    while self._current_task == task_id:
        self._api("POST", f"/api/task/{self.project_id}/heartbeat", {
            "task_id": task_id,
            "worker_id": self.worker_id,
        })
        time.sleep(30)
```

**Server side:** `POST /api/task/{pid}/heartbeat` updates `heartbeat_at` in tasks table.

**Timeout detection:** Task claimed + `heartbeat_at` > 120s old → considered stuck.

### 4.6 Crash Recovery (spec §8.4)

On executor startup, scan and recover orphaned tasks:

```python
def _recover_stuck_tasks(self):
    """Find claimed tasks with stale heartbeat, reset to queued."""
    tasks = self._api("GET", f"/api/task/{self.project_id}/list")
    for task in tasks.get("tasks", []):
        if task["status"] != "claimed":
            continue
        heartbeat = task.get("heartbeat_at", task.get("updated_at", ""))
        if _is_stale(heartbeat, timeout_sec=120):
            self._api("POST", f"/api/task/{self.project_id}/recover", {
                "task_id": task["task_id"],
                "action": "requeue",
                "reason": f"executor_crash_recovery (stale heartbeat: {heartbeat})"
            })
            log.info("Recovered stuck task: %s", task["task_id"])
```

Called at executor startup, before entering poll loop.

### 4.7 Executor Status Enhancement

```python
# MCP tool executor_status returns:
{
    "pid": 12345,
    "running": true,
    "uptime_s": 3600,
    "active_tasks": 1,
    "queued_tasks": 3,
    "restart_count": 2,       # NEW: crashes since last stable window
    "last_crash_at": "...",   # NEW
    "pycache_cleared": true,  # NEW
    "health": "healthy"       # NEW: healthy / degraded / crash_loop
}
```

### 4.6 Files Changed

| File | Action | Description |
|------|--------|-------------|
| `agent/service_manager.py` | **Modify** | Add monitor loop, crash restart, pycache clear, circuit breaker |
| `agent/executor_worker.py` | **Modify** | Add PID lock on startup |
| `agent/deploy_chain.py` | **Modify** | Clear pycache after merge |
| `agent/mcp/tools.py` | **Modify** | Enhanced executor_status response |

---

## 5. Implementation Priority

> **Authoritative ordering.** This table is the single source of truth for
> implementation sequence. It aligns with design-spec §11 (Implementation Order).
> Previous versions of this PRD used a different Phase numbering; this version
> supersedes all prior orderings.

| Phase | Scope | Files | Dependency | Status |
|-------|-------|-------|------------|--------|
| **Phase 1** | Executor lifecycle (crash recovery, PID lock, circuit breaker) | service_manager, executor_worker, deploy_chain | None | DONE |
| **Phase 2** | Memory backend interface + local SQLite FTS5 | memory_backend, memory_service, db, server | None | DONE |
| **Phase 3** | ref_id lifecycle, entity mapping, relation graph | memory_backend, db, server | Phase 2 | DONE |
| **Phase 4** | Task metadata enrichment + conflict rule engine | conflict_rules, server | Phase 2 | DONE |
| **Phase 5** | Coordinator awareness (intent classifier, prompt injection) | executor_worker, gateway | Phase 4 | DONE |
| **Phase 6** | Docker mem0 backend (semantic search + FTS5 fallback) | memory_backend | Phase 2 | DONE |
| **Phase 7** | Spec invariant verification tests | tests/test_verify_spec | Phase 1-6 | DONE |
| **Phase 8** | Chain Context (event-sourced runtime context) | chain_context, auto_chain, server, task_registry, db | None | TODO |
| **Phase 9** | Cloud RemoteBackend (nginx upstream switch) | nginx.conf, memory_backend | Phase 6 | Future |

**Dependency rules:**
- Phase 1 and 2 can run in parallel (no shared files).
- Phase 3-6 depend on Phase 2 (memory backend interface).
- Phase 5 depends on Phase 4 (conflict rules feed coordinator prompt).
- Phase 8 has zero dependency — can be implemented at any time.
- Phase 9: RemoteBackend code already exists from Phase 6. Cloud enablement is
  nginx upstream config + cloud API key — no Python code change.

**Out of scope (future PRD):**
- De-Dockerize governance + gateway (native Python deployment)
- Message layer abstraction (pluggable: Redis pub/sub vs Python queue)

---

## 6. Complete File Change Summary

| File | Action | Phases |
|------|--------|--------|
| `agent/governance/memory_backend.py` | **New** | 2,3,6 (LocalBackend + RemoteBackend) |
| `agent/governance/memory_service.py` | **Modify** | 2 |
| `agent/governance/db.py` | **Modify** | 2 |
| `agent/governance/server.py` | **Modify** | 2,4 |
| `agent/service_manager.py` | **Modify** | 1 |
| `agent/executor_worker.py` | **Modify** | 1,4 |
| `agent/deploy_chain.py` | **Modify** | 1 |
| `agent/mcp/tools.py` | **Modify** | 1 |
| `agent/governance/chain_context.py` | **New** | 8 |

## 7. Affected Nodes

| Node ID | Title | Current | Action | Phase |
|---------|-------|---------|--------|-------|
| L22.2 | Memory Write | qa_pass | → testing | 2 |
| L22.4 | Memory Query | qa_pass | → testing | 2 |
| L15.1 | AI Lifecycle | qa_pass | → testing | 4 |
| L13.5 | Deploy | qa_pass | → testing | 1 |

## 8. Documentation Updates

| Document | Change | Phase |
|----------|--------|-------|
| `docs/architecture-v6-executor-driven.md` | Memory backend architecture + executor lifecycle | 1,2 |
| `docs/ai-agent-integration-guide.md` | Memory search API + coordinator decision rules | 2,4 |
| `docs/deployment-guide.md` | MEMORY_BACKEND config + executor crash recovery | 1,2 |
| `README.md` | Architecture diagram update | 2 |

## 9. Acceptance Criteria

### Phase 1: Executor Lifecycle
- [ ] ServiceManager monitors executor, auto-restarts on crash (max 5 per 5 min)
- [ ] pycache cleared before every executor restart
- [ ] deploy_chain clears pycache after merge
- [ ] File lock (OS-level) prevents duplicate executors; PID file is supplementary only (spec §8.2)
- [ ] Old executor killed ONLY when: same project + heartbeat stale >120s + grace period exceeded
- [ ] executor_status shows health/restart_count/last_crash
- [ ] Circuit breaker stops restart loop, sends Telegram alert

### Phase 2: Memory Backend (Local)
- [ ] `memories` table + FTS5 in governance.db
- [ ] `MemoryBackend` interface with Local/Docker/Cloud subclasses
- [ ] `MEMORY_BACKEND=local` uses SQLite FTS5 search
- [ ] `/api/mem/{pid}/search?q=...` endpoint works
- [ ] Existing `/api/mem/{pid}/write` and `/query` still work

### Phase 3: ref_id Lifecycle
- [ ] entity_id ↔ ref_id stable mapping
- [ ] Version chain tracking (memory_id chain per ref_id)
- [ ] `search_and_aggregate` returns latest version per ref_id
- [ ] Relation graph (memory_relations table) with PRODUCED/CAUSED_BY/RELATED_TO
- [ ] `/api/mem/{pid}/expand?ref_id=X&depth=2` graph traversal works

### Phase 4: Coordinator Awareness
- [ ] Coordinator prompt includes semantic memory search results
- [ ] Coordinator prompt includes active task queue
- [ ] Duplicate task detected → asks user before re-executing
- [ ] Conflicting queue task detected → warns user with options
- [ ] Past failure detected → injects failure context into new task

**Task metadata quality (hard requirements per spec §7.2):**
- [ ] Every new task has `source_message_hash` (auto-generated by gateway from user message)
- [ ] File/module-aware tasks MUST have `target_files` or `target_modules` (gate rejects if empty)
- [ ] `operation_type` MUST NOT be empty (fallback: keyword extraction from prompt, default "modify")
- [ ] `intent_summary` MUST NOT be empty (fallback: first 100 chars of prompt)
- [ ] Conflict rule engine MUST use structured metadata fields for same-file detection, NOT raw prompt text matching
- [ ] Post-PM gate MUST reject tasks with empty `target_files` AND empty `intent_summary`

### Phase 6: Docker RemoteBackend
- [ ] `MEMORY_BACKEND=remote` routes search through nginx to dbservice
- [ ] RemoteBackend uses `MEMORY_SERVICE_URL` (nginx proxy), not direct dbservice URL
- [ ] Fallback to FTS5 if remote unavailable
- [ ] Existing knowledge entries accessible via RemoteBackend

### Phase 9: Cloud RemoteBackend (Future)
- [ ] Cloud upstream configured in nginx.conf
- [ ] Same RemoteBackend code works with cloud (zero Python change)
- [ ] API key auth injected by nginx (`proxy_set_header Authorization`)

### Future (separate PRD)
- De-Dockerize governance + gateway (native Python deployment)
- Message layer abstraction: pluggable transport (Redis pub/sub vs Python `queue.Queue`)
  to decouple EventBus from Redis dependency

---

## 10. Chain Context — Task Chain Runtime Context (Phase 8)

> **Spec Reference:** §4 Memory Data Model (`task_result`, `task_snapshot` kinds),
> §7.1 Coordinator Input Requirements, §8.4 Crash Recovery.
>
> **Consistency Boundary:** ChainContextStore is authoritative within a single
> governance process. Multi-instance deployments require a shared event store
> (out of scope for this phase).

### 10.1 Problem

Context passing between auto-chain stages has critical defects:

1. **Retry context loss** — When a gate blocks and creates a retry task,
   `_original_prompt` is uninitialized, producing `"Original task: "` (empty).
   The executor cannot know the original task intent.
   *Observed:* PM doc-update task retried 3 times; all failed due to lost context.

2. **No chain-level context** — `_build_dev_prompt()` extracts `target_files`/`verification`
   from PM result, but if PM result format is non-standard, dev task creation fails.
   Stages only have scattered metadata fields, no coherent chain context.

3. **`/api/context-snapshot?task_id=xxx` ignores task_id** — The parameter is accepted
   but unused in the handler. Every task receives the same project-level snapshot,
   unable to see its own chain position or parent task output.

4. **No real-time gate state** — Gate block events are broadcast via EventBus
   but never persisted. Subsequent retry tasks and context-snapshot queries
   cannot access gate block history.

5. **Parallel safety** — Querying task chains requires DB reads (tasks table JOIN).
   SQLite write locks under parallel executor scenarios cause timeouts.
   Spec §4 `task_result`/`task_snapshot` memory writes were never implemented.

### 10.2 Design: Event-Sourced Chain Context

Event sourcing pattern: in-memory dict for hot-path reads/writes, every event
persisted synchronously to DB, crash recovery via event replay.

#### Architecture

```
EventBus (in-process, existing)
    │
    ├── task.created ────┐
    ├── task.completed ──┤
    ├── gate.blocked ────┤──→ ChainContextStore (in-memory dict)
    ├── task.retry ──────┤         │
    ├── task.failed ─────┘         ├── read: O(1) dict lookup (lock-free snapshot)
    │                              ├── write: event-driven, threading.Lock
    │                              └── persist: sync INSERT to chain_events table
    │
    └── chain.archived ──→ release memory, DB data retained for audit
```

#### Chain State Machine

```
                ┌─────────────┐
                │   running   │ ← initial state
                └──────┬──────┘
                       │
              ┌────────┼────────┐
              ▼        ▼        ▼
         ┌─────────┐ ┌──────┐ ┌────────┐
         │ blocked │ │failed│ │cancelled│
         └────┬────┘ └──┬───┘ └────┬────┘
              │         │          │
              ▼         │          │
         ┌─────────┐   │          │
         │retrying │   │          │
         └────┬────┘   │          │
              │        │          │
              ▼        │          │
         ┌─────────┐   │          │
         │ running │   │          │
         └────┬────┘   │          │
              │        │          │
              ▼        ▼          ▼
         ┌──────────────────────────┐
         │       completed          │
         └────────────┬─────────────┘
                      ▼
         ┌──────────────────────────┐
         │       archived           │
         └──────────────────────────┘
```

**State transitions:**

| From | To | Trigger |
|------|----|---------|
| `running` | `blocked` | gate.blocked event |
| `running` | `completed` | merge task succeeded |
| `running` | `failed` | max retries exhausted OR task failed with no retry |
| `blocked` | `retrying` | task.retry event (auto-chain creates retry) |
| `retrying` | `running` | retry task claimed by executor |
| `retrying` | `failed` | retry exhausted (attempt >= max_retries) |
| `blocked` | `failed` | no retry created (gate_retry_count >= 2) |
| any | `cancelled` | manual cancellation via API |
| `completed` | `archived` | archive_chain() called |
| `failed` | `archived` | archive_chain() called (cleanup) |
| `cancelled` | `archived` | archive_chain() called (cleanup) |

**Archive triggers (memory release):**
- merge succeeded → immediate archive
- chain in `failed` state → archive after 60s delay (allow inspection)
- chain in `cancelled` state → immediate archive
- stale chain watchdog: any chain with `updated_at` > 1 hour old → force archive

#### Data Model

**In-memory structures:**

```python
ChainContextStore
  ├── _chains: dict[root_task_id → ChainContext]
  └── _task_to_root: dict[any_task_id → root_task_id]

ChainContext
  ├── root_task_id: str
  ├── project_id: str
  ├── state: "running" | "blocked" | "retrying" | "completed"
  │          | "failed" | "cancelled" | "archived"
  ├── current_stage: task_id
  ├── stages: dict[task_id → StageSnapshot]
  ├── created_at: str
  └── updated_at: str

StageSnapshot
  ├── task_id: str
  ├── task_type: str           # pm | dev | test | qa | merge
  ├── prompt: str              # full original prompt
  ├── result_core: dict | None # structured key fields (see §10.2.2)
  ├── result_raw: dict | None  # full result (truncated on serialize)
  ├── gate_reason: str | None
  ├── attempt: int
  ├── parent_task_id: str | None
  └── ts: str
```

##### 10.2.1 Root Task ID Assignment Rules

| Scenario | root_task_id | Rule |
|----------|-------------|------|
| First task in chain (PM or standalone dev) | `self.task_id` | No parent → becomes root |
| Auto-chained stage (dev, test, qa, merge) | inherited from parent | `_task_to_root[parent_task_id]` |
| Retry of any stage | same root as original | `_task_to_root[original_task_id]` |
| Observer manual intervention task | `self.task_id` | New chain (observer is a different flow) |
| Unlinked task (no parent_task_id in payload) | `self.task_id` | Standalone chain of length 1 |

**Invariant:** Once assigned, a task's root_task_id MUST NOT change.

##### 10.2.2 Result Storage: Core vs Raw

Not all result fields are equally important. Chain context stores results in two tiers:

**`result_core`** — Structured key fields, always preserved in full:

```python
RESULT_CORE_FIELDS = [
    "target_files",          # list[str] — files to modify
    "changed_files",         # list[str] — files actually modified (from git diff)
    "verification",          # dict — test/QA verification plan
    "requirements",          # list[str] — implementation requirements
    "acceptance_criteria",   # list[str] — criteria for success
    "test_report",           # dict — {passed, failed, tool, duration_sec}
    "prd",                   # dict — PM output (target_files, verification, etc.)
    "proposed_nodes",        # list[str] — new acceptance graph nodes
    "summary",               # str — one-line summary of what was done
    "related_nodes",         # list[str] — affected workflow nodes
]
```

**`result_raw`** — Full result dict. Preserved in memory during chain lifetime,
truncated on DB serialization (max 10KB per stage).

**Extraction logic:**

```python
def _extract_core(result: dict) -> dict:
    core = {}
    for field in RESULT_CORE_FIELDS:
        val = result.get(field)
        if val is None and "prd" in result:
            val = result["prd"].get(field)  # fallback to nested prd
        if val is not None:
            core[field] = val
    return core
```

**Consumer contract:** `_build_dev_prompt()`, `_build_test_prompt()`, etc. MUST
be able to reconstruct their prompts from `result_core` alone.

#### DB Table (event log)

```sql
CREATE TABLE IF NOT EXISTS chain_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id  TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_events_root
    ON chain_events(root_task_id, ts);
CREATE INDEX IF NOT EXISTS idx_chain_events_task
    ON chain_events(task_id, event_type, ts);
```

**No UNIQUE constraint.** Events are append-only. The same `(task_id, event_type)`
may appear multiple times (e.g., multiple `gate.blocked` with different reasons).

##### Event Categories

| Category | Events | DB behavior | Rationale |
|----------|--------|-------------|-----------|
| **State events** (latest wins) | `task.completed`, `chain.archived` | Append; replay takes latest | Only the final result matters |
| **Audit events** (full history) | `gate.blocked`, `task.retry`, `task.failed` | Append; replay processes all | Multiple blocks with different reasons must be preserved for debugging |
| **Init events** (once per task) | `task.created` | Append; replay skips duplicates via `_task_to_root` check | Idempotent by design |

On replay, the handler methods are idempotent:
- `on_task_created`: skips if `task_id` already in `_task_to_root`
- `on_gate_blocked`: always updates (latest reason wins in memory, all preserved in DB)
- `on_task_completed`: overwrites result (latest wins)
- `on_task_retry`: creates new stage entry (retry_id is always unique)

#### Event Handlers

| Event | Memory operation | DB operation | Trigger point |
|-------|-----------------|--------------|---------------|
| `task.created` | Create chain or join existing; store prompt | INSERT | `task_registry.create_task` / `auto_chain` next stage |
| `task.completed` | Update stage.result_core + result_raw | INSERT | `auto_chain.on_task_completed` |
| `gate.blocked` | chain.state="blocked"; store gate_reason | INSERT (append, not replace) | `auto_chain` gate check |
| `task.retry` | Inherit original prompt; attempt+1; state="retrying" | INSERT (append) | `auto_chain` retry creation |
| `task.failed` | chain.state="failed" if max retries | INSERT | `auto_chain` retry exhausted |
| `chain.archived` | Release memory | INSERT | merge complete / failed+timeout / cancelled |

#### Read API

| Method | Purpose | Caller |
|--------|---------|--------|
| `get_chain(task_id, role=None) → dict` | Chain context (role-filtered if role set) | `/api/context-snapshot?task_id=xxx&role=dev` |
| `get_original_prompt(task_id) → str` | Root task prompt (no role filter) | `auto_chain` retry prompt builder |
| `get_parent_result(task_id) → dict` | Parent stage result_core (no role filter) | `_build_dev_prompt` fallback |
| `get_state(task_id) → str` | Current chain state | Gate decisions, monitoring |

All reads from in-memory dict, O(1), no DB access.

#### Role-Based Context Projection

Not every role should see the full chain. Each role has a token budget
(spec: coordinator ~6000, dev ~4000) and a responsibility boundary
(spec §8.1: executor "Does NOT make product decisions").

`get_chain(task_id, role=None)` applies a projection filter when `role` is set:

| Field | pm | dev | test | qa | merge | coordinator |
|-------|:--:|:---:|:----:|:--:|:-----:|:-----------:|
| `chain.state` | - | current only | current only | current only | current only | full |
| `chain.stages` (which) | own | own + parent PM | own + parent dev | own + parent test | own + parent qa | all |
| `stage.prompt` | full | full | - | - | - | truncated (200 chars) |
| `stage.result_core` | - | parent's core | parent's changed_files + test config | parent's test_report + changed_files | parent's qa pass + changed_files | all (summary only) |
| `stage.result_raw` | - | - | - | - | - | - |
| `stage.gate_reason` | - | own (if retry) | - | - | - | all |
| `root prompt` | full | full (for retry) | - | - | - | truncated |

**Projection rules:**

```python
ROLE_VISIBLE_STAGES = {
    "pm":          lambda s: s.task_type == "pm",
    "dev":         lambda s: s.task_type in ("pm", "dev"),
    "test":        lambda s: s.task_type in ("dev", "test"),
    "qa":          lambda s: s.task_type in ("test", "qa"),
    "merge":       lambda s: s.task_type in ("qa", "merge"),
    "coordinator": lambda s: True,  # sees all stages
}

ROLE_RESULT_FIELDS = {
    "pm":          [],
    "dev":         ["target_files", "requirements", "acceptance_criteria",
                    "verification", "prd"],
    "test":        ["changed_files", "target_files"],
    "qa":          ["test_report", "changed_files", "acceptance_criteria"],
    "merge":       ["changed_files", "test_report"],
    "coordinator": ["target_files", "changed_files", "summary",
                    "test_report", "related_nodes"],
}
```

**Rationale:**
1. **Token savings** — dev does not see QA history, test does not see full PRD.
   Reduces injected context by ~40-60% per role.
2. **Responsibility isolation** — dev cannot see coordinator decisions or QA
   judgment rationale, preventing scope creep.
3. **Retry context** — dev in retry mode sees own `gate_reason` and root `prompt`
   (needed to fix the issue), but not other stages' internals.

**Implementation:** Projection is applied in `get_chain()` serialization, not in
the storage layer. In-memory data remains complete for internal use
(e.g., `get_original_prompt` always returns full prompt regardless of role).

#### Crash Recovery

```python
def recover_from_db(self, project_id):
    """Replay chain_events on startup to rebuild active chains."""
    from .db import get_connection
    conn = get_connection(project_id)
    rows = conn.execute(
        "SELECT root_task_id, task_id, event_type, payload_json, ts "
        "FROM chain_events "
        "WHERE root_task_id NOT IN ("
        "  SELECT root_task_id FROM chain_events "
        "  WHERE event_type = 'chain.archived'"
        ") ORDER BY ts"
    ).fetchall()

    for row in rows:
        payload = json.loads(row["payload_json"])
        handler = EVENT_HANDLERS.get(row["event_type"])
        if handler:
            handler(payload)  # reuse same on_xxx methods (idempotent)
```

**Recovery sequence:**
1. Governance server starts
2. `register_events()` subscribes handlers to EventBus
3. `recover_from_db(project_id)` replays non-archived events
4. In-memory state rebuilt, normal service resumes

**Idempotency:** `on_task_created` checks `_task_to_root` before creating;
duplicate events produce no side effects.

### 10.3 Integration Points (changes to existing files)

#### 10.3.1 `auto_chain.py` — Retry fetches original prompt from memory

```python
# Existing code line 110-114, replace retry_prompt construction
original_prompt = metadata.get("_original_prompt", "")
if not original_prompt:
    from .chain_context import get_store
    original_prompt = get_store().get_original_prompt(task_id)

retry_prompt = (
    f"Previous attempt ({task_id}) was blocked by gate.\n"
    f"Gate reason: {reason}\n\n"
    f"Fix the issue described above and retry.\n"
    f"Original task: {original_prompt}"
)
```

#### 10.3.2 `auto_chain.py` — Emit task.completed event; archive on merge

Before gate check in `on_task_completed`:

```python
_publish_event("task.completed", {
    "project_id": project_id, "task_id": task_id,
    "result": result, "type": task_type,
})
```

After merge succeeds:

```python
from .chain_context import get_store
get_store().archive_chain(task_id)
```

After retry exhausted (gate_retries >= 2, no retry created):

```python
_publish_event("task.failed", {
    "project_id": project_id, "task_id": task_id,
    "reason": "gate_retry_exhausted", "gate_reason": reason,
})
```

#### 10.3.3 `auto_chain.py` — `_build_dev_prompt` parent result fallback

```python
def _build_dev_prompt(task_id, result, metadata):
    prd = result.get("prd", {})
    target_files = result.get("target_files",
                     prd.get("target_files",
                     metadata.get("target_files", [])))

    # Fallback: if PM result lacks expected structure, read from chain context
    if not target_files:
        from .chain_context import get_store
        parent_result = get_store().get_parent_result(task_id)
        if parent_result:
            target_files = parent_result.get("target_files", [])
            # same for verification, requirements, acceptance_criteria
    ...
```

#### 10.3.4 `server.py` — context-snapshot uses task_id

```python
# handle_context_snapshot: task_id and role parameters exist but task_id is unused
task_chain = None
if task_id:
    from .chain_context import get_store
    task_chain = get_store().get_chain(task_id, role=role)

return {
    ...existing fields...,
    "task_chain": task_chain,  # role-filtered chain context
}
```

#### 10.3.5 `server.py` — Register events + recover on startup

```python
# In server startup logic
from .chain_context import register_events, get_store
register_events()
for pid in registered_projects:
    get_store().recover_from_db(pid)
```

#### 10.3.6 `task_registry.py` — Auto-store `_original_prompt` on create

```python
def create_task(conn, project_id, prompt, ..., metadata=None):
    meta = metadata or {}
    if "_original_prompt" not in meta:
        meta["_original_prompt"] = prompt
    ...
```

#### 10.3.7 `db.py` — Add chain_events table DDL

```python
SCHEMA_SQL += """
CREATE TABLE IF NOT EXISTS chain_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id  TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_events_root
    ON chain_events(root_task_id, ts);
CREATE INDEX IF NOT EXISTS idx_chain_events_task
    ON chain_events(task_id, event_type, ts);
"""
```

### 10.4 Data Lifecycle

```
Event fires → in-memory dict O(1) update + DB chain_events INSERT (<1ms)
Runtime read → in-memory dict O(1) (no DB)
Crash        → restart → recover_from_db() replays events → rebuild memory
Chain done   → chain.archived event → memory released → DB retained for audit
Failed chain → 60s delay → archive (allow inspection window)
Stale chain  → watchdog: updated_at > 1h → force archive + log warning
Old data     → optional: purge chain_events older than N days where archived
```

### 10.5 Files Changed

| File | Action | Description |
|------|--------|-------------|
| `agent/governance/chain_context.py` | **New** | ChainContextStore + EventBus subscribers + recover + archive |
| `agent/governance/auto_chain.py` | **Modify** | Retry reads original prompt from store; emit task.completed/task.failed; archive on merge; _build_dev_prompt fallback |
| `agent/governance/server.py` | **Modify** | context-snapshot injects task_chain; register events + recover on startup |
| `agent/governance/task_registry.py` | **Modify** | create_task auto-stores `_original_prompt` in metadata |
| `agent/governance/db.py` | **Modify** | chain_events table DDL |

### 10.6 NOT Changed

| File | Reason |
|------|--------|
| `VERSION` | Never touched; updated by merge stage only |
| `agent/governance/memory_backend.py` | Memory system not involved |
| `agent/ai_lifecycle.py` | context-snapshot returns extra `task_chain` field; `_build_system_prompt` auto-injects |
| `agent/executor_worker.py` | No change; context flows through snapshot API |
| `agent/governance/event_bus.py` | Existing functionality sufficient |

### 10.7 Documentation Updates

| Document | Change |
|----------|--------|
| `docs/ai-agent-integration-guide.md` | context-snapshot: new `task_chain` field |
| `docs/p0-3-design.md` | auto-chain retry context recovery mechanism |
| `docs/architecture-v6-executor-driven.md` | Gate Retry section: add event-sourcing |

### 10.8 Risks

| Risk | Mitigation |
|------|------------|
| Process crash loses in-memory state | `recover_from_db` replays events; `_original_prompt` in metadata as fallback |
| chain_events INSERT write lock | Single-row append-only, WAL mode <1ms; lower write frequency than tasks table |
| Memory leak (chains never archived) | State machine: failed/cancelled → archive; stale watchdog (1h) → force archive |
| payload_json too large | `result_core` extracts only key fields; `result_raw` truncated to 10KB on DB write |
| Multi-gate.blocked overwrites history | No UNIQUE constraint; append-only; all events preserved for audit |
| Multi-process inconsistency | Explicit boundary: single governance process is authority. Future: shared event store |

### 10.9 Acceptance Criteria

- [ ] PM task retry prompt contains full original PRD (no more empty `"Original task: "`)
- [ ] PM succeed → dev task target_files correct even when PM result format is non-standard
- [ ] `/api/context-snapshot?task_id=xxx` returns `task_chain` field with full chain context
- [ ] After gate.blocked, `get_chain()` immediately returns `state="blocked"` + `gate_reason`
- [ ] After governance restart, `recover_from_db()` correctly rebuilds active chain state
- [ ] After merge, chain released from memory; chain_events data preserved in DB
- [ ] Two parallel chains execute without DB lock timeout
- [ ] Multiple gate.blocked events on same task all preserved in chain_events (no overwrite)
- [ ] Failed chain (retry exhausted) transitions to `failed` → archived within 60s
- [ ] **Integration test:** Chain A blocked→retry + Chain B normal progress + governance restart → A retains original_prompt and gate_reason, B continues to completion
- [ ] pytest full suite passes, 0 new failures

### 10.10 Future: Align with Spec §4

This phase solves runtime context passing. A future phase can write `task_result`
kind to the memory system on chain archive (Spec §4), enabling cross-chain
knowledge recall:

```
chain.archived → memory.write(kind="task_result", ref_id=root_task_id, content=chain_summary)
```

This would allow Coordinator to recall historical chain outcomes via `/api/mem/search`
when creating new tasks.
