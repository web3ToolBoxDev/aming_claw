"""SQLite database layer for governance runtime state.

Manages:
  - Connection lifecycle (per-project databases)
  - Schema creation and migration
  - WAL mode for concurrent read/write
"""

import os
import sys
import sqlite3
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root


SCHEMA_VERSION = 4

SCHEMA_SQL = """
-- Node runtime state
CREATE TABLE IF NOT EXISTS node_state (
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    verify_status TEXT NOT NULL DEFAULT 'pending',
    build_status  TEXT NOT NULL DEFAULT 'impl:missing',
    evidence_json TEXT,
    updated_by    TEXT,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, node_id)
);

-- Node state history (event sourcing auxiliary)
CREATE TABLE IF NOT EXISTS node_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    role          TEXT NOT NULL,
    evidence_json TEXT,
    session_id    TEXT,
    ts            TEXT NOT NULL,
    version       INTEGER NOT NULL
);

-- Session management
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    principal_id  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    scope_json    TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_heartbeat TEXT,
    metadata_json TEXT
);

-- Task registry (v4: upgraded from file-based)
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created',
    type          TEXT NOT NULL DEFAULT 'task',
    prompt        TEXT,
    related_nodes TEXT,
    assigned_to   TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    priority      INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    retry_round   INTEGER NOT NULL DEFAULT 0,
    parent_task_id TEXT
);
-- idx_tasks_status and idx_tasks_assigned created in migration v2

-- Task attempts (retry tracking)
CREATE TABLE IF NOT EXISTS task_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_num   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT
);

-- Idempotency keys
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- Audit index (raw events in JSONL, this is the query index)
CREATE TABLE IF NOT EXISTS audit_index (
    event_id      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    event         TEXT NOT NULL,
    actor         TEXT,
    ok            INTEGER NOT NULL DEFAULT 1,
    ts            TEXT NOT NULL,
    node_ids      TEXT
);

-- Version snapshots (for rollback)
CREATE TABLE IF NOT EXISTS snapshots (
    project_id    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    PRIMARY KEY (project_id, version)
);

-- Event outbox (transactional outbox pattern)
CREATE TABLE IF NOT EXISTS event_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    dead_letter   INTEGER NOT NULL DEFAULT 0,
    trace_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON event_outbox(delivered_at) WHERE delivered_at IS NULL AND dead_letter = 0;
CREATE INDEX IF NOT EXISTS idx_outbox_dead ON event_outbox(dead_letter) WHERE dead_letter = 1;

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_session_principal ON sessions(principal_id, project_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_session_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_audit_project_ts ON audit_index(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ok ON audit_index(ok);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);
CREATE INDEX IF NOT EXISTS idx_node_history_project ON node_history(project_id, node_id, ts);
"""


def _governance_root() -> Path:
    """Root directory for governance data."""
    return Path(tasks_root()) / "state" / "governance"


def _normalize_id(pid: str) -> str:
    """Normalize project ID inline (avoid circular import with project_service)."""
    import re
    s = pid.strip()
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def _resolve_project_dir(project_id: str) -> Path:
    """Resolve the actual project directory, handling normalize mismatch.

    Tries normalized ID first, then raw ID as fallback. This handles the case
    where data was created with the raw ID (e.g., 'amingClaw') before normalize
    was enforced (P0-1), so the directory on disk doesn't match the normalized
    form ('aming-claw').
    """
    root = _governance_root()
    normalized = _normalize_id(project_id) if project_id else project_id
    normalized_dir = root / normalized
    if normalized_dir.exists():
        return normalized_dir
    # Fallback: try raw project_id (handles pre-normalize data)
    raw_dir = root / project_id
    if raw_dir.exists():
        return raw_dir
    # Neither exists — use normalized (will be created)
    return normalized_dir


def _project_db_path(project_id: str) -> Path:
    """Path to the SQLite database for a specific project."""
    project_dir = _resolve_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / "governance.db"


