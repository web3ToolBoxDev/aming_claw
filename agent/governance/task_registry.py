"""Task Registry — SQLite-backed task lifecycle management.

Replaces file-based task tracking with proper state machine:
  created → queued → running → succeeded / failed / cancelled

Supports: retry, priority, assignment, result storage.
"""

import json
import logging
import sqlite3
import time
import uuid

log = logging.getLogger(__name__)

VALID_STATUSES = {"created", "queued", "running", "succeeded", "failed", "cancelled"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_task_id() -> str:
    return f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def create_task(
    conn: sqlite3.Connection,
    project_id: str,
    prompt: str,
    task_type: str = "task",
    related_nodes: list[str] = None,
    created_by: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    metadata: dict = None,
) -> dict:
    """Create a new task."""
    task_id = _new_task_id()
    now = _utc_iso()

    conn.execute(
        """INSERT INTO tasks
           (task_id, project_id, status, type, prompt, related_nodes,
            created_by, created_at, updated_at, priority, max_attempts, metadata_json)
           VALUES (?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, project_id, task_type, prompt,
            json.dumps(related_nodes or []),
            created_by, now, now, priority, max_attempts,
            json.dumps(metadata or {}),
        ),
    )

    log.info("Task created: %s (project: %s, type: %s)", task_id, project_id, task_type)
    return {
        "task_id": task_id,
        "project_id": project_id,
        "status": "created",
        "type": task_type,
        "created_at": now,
    }


def claim_task(conn: sqlite3.Connection, project_id: str, assigned_to: str) -> dict | None:
    """Claim the next available task (FIFO by priority then creation time).

    Returns task dict or None if no tasks available.
    """
    now = _utc_iso()
    row = conn.execute(
        """SELECT task_id, type, prompt, related_nodes, priority, attempt_count, max_attempts, metadata_json
           FROM tasks
           WHERE project_id = ? AND status IN ('created', 'queued')
           ORDER BY priority DESC, created_at ASC
           LIMIT 1""",
        (project_id,),
    ).fetchone()

    if not row:
        return None

    task_id = row["task_id"]
    attempt_num = row["attempt_count"] + 1

    conn.execute(
        """UPDATE tasks SET status = 'running', assigned_to = ?,
           started_at = ?, updated_at = ?, attempt_count = ?
           WHERE task_id = ?""",
        (assigned_to, now, now, attempt_num, task_id),
    )

    conn.execute(
        """INSERT INTO task_attempts (task_id, attempt_num, status, started_at)
           VALUES (?, ?, 'running', ?)""",
        (task_id, attempt_num, now),
    )

    return {
        "task_id": task_id,
        "type": row["type"],
        "prompt": row["prompt"],
        "related_nodes": json.loads(row["related_nodes"] or "[]"),
        "priority": row["priority"],
        "attempt_num": attempt_num,
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    status: str = "succeeded",
    result: dict = None,
    error_message: str = "",
) -> dict:
    """Mark a task as completed (succeeded/failed)."""
    if status not in ("succeeded", "failed"):
        from .errors import ValidationError
        raise ValidationError(f"Invalid completion status: {status}")

    now = _utc_iso()
    row = conn.execute(
        "SELECT attempt_count, max_attempts FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    # Update task
    final_status = status
    if status == "failed" and row["attempt_count"] < row["max_attempts"]:
        final_status = "queued"  # Auto-retry

    conn.execute(
        """UPDATE tasks SET status = ?, completed_at = ?, updated_at = ?,
           result_json = ?, error_message = ?
           WHERE task_id = ?""",
        (final_status, now, now,
         json.dumps(result or {}, ensure_ascii=False), error_message,
         task_id),
    )

    # Update attempt
    conn.execute(
        """UPDATE task_attempts SET status = ?, completed_at = ?,
           result_json = ?, error_message = ?
           WHERE task_id = ? AND status = 'running'""",
        (status, now, json.dumps(result or {}, ensure_ascii=False),
         error_message, task_id),
    )

    return {
        "task_id": task_id,
        "status": final_status,
        "retrying": final_status == "queued",
        "completed_at": now,
    }


def cancel_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Cancel a task."""
    now = _utc_iso()
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE task_id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled')",
        (now, task_id),
    )
    return {"task_id": task_id, "status": "cancelled"}


def list_tasks(
    conn: sqlite3.Connection,
    project_id: str,
    status: str = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks for a project."""
    if status:
        rows = conn.execute(
            """SELECT task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, attempt_count, priority
               FROM tasks WHERE project_id = ? AND status = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, attempt_count, priority
               FROM tasks WHERE project_id = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()

    return [dict(r) for r in rows]


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """Get a single task with attempts."""
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return None

    task = dict(row)
    attempts = conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_num",
        (task_id,),
    ).fetchall()
    task["attempts"] = [dict(a) for a in attempts]
    return task
