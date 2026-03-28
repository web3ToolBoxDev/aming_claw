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

### 2.2 Architecture

```
memory_service.py
    │
    ├── MemoryBackend (abstract interface)
    │       ├── write(entry) → store structured data + semantic index
    │       ├── search(query, top_k) → semantic search results
    │       ├── query(module, kind, ref_id) → structured query
    │       └── delete(memory_id) → remove
    │
    ├── LocalBackend (default, zero dependency)
    │       ├── SQLite `memories` table (structured)
    │       └── SQLite FTS5 (full-text search, keyword matching)
    │
    ├── DockerBackend (current dbservice)
    │       ├── SQLite `memories` table (structured, same as local)
    │       └── dbservice :40002 (mem0 semantic search via HTTP)
    │           ├── /store → mem0 Memory.add (vector index)
    │           ├── /search → mem0 Memory.search (semantic query)
    │           └── /knowledge/upsert → knowledge store (SQLite + audit)
    │
    └── CloudBackend (future)
            ├── SQLite `memories` table (structured, local cache)
            └── Cloud API (shared semantic search across users)
```

### 2.3 Configuration

```bash
# Environment variable selects backend
MEMORY_BACKEND=local    # Default: SQLite + FTS5, zero dependency
MEMORY_BACKEND=docker   # dbservice mem0 (requires Docker)
MEMORY_BACKEND=cloud    # Future cloud service

# Docker backend config
DBSERVICE_URL=http://localhost:40002   # or http://dbservice:40002 inside Docker

# Cloud backend config (future)
MEMORY_CLOUD_URL=https://memory.aming-claw.com
MEMORY_CLOUD_API_KEY=xxx
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
    """SQLite + FTS5. Zero external dependency."""

    def write(self, project_id, entry):
        # 1. INSERT into memories table
        # 2. INSERT into memories_fts (FTS5 virtual table)
        pass

    def search(self, project_id, query, top_k=5):
        # FTS5 MATCH query with rank ordering
        # SELECT *, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?
        pass


class DockerBackend(MemoryBackend):
    """SQLite (structured) + dbservice mem0 (semantic)."""

    def write(self, project_id, entry):
        # 1. INSERT into memories table (same as local)
        # 2. POST dbservice /store (mem0 vector index)
        # 3. POST dbservice /knowledge/upsert (knowledge store)
        pass

    def search(self, project_id, query, top_k=5):
        # POST dbservice /search (mem0 semantic search)
        # Fallback to FTS5 if dbservice unavailable
        pass


class CloudBackend(MemoryBackend):
    """SQLite (local cache) + Cloud API (shared semantic search)."""
    # Future implementation
    pass
```

### 2.5 DB Schema Addition

```sql
-- Structured memory storage (all backends use this)
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'decision',
    content TEXT NOT NULL,
    structured_json TEXT DEFAULT '{}',
    ref_id TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    superseded_by TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, is_active);
CREATE INDEX IF NOT EXISTS idx_memories_module ON memories(project_id, module_id);
CREATE INDEX IF NOT EXISTS idx_memories_ref ON memories(project_id, ref_id);

-- FTS5 full-text search (local backend)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, module_id, kind,
    content=memories, content_rowid=id
);

-- FTS5 triggers to keep in sync
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, module_id, kind)
    VALUES (new.id, new.content, new.module_id, new.kind);
END;
```

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

### 3.2 Coordinator Decision Flow (Rules First, AI Assists — spec §7.3)

```
User message arrives
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

### 4.4 PID Lock

```python
# executor_worker.py startup:

def _acquire_pid_lock(self):
    """Ensure only one executor per project."""
    lock_path = os.path.join(tempfile.gettempdir(),
                            f"aming-claw-executor-{self.project_id}.pid")
    if os.path.exists(lock_path):
        old_pid = int(open(lock_path).read().strip())
        if _is_process_alive(old_pid):
            log.warning("Executor already running (PID %d). Killing old.", old_pid)
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(2)
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    return lock_path
```

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

| Phase | Scope | Files | Dependency |
|-------|-------|-------|------------|
| **Phase 1** | Executor lifecycle (crash recovery + pycache) | service_manager, executor_worker, deploy_chain | None |
| **Phase 2** | Memory backend interface + local SQLite FTS5 | memory_backend, memory_service, db, server | None |
| **Phase 3** | Docker backend (wire existing dbservice) | memory_backend | Phase 2 |
| **Phase 4** | Coordinator task awareness | executor_worker | Phase 2 |
| **Phase 5** | Cloud backend stub | memory_backend | Phase 2 |

Phase 1 and 2 can be done in parallel. Phase 4 depends on Phase 2 (search API).

---

## 6. Complete File Change Summary

| File | Action | Phases |
|------|--------|--------|
| `agent/governance/memory_backend.py` | **New** | 2,3,5 |
| `agent/governance/memory_service.py` | **Modify** | 2 |
| `agent/governance/db.py` | **Modify** | 2 |
| `agent/governance/server.py` | **Modify** | 2,4 |
| `agent/service_manager.py` | **Modify** | 1 |
| `agent/executor_worker.py` | **Modify** | 1,4 |
| `agent/deploy_chain.py` | **Modify** | 1 |
| `agent/mcp/tools.py` | **Modify** | 1 |

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
- [ ] PID lock prevents duplicate executors
- [ ] executor_status shows health/restart_count/last_crash
- [ ] Circuit breaker stops restart loop, sends Telegram alert

### Phase 2: Memory Backend (Local)
- [ ] `memories` table + FTS5 in governance.db
- [ ] `MemoryBackend` interface with Local/Docker/Cloud subclasses
- [ ] `MEMORY_BACKEND=local` uses SQLite FTS5 search
- [ ] `/api/mem/{pid}/search?q=...` endpoint works
- [ ] Existing `/api/mem/{pid}/write` and `/query` still work

### Phase 3: Docker Backend
- [ ] `MEMORY_BACKEND=docker` routes search to dbservice :40002
- [ ] Fallback to FTS5 if dbservice unavailable
- [ ] Existing 10 knowledge entries accessible via new interface

### Phase 4: Coordinator Awareness
- [ ] Coordinator prompt includes semantic memory search results
- [ ] Coordinator prompt includes active task queue
- [ ] Duplicate task detected → asks user before re-executing
- [ ] Conflicting queue task detected → warns user with options
- [ ] Past failure detected → injects failure context into new task

### Phase 5: Cloud Backend (Future)
- [ ] `MEMORY_BACKEND=cloud` stub exists
- [ ] Interface defined, not implemented
