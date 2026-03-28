"""Chain Context Store — event-sourced task chain runtime context.

Provides in-memory runtime state for auto-chain task progression.
Events drive state updates and are persisted to chain_events table.
Crash recovery replays events from DB to rebuild in-memory state.

Architecture:
    EventBus → ChainContextStore (in-memory dict)
                    │
                    ├── read: O(1) dict lookup
                    ├── write: event-driven, threading.Lock
                    └── persist: sync INSERT to chain_events table

Consistency boundary: single governance process.
"""

import json
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Fields always preserved in full from task result
RESULT_CORE_FIELDS = [
    "target_files", "changed_files", "verification", "requirements",
    "acceptance_criteria", "test_report", "prd", "proposed_nodes",
    "summary", "related_nodes",
]

# Role-based visibility: which stage types each role can see
ROLE_VISIBLE_STAGES = {
    "pm":          lambda s: s.task_type == "pm",
    "dev":         lambda s: s.task_type in ("pm", "dev"),
    "test":        lambda s: s.task_type in ("dev", "test"),
    "qa":          lambda s: s.task_type in ("test", "qa"),
    "merge":       lambda s: s.task_type in ("qa", "merge"),
    "coordinator": lambda s: True,
}

# Role-based visibility: which result_core fields each role can see
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

