"""
workspace_queue.py - Per-workspace task queuing with auto-launch on acceptance.

When a task targets a workspace that already has an active (processing) task,
the new task is queued instead of being dispatched immediately. When the
preceding task in that workspace is accepted, the next queued task is
automatically promoted to pending for execution.

Queue state is persisted in state/workspace_task_queue.json:
  {
    "version": 1,
    "queues": {
      "<ws_id>": [
        {
          "task_id": "task-...",
          "task_code": "T0001",
          "chat_id": 12345,
          "user_id": 67890,
          "text": "...",
          "action": "codex",
          "queued_at": "<iso>",
        },
        ...
      ]
    },
    "updated_at": "<iso>",
  }
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from typing import Dict, List, Optional

from utils import load_json, save_json, tasks_root, utc_iso


def _queue_file() -> Path:
    return tasks_root() / "state" / "workspace_task_queue.json"


def _load_queue() -> Dict:
    p = _queue_file()
    if not p.exists():
        return {"version": 1, "queues": {}, "updated_at": utc_iso()}
    try:
        data = load_json(p)
        if not isinstance(data.get("queues"), dict):
            data["queues"] = {}
        return data
    except Exception:
        return {"version": 1, "queues": {}, "updated_at": utc_iso()}


def _save_queue(data: Dict) -> None:
    data["updated_at"] = utc_iso()
    save_json(_queue_file(), data)


def enqueue_task(ws_id: str, task_info: Dict) -> int:
    """Add a task to the workspace queue. Returns new queue position (1-based)."""
    data = _load_queue()
    queues = data.get("queues", {})
    if ws_id not in queues:
        queues[ws_id] = []
    entry = {
        "task_id": str(task_info.get("task_id", "")),
        "task_code": str(task_info.get("task_code", "")),
        "chat_id": int(task_info.get("chat_id", 0)),
        "user_id": int(task_info.get("user_id") or task_info.get("requested_by") or 0),
        "text": str(task_info.get("text", "")),
        "action": str(task_info.get("action", "codex")),
        "queued_at": utc_iso(),
    }
    queues[ws_id].append(entry)
    data["queues"] = queues
    _save_queue(data)
    return len(queues[ws_id])


def dequeue_task(ws_id: str) -> Optional[Dict]:
    """Pop the first queued task for a workspace. Returns None if empty."""
    data = _load_queue()
    queues = data.get("queues", {})
    q = queues.get(ws_id, [])
    if not q:
        return None
    entry = q.pop(0)
    queues[ws_id] = q
    data["queues"] = queues
    _save_queue(data)
    return entry


def peek_queue(ws_id: str) -> Optional[Dict]:
    """Look at the first queued task without removing it."""
    data = _load_queue()
    q = data.get("queues", {}).get(ws_id, [])
    return q[0] if q else None


def list_queue(ws_id: str) -> List[Dict]:
    """Return all queued tasks for a workspace."""
    data = _load_queue()
    return list(data.get("queues", {}).get(ws_id, []))


def queue_length(ws_id: str) -> int:
    """Return the number of queued tasks for a workspace."""
    data = _load_queue()
    return len(data.get("queues", {}).get(ws_id, []))


def remove_from_queue(ws_id: str, task_id: str) -> bool:
    """Remove a specific task from a workspace queue. Returns True if found."""
    data = _load_queue()
    queues = data.get("queues", {})
    q = queues.get(ws_id, [])
    before = len(q)
    q = [t for t in q if t.get("task_id") != task_id]
    if len(q) < before:
        queues[ws_id] = q
        data["queues"] = queues
        _save_queue(data)
        return True
    return False


def list_all_queues() -> Dict[str, List[Dict]]:
    """Return all queues for status display."""
    data = _load_queue()
    return dict(data.get("queues", {}))


def has_active_task_in_workspace(ws_id: str) -> bool:
    """Check if workspace has any processing task.

    Looks at task runtime state for tasks targeting this workspace
    that are in 'processing' status.
    """
    from task_state import load_runtime_state
    state = load_runtime_state()
    active = state.get("active", {})
    for entry in active.values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).strip().lower()
        if status != "processing":
            continue
        # Check if this task targets the workspace
        task_ws_id = str(entry.get("target_workspace_id", "")).strip()
        if task_ws_id == ws_id:
            return True
    return False


def should_queue_task(ws_id: str) -> bool:
    """Determine if a new task for this workspace should be queued.

    Returns True if workspace already has an active (processing or pending_acceptance) task.
    """
    from task_state import load_runtime_state, load_task_status
    state = load_runtime_state()
    active = state.get("active", {})
    for entry in active.values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).strip().lower()
        if status not in ("processing", "pending_acceptance"):
            continue
        task_ws_id = str(entry.get("target_workspace_id", "")).strip()
        if task_ws_id == ws_id:
            return True
    return False


def promote_next_queued_task(ws_id: str) -> Optional[Dict]:
    """Promote the next queued task for a workspace to pending.

    Called after a task is accepted. Creates a pending task file
    and returns the task info, or None if queue is empty.
    """
    entry = dequeue_task(ws_id)
    if not entry:
        return None

    from utils import new_task_id, task_file
    from task_state import register_task_created

    task_id = new_task_id()
    task = {
        "task_id": task_id,
        "chat_id": int(entry.get("chat_id", 0)),
        "requested_by": int(entry.get("user_id", 0)),
        "action": str(entry.get("action", "codex")),
        "text": str(entry.get("text", "")),
        "status": "pending",
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
        "target_workspace_id": ws_id,
        "queued_task_id": str(entry.get("task_id", "")),
        "queued_task_code": str(entry.get("task_code", "")),
    }

    task["task_code"] = register_task_created(task)
    save_json(task_file("pending", task_id), task)
    return task
