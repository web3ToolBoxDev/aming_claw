"""
context_store.py - SQLite + Redis backed context storage with state machine transitions.
"""

import json
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending":    ["processing"],
    "processing": ["qa_review"],
    "qa_review":  ["qa_pass", "qa_fail"],
    "qa_pass":    ["accepted", "archived"],
    "qa_fail":    ["escalated", "archived"],
    "escalated":  ["accepted", "rejected"],
    "accepted":   ["archived"],
    "rejected":   ["archived"],
    "archived":   [],
}


class InvalidTransitionError(Exception):
    """Raised when a state transition is not permitted by VALID_TRANSITIONS."""


class ContextStore:
    def __init__(self, db_path: str, redis_client=None) -> None:
        self._redis = redis_client
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS context (
                    task_id   TEXT PRIMARY KEY,
                    payload   TEXT NOT NULL,
                    status    TEXT NOT NULL DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS outputs (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id   TEXT NOT NULL,
                    output    TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS archived_contexts (
                    task_id   TEXT PRIMARY KEY,
                    payload   TEXT NOT NULL,
                    status    TEXT NOT NULL,
                    created_at DATETIME,
                    archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS context_audit (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    project_id  TEXT,
                    role        TEXT,
                    prompt      TEXT,
                    ai_stdout   TEXT,
                    status      TEXT,
                    duration_ms INTEGER,
                    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                );

                CREATE INDEX IF NOT EXISTS idx_context_audit_session_id
                    ON context_audit(session_id);
            """)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_input(self, task_id: str, payload: dict) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO context (task_id, payload, status)
                VALUES (?, ?, 'pending')
                ON CONFLICT(task_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (task_id, payload_json),
            )
        self._redis_hset(f"ctx:{task_id}", {"payload": payload_json, "status": "pending"})

    def save_output(self, task_id: str, output: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO outputs (task_id, output) VALUES (?, ?)",
                (task_id, output),
            )
        self._redis_hset(f"ctx:{task_id}:outputs", {"latest": output})

    def transition(self, task_id: str, from_state: str, to_state: str) -> None:
        allowed = VALID_TRANSITIONS.get(from_state, [])
        if to_state not in allowed:
            raise InvalidTransitionError(
                f"Transition '{from_state}' -> '{to_state}' is not permitted. "
                f"Allowed: {allowed}"
            )
        with self._conn:
            self._conn.execute(
                """
                UPDATE context
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (to_state, task_id),
            )
        self._redis_hset(f"ctx:{task_id}", {"status": to_state})

    def archive(self, task_id: str) -> None:
        row = self._conn.execute(
            "SELECT task_id, payload, status, created_at FROM context WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            logger.warning("archive: task_id '%s' not found in context table", task_id)
            return
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO archived_contexts (task_id, payload, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["task_id"], row["payload"], row["status"], row["created_at"]),
            )
            self._conn.execute("DELETE FROM context WHERE task_id = ?", (task_id,))
        self._redis_delete(f"ctx:{task_id}")

    def insert_audit(
        self,
        session_id: str,
        project_id: Optional[str],
        role: Optional[str],
        prompt: Optional[str],
        ai_stdout: Optional[str],
        status: Optional[str],
        duration_ms: Optional[int],
    ) -> None:
        """Insert one audit record into context_audit."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO context_audit
                    (session_id, project_id, role, prompt, ai_stdout, status, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, project_id, role, prompt, ai_stdout, status, duration_ms),
            )

    def query_audit_by_session(self, session_id: str) -> list[dict]:
        """Return all audit records for *session_id*, ordered by created_at ASC."""
        cursor = self._conn.execute(
            """
            SELECT session_id, project_id, role, prompt, ai_stdout,
                   status, duration_ms, created_at
            FROM context_audit
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _redis_hset(self, key: str, mapping: dict) -> None:
        if self._redis is None:
            return
        try:
            self._redis.hset(key, mapping=mapping)
        except Exception as exc:
            logger.warning("Redis hset failed for key '%s': %s", key, exc)

    def _redis_delete(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            self._redis.delete(key)
        except Exception as exc:
            logger.warning("Redis delete failed for key '%s': %s", key, exc)


def query_audit_by_session(session_id: str, db_path: Optional[str] = None) -> list:
    """Module-level helper: query audit records for *session_id*.

    When *db_path* is ``None`` the path is derived from the
    ``SHARED_VOLUME_PATH`` environment variable (falling back to
    ``../shared-volume`` relative to this file).  A fresh, short-lived
    connection is used so callers do not need a pre-existing
    :class:`ContextStore` instance.

    Returns a list of dicts with keys:
    ``role``, ``prompt``, ``ai_stdout``, ``status``, ``duration_ms``, ``created_at``.
    Returns an empty list if no records exist or the DB is not yet initialised.
    """
    import os as _os
    if db_path is None:
        shared_vol = _os.getenv(
            "SHARED_VOLUME_PATH",
            _os.path.join(_os.path.dirname(__file__), "..", "shared-volume"),
        )
        db_path = _os.path.join(shared_vol, "context_store.db")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                """
                SELECT role, prompt, ai_stdout, status, duration_ms, created_at
                FROM context_audit
                WHERE session_id = ?
                ORDER BY created_at ASC
                """,
                (session_id,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
        return rows
    except Exception as exc:
        logger.warning("query_audit_by_session('%s') failed: %s", session_id, exc)
        return []


class PromptRenderer:
    def __init__(self, context_store: ContextStore, template_dir: str) -> None:
        self._store = context_store
        self._template_dir = template_dir

    def render_to_file(
        self, task_id: str, template_name: str, output_path: str
    ) -> None:
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError as exc:
            raise ImportError("jinja2 is required for PromptRenderer") from exc

        row = self._store._conn.execute(
            "SELECT payload, status FROM context WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task_id '{task_id}' not found in context store")

        context = {
            "task_id": task_id,
            "status": row["status"],
            **json.loads(row["payload"]),
        }

        env = Environment(
            loader=FileSystemLoader(self._template_dir), autoescape=False
        )
        template = env.get_template(template_name)
        rendered = template.render(**context)

        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(rendered)