# Valid chain states
CHAIN_STATES = {
    "running", "blocked", "retrying", "completed",
    "failed", "cancelled", "archived",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_core(result: dict) -> dict:
    """Extract key fields from result for durable storage."""
    if not result:
        return {}
    core = {}
    for field in RESULT_CORE_FIELDS:
        val = result.get(field)
        if val is None and isinstance(result.get("prd"), dict):
            val = result["prd"].get(field)
        if val is not None:
            core[field] = val
    return core


class StageSnapshot:
    """One stage's context snapshot."""
    __slots__ = (
        "task_id", "task_type", "prompt", "result_core", "result_raw",
        "gate_reason", "attempt", "parent_task_id", "ts",
    )

    def __init__(self, task_id, task_type, prompt, parent_task_id=None):
        self.task_id = task_id
        self.task_type = task_type
        self.prompt = prompt
        self.result_core = None
        self.result_raw = None
        self.gate_reason = None
        self.attempt = 1
        self.parent_task_id = parent_task_id
        self.ts = _utc_iso()


class ChainContext:
    """Single chain's runtime context."""
    __slots__ = (
        "root_task_id", "project_id", "stages",
        "current_stage", "state", "created_at", "updated_at",
    )

    def __init__(self, root_task_id, project_id):
        self.root_task_id = root_task_id
        self.project_id = project_id
        self.stages = {}
        self.current_stage = None
        self.state = "running"
        self.created_at = _utc_iso()
        self.updated_at = self.created_at


class ChainContextStore:
    """Process-wide chain context store. Thread-safe."""

    def __init__(self):
        self._chains: dict[str, ChainContext] = {}
        self._task_to_root: dict[str, str] = {}
        self._lock = threading.Lock()
        self._recovering = False  # suppress DB writes during replay

    # ── Event handlers (called by EventBus subscribers) ──

    def on_task_created(self, payload: dict):
        """Handle task.created event."""
        task_id = payload.get("task_id", "")
        parent_id = payload.get("parent_task_id", "")
        task_type = payload.get("type", "task")
        prompt = payload.get("prompt", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            # Skip if already registered (idempotent)
            if task_id in self._task_to_root:
                return

            if parent_id and parent_id in self._task_to_root:
                root_id = self._task_to_root[parent_id]
                chain = self._chains.get(root_id)
                if not chain:
                    return
            else:
                root_id = task_id
                chain = ChainContext(root_id, project_id)
                self._chains[root_id] = chain

            stage = StageSnapshot(task_id, task_type, prompt, parent_id)
            chain.stages[task_id] = stage
            chain.current_stage = task_id
            chain.updated_at = _utc_iso()
            self._task_to_root[task_id] = root_id

        self._persist_event(root_id, task_id, "task.created", payload, project_id)

    def on_task_completed(self, payload: dict):
        """Handle task.completed event."""
        task_id = payload.get("task_id", "")
        result = payload.get("result", {})
        project_id = payload.get("project_id", "")
        task_type = payload.get("type", "pm")

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                # Bootstrap: task was created externally (e.g. initial PM task via user API,
                # not by auto-chain), so task.created was never published for it.
                # Treat it as chain root so events are persisted going forward.
                root_id = task_id
                chain = ChainContext(root_id, project_id)
                stage = StageSnapshot(task_id, task_type, "", None)
                chain.stages[task_id] = stage
                self._chains[root_id] = chain
                self._task_to_root[task_id] = root_id
                log.debug("chain_context: bootstrapped root chain for %s (%s)", task_id, task_type)
            chain = self._chains.get(root_id)
            if not chain or task_id not in chain.stages:
                return
            stage = chain.stages[task_id]
            stage.result_core = _extract_core(result)
            stage.result_raw = result
            chain.updated_at = _utc_iso()
            # If this was a merge, mark chain completed
            if stage.task_type == "merge":
                chain.state = "completed"

        # Persist with core only (not raw) to limit DB size
        persist_payload = {**payload, "result": _extract_core(result)}
        self._persist_event(root_id, task_id, "task.completed",
                            persist_payload, project_id)

    def on_gate_blocked(self, payload: dict):
        """Handle gate.blocked event. Append-only (audit)."""
        task_id = payload.get("task_id", "")
        reason = payload.get("reason", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            chain.state = "blocked"
            if task_id in chain.stages:
                chain.stages[task_id].gate_reason = reason
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, task_id, "gate.blocked",
                            payload, project_id)

    def on_task_retry(self, payload: dict):
        """Handle task.retry event."""
        retry_id = payload.get("task_id", "")
        original_id = payload.get("original_task_id", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(original_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return

            chain.state = "retrying"
            original = chain.stages.get(original_id)
            if original:
                stage = StageSnapshot(
                    retry_id, original.task_type,
                    original.prompt, original_id,
                )
                stage.attempt = original.attempt + 1
                chain.stages[retry_id] = stage
                chain.current_stage = retry_id
            self._task_to_root[retry_id] = root_id
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, retry_id, "task.retry",
                            payload, project_id)

    def on_task_failed(self, payload: dict):
        """Handle task.failed event (retry exhausted). Auto-archives to release memory."""
        task_id = payload.get("task_id", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            chain.state = "failed"
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, task_id, "task.failed",
                            payload, project_id)

        # Auto-archive failed chains to prevent memory leak
        if not self._recovering:
            self.archive_chain(task_id, project_id)

    # ── Read API (from memory, O(1)) ──

    def get_chain(self, task_id: str, role: str = None) -> dict | None:
        """Get chain context for a task, optionally filtered by role."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            if not chain:
                return None
            return self._serialize(chain, role)

    def get_original_prompt(self, task_id: str) -> str:
        """Get root task prompt (no role filter). For retry prompt building."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return ""
            chain = self._chains.get(root_id)
            if not chain:
                return ""
            root_stage = chain.stages.get(root_id)
            return root_stage.prompt if root_stage else ""

    def get_parent_result(self, task_id: str) -> dict | None:
        """Get parent stage result_core (no role filter). For prompt fallback."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            if not chain:
                return None
            stage = chain.stages.get(task_id)
            if stage and stage.parent_task_id:
                parent = chain.stages.get(stage.parent_task_id)
                return parent.result_core if parent else None
            return None

    def get_state(self, task_id: str) -> str | None:
        """Get current chain state."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            return chain.state if chain else None

    # ── Archive ──

    def archive_chain(self, task_id: str, project_id: str = ""):
        """Mark chain as archived, release memory."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            pid = project_id or chain.project_id
            chain.state = "archived"

        self._persist_event(root_id, task_id, "chain.archived",
                            {"root_task_id": root_id, "archived_at": _utc_iso()},
                            pid)

        # Release memory
        with self._lock:
            chain = self._chains.pop(root_id, None)
            if chain:
                for tid in list(chain.stages.keys()):
                    self._task_to_root.pop(tid, None)

        log.info("chain_context: archived chain %s (%d stages)",
                 root_id, len(chain.stages) if chain else 0)

    # ── Crash Recovery ──

    def recover_from_db(self, project_id: str):
        """Replay chain_events to rebuild in-memory state for active chains."""
        try:
            from .db import get_connection
            conn = get_connection(project_id)
        except Exception:
            log.warning("chain_context: cannot open DB for recovery (%s)", project_id)
            return

        try:
            rows = conn.execute(
                "SELECT root_task_id, task_id, event_type, payload_json, ts "
                "FROM chain_events "
                "WHERE root_task_id NOT IN ("
                "  SELECT root_task_id FROM chain_events "
                "  WHERE event_type = 'chain.archived'"
                ") ORDER BY ts"
            ).fetchall()
        except Exception:
            log.debug("chain_context: chain_events table not found, skip recovery")
            return
        finally:
            conn.close()

        if not rows:
            return

        self._recovering = True
        handlers = {
            "task.created": self.on_task_created,
            "task.completed": self.on_task_completed,
            "gate.blocked": self.on_gate_blocked,
            "task.retry": self.on_task_retry,
            "task.failed": self.on_task_failed,
        }

        count = 0
        for row in rows:
            handler = handlers.get(row["event_type"])
            if handler:
                try:
                    payload = json.loads(row["payload_json"])
                    handler(payload)
                    count += 1
                except Exception:
                    log.debug("chain_context: skip bad event %s/%s",
                              row["task_id"], row["event_type"])

        self._recovering = False
        log.info("chain_context: recovered %d events, %d active chains for %s",
                 count, len(self._chains), project_id)

    # ── Serialization ──

    def _serialize(self, chain: ChainContext, role: str = None) -> dict:
        """Serialize chain to dict, optionally filtered by role."""
        stage_filter = ROLE_VISIBLE_STAGES.get(role, lambda s: True) if role else lambda s: True
        result_fields = ROLE_RESULT_FIELDS.get(role) if role else None

        stages = []
        for s in chain.stages.values():
            if not stage_filter(s):
                continue

            result_data = s.result_core or {}
            if result_fields is not None:
                result_data = {k: v for k, v in result_data.items()
                               if k in result_fields}

            stage_dict = {
                "task_id": s.task_id,
                "type": s.task_type,
                "attempt": s.attempt,
            }
            # Prompt visibility: dev/pm get full, coordinator gets truncated, others none
            if role in (None, "pm", "dev"):
                stage_dict["prompt"] = s.prompt
            elif role == "coordinator":
                stage_dict["prompt"] = s.prompt[:200] if s.prompt else ""

            if result_data:
                stage_dict["result_core"] = result_data

            # Gate reason: only visible to own stage (dev retry) and coordinator
            if s.gate_reason and (role in (None, "coordinator") or
                                   s.task_type == role):
                stage_dict["gate_reason"] = s.gate_reason

            stages.append(stage_dict)

        return {
            "root_task_id": chain.root_task_id,
            "project_id": chain.project_id,
            "state": chain.state,
            "current_stage": chain.current_stage,
            "stage_count": len(chain.stages),
            "stages": stages,
            "created_at": chain.created_at,
            "updated_at": chain.updated_at,
        }

    # ── DB Persistence ──

    def _persist_event(self, root_task_id: str, task_id: str,
                       event_type: str, payload: dict, project_id: str):
        """Append event to chain_events table. Non-blocking, best-effort."""
        if self._recovering:
            return  # Don't write back to DB during replay

        try:
            from .db import get_connection
            conn = get_connection(project_id)
            conn.execute(
                "INSERT INTO chain_events "
                "(root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (root_task_id, task_id, event_type,
                 json.dumps(payload, ensure_ascii=False, default=str)[:20000],
                 _utc_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.debug("chain_context: persist event failed (%s/%s)",
                      task_id, event_type, exc_info=True)

    def _project_id_for(self, root_task_id: str) -> str:
        chain = self._chains.get(root_task_id)
        return chain.project_id if chain else ""


# ── Singleton + EventBus registration ──

_store = ChainContextStore()


def get_store() -> ChainContextStore:
    return _store


def register_events():
    """Subscribe store handlers to EventBus. Call once on startup."""
    from . import event_bus
    bus = event_bus.get_event_bus()
    bus.subscribe("task.created", _store.on_task_created)
    bus.subscribe("task.completed", _store.on_task_completed)
    bus.subscribe("gate.blocked", _store.on_gate_blocked)
    bus.subscribe("task.retry", _store.on_task_retry)
    bus.subscribe("task.failed", _store.on_task_failed)
    log.info("chain_context: registered EventBus subscribers")
