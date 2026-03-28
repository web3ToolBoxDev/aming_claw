"""Task Registry — SQLite-backed task lifecycle management (v5).

Dual-field state model:
  execution_status: queued → claimed → running → succeeded/failed/timed_out/cancelled
  notification_status: none → pending → sent → read

Supports: retry, priority, assignment, fencing token, progress heartbeat.
"""

import json
import logging
import os
import sqlite3
import time
import uuid

log = logging.getLogger(__name__)

EXECUTION_STATUSES = {
    "queued", "claimed", "running", "waiting_human", "blocked",
    "succeeded", "failed", "cancelled", "timed_out", "enqueue_failed",
    "design_mismatch",
}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}

NOTIFICATION_STATUSES = {"none", "pending", "sent", "read"}

# Backward compat
VALID_STATUSES = EXECUTION_STATUSES


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_task_id() -> str:
    return f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _utc_iso_after(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    parent_task_id: str = None,
    retry_round: int = 0,
) -> dict:
    """Create a new task."""
    task_id = _new_task_id()
    now = _utc_iso()

    # Auto-store original prompt for retry context recovery
    metadata = metadata or {}
    if "_original_prompt" not in metadata:
        metadata["_original_prompt"] = prompt

    notify = "pending" if metadata.get("chat_id") else "none"
    conn.execute(
        """INSERT INTO tasks
           (task_id, project_id, status, execution_status, notification_status,
            type, prompt, related_nodes,
            created_by, created_at, updated_at, priority, max_attempts, metadata_json,
            parent_task_id, retry_round)
           VALUES (?, ?, 'queued', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, project_id, notify,
            task_type, prompt,
            json.dumps(related_nodes or []),
            created_by, now, now, priority, max_attempts,
            json.dumps(metadata or {}),
            parent_task_id, retry_round,
        ),
    )

    log.info("Task created: %s (project: %s, type: %s, retry_round: %d)", task_id, project_id, task_type, retry_round)
    return {
        "task_id": task_id,
        "project_id": project_id,
        "status": "created",
        "type": task_type,
        "created_at": now,
    }


def claim_task(
    conn: sqlite3.Connection,
    project_id: str,
    assigned_to: str,
    worker_id: str = "",
) -> tuple[dict, str] | tuple[None, str]:
    """Claim the next available task with fencing token.

    Returns (task_dict, fence_token) or (None, "") if no tasks.
    """
    now = _utc_iso()
    row = conn.execute(
        """SELECT task_id, type, prompt, related_nodes, priority, attempt_count, max_attempts, metadata_json
           FROM tasks
           WHERE project_id = ? AND execution_status IN ('queued')
           ORDER BY priority DESC, created_at ASC
           LIMIT 1""",
        (project_id,),
    ).fetchone()

    if not row:
        return None, ""

    task_id = row["task_id"]
    attempt_num = row["attempt_count"] + 1
    fence_token = f"fence-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    lease_expires = _utc_iso_after(300)  # 5 min lease

    # CAS update: only queued → claimed
    result = conn.execute(
        """UPDATE tasks SET status = 'claimed', execution_status = 'claimed',
           assigned_to = ?, started_at = ?, updated_at = ?, attempt_count = ?,
           metadata_json = json_set(COALESCE(metadata_json, '{}'),
             '$.fence_token', ?,
             '$.lease_owner', ?,
             '$.lease_expires_at', ?,
             '$.worker_pid', ?
           )
           WHERE task_id = ? AND execution_status IN ('queued')""",
        (assigned_to, now, now, attempt_num,
         fence_token, worker_id or assigned_to, lease_expires, str(os.getpid()),
         task_id),
    )
    if result.rowcount == 0:
        return None, ""  # Already claimed by another worker

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
    }, fence_token


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    status: str = "succeeded",
    result: dict = None,
    error_message: str = "",
    fence_token: str = "",
    project_id: str = "",
    completed_by: str = "",
    override_reason: str = "",
) -> dict:
    """Mark a task as completed (succeeded/failed). Dual-field update."""
    if status not in ("succeeded", "failed", "timed_out"):
        from .errors import ValidationError
        raise ValidationError(f"Invalid completion status: {status}")

    now = _utc_iso()
    row = conn.execute(
        "SELECT attempt_count, max_attempts, notification_status, metadata_json, assigned_to FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    # M1: Ownership check — only assignee or observer can complete
    assigned_to = row["assigned_to"] or ""
    if completed_by and assigned_to and completed_by != assigned_to:
        is_observer = completed_by.startswith("observer")
        if not is_observer:
            from .errors import GovernanceError
            raise GovernanceError(
                f"Ownership violation: task assigned to {assigned_to}, "
                f"completed_by {completed_by}", 403)
        # M2: Observer override — allow but audit + warn
        log.warning("task_registry: observer override: %s completing task %s "
                     "assigned to %s (reason: %s)",
                     completed_by, task_id, assigned_to,
                     override_reason or "not provided")
        try:
            from . import event_bus, audit_service
            event_bus.publish("task.observer_override", {
                "project_id": project_id,
                "task_id": task_id,
                "assigned_to": assigned_to,
                "override_by": completed_by,
                "override_reason": override_reason,
            })
            audit_service.record(
                conn, project_id, "task.observer_override",
                actor=completed_by,
                details={
                    "task_id": task_id,
                    "assigned_to": assigned_to,
                    "override_reason": override_reason,
                },
            )
        except Exception:
            pass  # audit failure should not block completion

    # Fence token check (if provided)
    if fence_token:
        stored_fence = json.loads(row["metadata_json"] or "{}").get("fence_token", "")
        if stored_fence and stored_fence != fence_token:
            from .errors import GovernanceError
            raise GovernanceError("Fence token mismatch: task reclaimed by another worker", 409)

    # Determine execution status
    exec_status = status
    if status == "failed" and row["attempt_count"] < row["max_attempts"]:
        exec_status = "queued"  # Auto-retry

    # Determine notification status
    notify_status = row["notification_status"]
    if exec_status in TERMINAL_STATUSES and notify_status == "none":
        # Has chat_id → needs notification
        meta = json.loads(row["metadata_json"] or "{}")
        if meta.get("chat_id"):
            notify_status = "pending"

    conn.execute(
        """UPDATE tasks SET status = ?, execution_status = ?,
           notification_status = ?,
           completed_at = ?, updated_at = ?,
           result_json = ?, error_message = ?
           WHERE task_id = ?""",
        (exec_status, exec_status, notify_status,
         now, now,
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

    response = {
        "task_id": task_id,
        "status": exec_status,
        "retrying": exec_status == "queued",
        "completed_at": now,
    }

    # Auto-chain: dispatch next stage on success
    if exec_status == "succeeded" and project_id:
        try:
            from . import auto_chain
            meta = json.loads(row["metadata_json"] or "{}")
            type_row = conn.execute(
                "SELECT type FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            # Commit before auto_chain opens its independent connection.
            # Without this, the caller's open transaction holds a write lock and
            # auto_chain's separate conn fails with "database is locked".
            conn.commit()
            chain_result = auto_chain.on_task_completed(
                conn, project_id, task_id,
                task_type=type_row["type"] if type_row else "task",
                status=exec_status,
                result=result or {},
                metadata=meta,
            )
            if chain_result:
                response["auto_chain"] = chain_result
        except Exception:
            import traceback as _tb
            _tb.print_exc()

    return response


def cancel_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Cancel a task."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET status = 'cancelled', execution_status = 'cancelled',
           updated_at = ?
           WHERE task_id = ? AND execution_status NOT IN ('succeeded', 'failed', 'cancelled', 'timed_out')""",
        (now, task_id),
    )
    return {"task_id": task_id, "status": "cancelled"}


