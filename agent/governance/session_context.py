"""Session Context — cross-session state persistence.

Two data structures:
  - snapshot: Current state (replace with optimistic locking via version)
  - log: Append-only event log (messages, actions, decisions)

Storage: Redis (fast) + SQLite (durable fallback).
"""

import json
import logging
import time

log = logging.getLogger(__name__)

MAX_RECENT_MESSAGES = 20
MAX_LOG_ENTRIES = 200


def _utc_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_key(project_id: str) -> str:
    return f"context:snapshot:{project_id}"


def _log_key(project_id: str) -> str:
    return f"context:log:{project_id}"


def save_snapshot(project_id: str, context: dict, expected_version: int = None) -> dict:
    """Save session context snapshot with optimistic locking.

    Args:
        project_id: Project scope
        context: {current_focus, active_nodes, pending_tasks, coordinator_token, chat_id, ...}
        expected_version: If set, raises ConflictError if current version != expected

    Returns: {ok, version}
    """
    from .redis_client import get_redis
    rc = get_redis()
    key = _snapshot_key(project_id)

    # Optimistic lock check
    if expected_version is not None:
        existing = rc.get_json(key)
        current_version = (existing or {}).get("version", 0)
        if current_version != expected_version:
            from .errors import GovernanceError
            raise GovernanceError(
                f"Context version conflict: expected {expected_version}, got {current_version}",
                status=409,
            )

    current = rc.get_json(key) or {}
    new_version = current.get("version", 0) + 1

    context["version"] = new_version
    context["updated_at"] = _utc_iso()
    context["project_id"] = project_id

    # Trim recent_messages
    msgs = context.get("recent_messages", [])
    if len(msgs) > MAX_RECENT_MESSAGES:
        context["recent_messages"] = msgs[-MAX_RECENT_MESSAGES:]

    rc.set_json(key, context, ttl_sec=86400)  # 24h TTL

    # Also persist to SQLite for durability
    _persist_to_sqlite(project_id, context)

    log.info("Context snapshot saved: %s v%d", project_id, new_version)
    return {"ok": True, "version": new_version}


def load_snapshot(project_id: str) -> dict | None:
    """Load the latest session context snapshot.

    Read path: Redis → SQLite fallback.
    """
    from .redis_client import get_redis
    rc = get_redis()
    key = _snapshot_key(project_id)

    # Try Redis
    data = rc.get_json(key)
    if data:
        return data

    # Fallback to SQLite
    data = _load_from_sqlite(project_id)
    if data:
        # Backfill Redis
        rc.set_json(key, data, ttl_sec=86400)
    return data


def append_log(project_id: str, entry_type: str, content: dict) -> dict:
    """Append an entry to the session log.

    Args:
        entry_type: msg_in, msg_out, action, decision
        content: {text, node, action, ...}

    Returns: {ok, log_length}
    """
    from .redis_client import get_redis
    rc = get_redis()
    key = _log_key(project_id)

    entry = {
        "type": entry_type,
        "ts": _utc_iso(),
        **content,
    }

    # Append to Redis list
    entry_json = json.dumps(entry, ensure_ascii=False)
    if rc.available:
        rc._safe(lambda: rc._client.rpush(key, entry_json))
        rc._safe(lambda: rc._client.ltrim(key, -MAX_LOG_ENTRIES, -1))
        rc._safe(lambda: rc._client.expire(key, 86400))
        length = rc._safe(lambda: rc._client.llen(key), 0)
    else:
        length = 0

    return {"ok": True, "log_length": length}


def read_log(project_id: str, limit: int = 50) -> list[dict]:
    """Read recent session log entries."""
    from .redis_client import get_redis
    rc = get_redis()
    key = _log_key(project_id)

    if not rc.available:
        return []

    raw_entries = rc._safe(lambda: rc._client.lrange(key, -limit, -1), [])
    entries = []
    for raw in raw_entries:
        try:
            entries.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return entries


def archive_context(project_id: str) -> dict:
    """Archive valuable content from context to long-term memory before expiry.

    Extracts decisions, pitfalls, and key actions from the log.
    Writes them to governance memory service (or dbservice when available).
    """
    log_entries = read_log(project_id, limit=MAX_LOG_ENTRIES)
    snapshot = load_snapshot(project_id)

    archived = []

    # Extract decisions
    for entry in log_entries:
        if entry.get("type") == "decision":
            archived.append({
                "kind": "verify_decision",
                "content": entry.get("content", entry.get("text", "")),
                "source": "session_archive",
                "ts": entry.get("ts"),
            })

    # Create session summary
    if snapshot:
        summary = {
            "kind": "session_summary",
            "content": json.dumps({
                "focus": snapshot.get("current_focus", ""),
                "active_nodes": snapshot.get("active_nodes", []),
                "pending_tasks": snapshot.get("pending_tasks", []),
                "decisions_count": len(archived),
                "messages_count": len(snapshot.get("recent_messages", [])),
            }, ensure_ascii=False),
            "source": "session_archive",
            "ts": _utc_iso(),
        }
        archived.append(summary)

    # Write to memory service
    try:
        from . import memory_service
        from .models import MemoryEntry
        from .db import get_connection
        conn = get_connection(project_id)
        for item in archived:
            entry = MemoryEntry(
                module_id="session",
                kind=item["kind"],
                content=item["content"],
                created_by="session_archive",
            )
            memory_service.write_memory(conn, project_id, entry)
        conn.close()
    except Exception:
        log.exception("Failed to archive context to memory service")

    # Clear expired context
    from .redis_client import get_redis
    rc = get_redis()
    rc.delete(_snapshot_key(project_id))
    rc.delete(_log_key(project_id))

    log.info("Context archived: %s (%d entries)", project_id, len(archived))
    return {"archived": len(archived), "items": archived}


# --- SQLite persistence (durable fallback) ---

def _persist_to_sqlite(project_id: str, context: dict) -> None:
    """Persist snapshot to SQLite for durability."""
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        now = _utc_iso()
        context_json = json.dumps(context, ensure_ascii=False)

        # Upsert into a simple key-value approach using existing tables
        # Use snapshots table with version=-1 as context storage
        conn.execute(
            """INSERT OR REPLACE INTO snapshots (project_id, version, snapshot_json, created_at, created_by)
               VALUES (?, -1, ?, ?, 'context_service')""",
            (project_id, context_json, now),
        )
        conn.commit()
        conn.close()
    except Exception:
        log.debug("SQLite context persist failed (non-critical)")


def _load_from_sqlite(project_id: str) -> dict | None:
    """Load snapshot from SQLite."""
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        row = conn.execute(
            "SELECT snapshot_json FROM snapshots WHERE project_id = ? AND version = -1",
            (project_id,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["snapshot_json"])
    except Exception:
        log.debug("SQLite context load failed")
    return None