def get_connection(project_id: str) -> sqlite3.Connection:
    """Get a SQLite connection for a project, creating/migrating schema if needed.

    Returns:
        sqlite3.Connection: An open, fully-configured connection to the
        per-project governance database.

    Connection configuration applied on every call:

    WAL mode (PRAGMA journal_mode=WAL):
        Enables Write-Ahead Logging, which allows concurrent readers to proceed
        without being blocked by an active writer.  This is important because
        multiple agents may query the database simultaneously while a write
        transaction is in progress.

    Foreign-key enforcement (PRAGMA foreign_keys=ON):
        SQLite does not enforce foreign-key constraints by default; this PRAGMA
        activates referential-integrity checks for the lifetime of the
        connection (e.g. task_attempts.task_id → tasks.task_id).

    Busy timeout (PRAGMA busy_timeout=5000):
        Instructs SQLite to wait up to 5 000 ms before raising
        ``OperationalError: database is locked`` when another connection holds
        an exclusive lock.  This prevents spurious failures under brief write
        contention.

    Row factory (sqlite3.Row):
        Sets ``conn.row_factory = sqlite3.Row`` so that every fetched row
        supports both index-based and column-name-based access
        (``row["column_name"]`` as well as ``row[0]``).

    Auto-schema migration (_ensure_schema):
        ``_ensure_schema(conn)`` is called on every new connection.  It runs
        the full ``SCHEMA_SQL`` block (``CREATE TABLE IF NOT EXISTS …``) to
        create tables on first use, then checks the stored ``schema_version``
        against ``SCHEMA_VERSION`` (currently {version}) and runs any
        outstanding incremental migration functions up to that target version.
        This means callers never need to manage schema lifecycle manually.
    """.format(version=SCHEMA_VERSION)
    db_path = _project_db_path(project_id)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist, run migrations if needed."""
    conn.executescript(SCHEMA_SQL)

    # Check and set schema version
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        current_version = 0

    if current_version < SCHEMA_VERSION:
        _run_migrations(conn, current_version, SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _run_migrations(conn: sqlite3.Connection, from_version: int, to_version: int):
    """Run incremental migrations between versions.

    Add migration functions as the schema evolves:
        MIGRATIONS = {
            1: _migrate_v0_to_v1,
            2: _migrate_v1_to_v2,
        }
    """
    def _migrate_v1_to_v2(c):
        """Add new columns to tasks table + event_outbox + task_attempts."""
        # Add missing columns to tasks (ALTER TABLE ADD is safe for existing data)
        for col, typedef in [
            ("type", "TEXT NOT NULL DEFAULT 'task'"),
            ("prompt", "TEXT"),
            ("assigned_to", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
            ("error_message", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("metadata_json", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Create task_attempts table if not exists
        c.execute("""CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_num INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT
        )""")

        # Create event_outbox if not exists (may already be from schema)
        c.execute("""CREATE TABLE IF NOT EXISTS event_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            dead_letter INTEGER NOT NULL DEFAULT 0,
            trace_id TEXT
        )""")

        # Create indexes
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to, status)")
        except sqlite3.OperationalError:
            pass

    def _migrate_v2_to_v3(c):
        """Add dual-field status model to tasks."""
        for col, typedef in [
            ("execution_status", "TEXT NOT NULL DEFAULT 'queued'"),
            ("notification_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("notified_at", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        # Sync execution_status from status for existing rows
        try:
            c.execute("UPDATE tasks SET execution_status = status WHERE execution_status = 'queued' AND status != 'queued'")
        except sqlite3.OperationalError:
            pass

    def _migrate_v3_to_v4(c):
        """Add retry_round and parent_task_id fields to tasks for QA→Dev escalation."""
        for col, typedef in [
            ("retry_round", "INTEGER NOT NULL DEFAULT 0"),
            ("parent_task_id", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    MIGRATIONS = {2: _migrate_v1_to_v2, 3: _migrate_v2_to_v3, 4: _migrate_v3_to_v4}
    for version in range(from_version + 1, to_version + 1):
        if version in MIGRATIONS:
            MIGRATIONS[version](conn)


def close_connection(conn: sqlite3.Connection):
    """Close a database connection."""
    if conn:
        conn.close()


class DBContext:
    """Context manager for database connections with automatic commit/rollback."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.conn = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_connection(self.project_id)
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            close_connection(self.conn)
        return False  # Don't suppress exceptions