def mark_notified(conn: sqlite3.Connection, task_id: str) -> dict:
    """Mark a task's notification as sent."""
    now = _utc_iso()
    conn.execute(
        "UPDATE tasks SET notification_status = 'sent', notified_at = ? WHERE task_id = ?",
        (now, task_id),
    )
    return {"task_id": task_id, "notification_status": "sent"}


def list_pending_notifications(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """List tasks that need notification (execution done but user not notified)."""
    rows = conn.execute(
        """SELECT task_id, execution_status, result_json, error_message,
                  completed_at, metadata_json
           FROM tasks
           WHERE project_id = ? AND notification_status = 'pending'
             AND execution_status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
           ORDER BY completed_at ASC""",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_progress(conn: sqlite3.Connection, task_id: str,
                    phase: str, percent: int, message: str) -> dict:
    """Update task progress heartbeat."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET
           execution_status = 'running',
           updated_at = ?,
           metadata_json = json_set(COALESCE(metadata_json, '{}'),
             '$.progress_phase', ?,
             '$.progress_percent', ?,
             '$.progress_message', ?,
             '$.progress_at', ?,
             '$.lease_expires_at', ?
           )
           WHERE task_id = ? AND execution_status IN ('claimed', 'running')""",
        (now, phase, percent, message, now, _utc_iso_after(300), task_id),
    )
    return {"task_id": task_id, "phase": phase, "percent": percent}


def recover_stale_tasks(conn: sqlite3.Connection, project_id: str) -> dict:
    """Recover tasks with expired leases — re-queue them."""
    now = _utc_iso()
    rows = conn.execute(
        """SELECT task_id FROM tasks
           WHERE project_id = ? AND execution_status IN ('claimed', 'running')
             AND json_extract(metadata_json, '$.lease_expires_at') < ?""",
        (project_id, now),
    ).fetchall()

    recovered = 0
    for row in rows:
        conn.execute(
            "UPDATE tasks SET execution_status = 'queued', status = 'queued' WHERE task_id = ?",
            (row["task_id"],),
        )
        recovered += 1
        log.info("Recovered stale task: %s", row["task_id"])

    return {"recovered": recovered}


def list_tasks(
    conn: sqlite3.Connection,
    project_id: str,
    status: str = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks for a project."""
    cols = """task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, attempt_count, priority,
                      result_json, metadata_json"""
    if status:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ? AND status = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields for API consumers
        for field in ("result_json", "metadata_json"):
            raw = d.get(field)
            if raw and isinstance(raw, str):
                try:
                    d[field.replace("_json", "")] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[field.replace("_json", "")] = raw
            else:
                d[field.replace("_json", "")] = raw
        results.append(d)
    return results


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


def escalate_task(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Escalate a task via QA→Dev retry loop (max 3 rounds).

    - retry_round < 3: increment retry_round, create a child task with parent linkage,
      return new task_id.
    - retry_round >= 3: mark task as design_mismatch, log user notification, return None.
    """
    row = conn.execute(
        """SELECT project_id, type, prompt, related_nodes, created_by, priority,
                  max_attempts, metadata_json, retry_round
           FROM tasks WHERE task_id = ?""",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    retry_round = row["retry_round"] or 0

    if retry_round < 3:
        new_round = retry_round + 1
        result = create_task(
            conn,
            project_id=row["project_id"],
            prompt=row["prompt"],
            task_type=row["type"],
            related_nodes=json.loads(row["related_nodes"] or "[]"),
            created_by=row["created_by"] or "",
            priority=row["priority"],
            max_attempts=row["max_attempts"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            parent_task_id=task_id,
            retry_round=new_round,
        )
        log.info(
            "Escalated task %s → %s (retry_round=%d)",
            task_id, result["task_id"], new_round,
        )
        return result["task_id"]
    else:
        now = _utc_iso()
        conn.execute(
            """UPDATE tasks SET status = 'design_mismatch', execution_status = 'design_mismatch',
               updated_at = ? WHERE task_id = ?""",
            (now, task_id),
        )
        log.warning(
            "Task %s reached max escalation (retry_round=%d) — marked design_mismatch. "
            "Manual intervention required.",
            task_id, retry_round,
        )
        return None
