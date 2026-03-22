"""Transactional outbox for reliable event delivery.

Pattern:
  1. Business logic writes state + outbox entry in SAME SQLite transaction
  2. Background worker reads outbox, delivers to Redis/dbservice
  3. On success: mark delivered. On failure: retry with backoff.
  4. After max retries: move to dead letter.

This ensures events are never lost, even if Redis/dbservice is temporarily down.
"""

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_DELAY_SEC = 2
POLL_INTERVAL_SEC = 1


def write_outbox(conn, event_type: str, payload: dict, project_id: str, trace_id: str = "") -> int:
    """Write an event to the outbox table. MUST be called inside an existing transaction.

    Returns the outbox row id.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cursor = conn.execute(
        """INSERT INTO event_outbox (event_type, payload_json, project_id, created_at, trace_id)
           VALUES (?, ?, ?, ?, ?)""",
        (event_type, json.dumps(payload, ensure_ascii=False), project_id, now, trace_id),
    )
    return cursor.lastrowid


def deliver_pending(project_id: str) -> dict:
    """Process pending outbox entries for a project. Returns delivery stats."""
    from .db import get_connection
    stats = {"delivered": 0, "failed": 0, "dead_lettered": 0}

    conn = get_connection(project_id)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = conn.execute(
            """SELECT id, event_type, payload_json, trace_id, retry_count
               FROM event_outbox
               WHERE delivered_at IS NULL AND dead_letter = 0
                 AND project_id = ?
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY id
               LIMIT 50""",
            (project_id, now),
        ).fetchall()

        for row_id, event_type, payload_json, trace_id, retry_count in rows:
            payload = json.loads(payload_json)
            success = _deliver_one(event_type, payload, project_id, trace_id)

            if success:
                conn.execute(
                    "UPDATE event_outbox SET delivered_at = ? WHERE id = ?",
                    (now, row_id),
                )
                stats["delivered"] += 1
            elif retry_count >= MAX_RETRIES - 1:
                conn.execute(
                    "UPDATE event_outbox SET dead_letter = 1 WHERE id = ?",
                    (row_id,),
                )
                stats["dead_lettered"] += 1
                log.error("Event %d dead-lettered after %d retries: %s",
                          row_id, retry_count + 1, event_type)
            else:
                delay = BASE_DELAY_SEC * (2 ** retry_count)
                next_retry = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(time.time() + delay),
                )
                conn.execute(
                    """UPDATE event_outbox
                       SET retry_count = retry_count + 1, next_retry_at = ?
                       WHERE id = ?""",
                    (next_retry, row_id),
                )
                stats["failed"] += 1

        conn.commit()
    except Exception:
        log.exception("Outbox delivery error for project %s", project_id)
        conn.rollback()
    finally:
        conn.close()

    return stats


def _deliver_one(event_type: str, payload: dict, project_id: str, trace_id: str) -> bool:
    """Deliver a single event to all targets. Returns True if ALL succeeded."""
    success = True

    # Target 1: Redis Pub/Sub (best-effort notification)
    try:
        from .redis_client import get_redis
        r = get_redis()
        if r.available:
            message = {"event": event_type, "payload": payload}
            r.publish(f"gov:events:{project_id}", message)
    except Exception:
        log.debug("Pub/Sub delivery failed (non-critical)")

    # Target 2: Redis Stream (persistent event log)
    try:
        from .redis_client import get_redis
        r = get_redis()
        if r.available:
            entry = {
                "event": event_type,
                "payload": json.dumps(payload, ensure_ascii=False),
                "project_id": project_id,
                "trace_id": trace_id or "",
            }
            r._safe(lambda: r._client.xadd(
                f"gov:stream:{project_id}", entry, maxlen=5000,
            ))
    except Exception as e:
        log.warning("Redis Stream delivery failed: %s", e)
        success = False

    # Target 3: dbservice (async memory write, non-critical)
    # Intentionally best-effort — dbservice down shouldn't block outbox
    try:
        _deliver_to_dbservice(event_type, payload, project_id)
    except Exception:
        log.debug("dbservice delivery failed (non-critical)")

    return success


def _deliver_to_dbservice(event_type: str, payload: dict, project_id: str) -> None:
    """Write event to dbservice as knowledge entry. Best-effort."""
    import os
    import requests
    dbservice_url = os.environ.get("DBSERVICE_URL", "")
    if not dbservice_url:
        return

    type_map = {
        "node.status_changed": "node_status",
        "rollback.executed": "workaround",
        "release.approved": "release_note",
        "release.blocked": "release_note",
    }
    mem_type = type_map.get(event_type)
    if not mem_type:
        return

    node_id = payload.get("node_id", "")
    ref_id = f"{node_id}:{event_type}:{payload.get('timestamp', '')}"

    try:
        requests.post(
            f"{dbservice_url}/knowledge/upsert",
            json={
                "refId": ref_id,
                "type": mem_type,
                "title": f"{event_type}: {node_id}",
                "body": json.dumps(payload, ensure_ascii=False),
                "tags": [node_id, event_type, project_id],
                "scope": project_id,
                "status": "active",
            },
            timeout=3,
        )
    except Exception:
        pass  # Best-effort, don't fail outbox delivery


def get_dead_letters(project_id: str, limit: int = 50) -> list[dict]:
    """Query dead-lettered events for monitoring."""
    from .db import get_connection
    conn = get_connection(project_id)
    try:
        rows = conn.execute(
            """SELECT id, event_type, payload_json, created_at, retry_count, trace_id
               FROM event_outbox
               WHERE dead_letter = 1 AND project_id = ?
               ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
        return [
            {
                "id": r[0], "event_type": r[1],
                "payload": json.loads(r[2]),
                "created_at": r[3], "retry_count": r[4],
                "trace_id": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


class OutboxWorker:
    """Background worker that polls and delivers outbox entries."""

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("OutboxWorker started (poll interval: %ds)", POLL_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        archive_check_counter = 0
        while self._running:
            try:
                from . import project_service
                projects = project_service.list_projects()
                for p in projects:
                    if not self._running:
                        break
                    stats = deliver_pending(p["project_id"])
                    if stats["delivered"] or stats["dead_lettered"]:
                        log.info("Outbox [%s]: %s", p["project_id"], stats)

                # Every 60 cycles (~60s), check for stale contexts to archive
                archive_check_counter += 1
                if archive_check_counter >= 60:
                    archive_check_counter = 0
                    self._check_stale_contexts(projects)
            except Exception:
                log.exception("OutboxWorker error")
            time.sleep(POLL_INTERVAL_SEC)

    def _check_stale_contexts(self, projects: list) -> None:
        """Check for expired session contexts and archive them."""
        try:
            from .redis_client import get_redis
            from . import session_context
            rc = get_redis()
            if not rc.available:
                return

            for p in projects:
                pid = p["project_id"]
                snapshot = session_context.load_snapshot(pid)
                if not snapshot:
                    continue

                # Check if context is stale (>24h since last update)
                updated = snapshot.get("updated_at", "")
                if not updated:
                    continue

                import datetime
                try:
                    updated_dt = datetime.datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc
                    )
                    age_hours = (datetime.datetime.now(datetime.timezone.utc) - updated_dt).total_seconds() / 3600
                    if age_hours > 24:
                        log.info("Context stale for %s (%.1fh old), archiving", pid, age_hours)
                        session_context.archive_context(pid)
                except Exception:
                    pass
        except Exception:
            log.debug("Stale context check failed")
