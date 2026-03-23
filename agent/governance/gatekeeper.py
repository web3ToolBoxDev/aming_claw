"""Gatekeeper — pre-release checks that must pass before release-gate approves.

Stores check results in SQLite. release-gate reads latest results to decide.

Checks:
  - coverage_check: All changed files have acceptance graph nodes
  - (future: security_scan, dependency_audit, etc.)
"""

import json
import logging
import sqlite3
import time

log = logging.getLogger(__name__)

# Table created via migration or on first use
GATEKEEPER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gatekeeper_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    check_type    TEXT NOT NULL,
    pass          INTEGER NOT NULL,
    result_json   TEXT NOT NULL,
    created_by    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gk_project_type ON gatekeeper_checks(project_id, check_type, created_at);
"""


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create gatekeeper_checks table if not exists."""
    try:
        conn.executescript(GATEKEEPER_TABLE_SQL)
    except Exception:
        pass


def record_check(
    conn: sqlite3.Connection,
    project_id: str,
    check_type: str,
    passed: bool,
    result: dict,
    created_by: str = "",
) -> dict:
    """Record a gatekeeper check result."""
    _ensure_table(conn)
    now = _utc_iso()
    conn.execute(
        """INSERT INTO gatekeeper_checks (project_id, check_type, pass, result_json, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project_id, check_type, 1 if passed else 0,
         json.dumps(result, ensure_ascii=False), created_by, now),
    )
    return {"ok": True, "check_type": check_type, "pass": passed, "recorded_at": now}


def get_latest_check(
    conn: sqlite3.Connection,
    project_id: str,
    check_type: str,
) -> dict | None:
    """Get the most recent check result of a given type."""
    _ensure_table(conn)
    row = conn.execute(
        """SELECT pass, result_json, created_by, created_at
           FROM gatekeeper_checks
           WHERE project_id = ? AND check_type = ?
           ORDER BY created_at DESC LIMIT 1""",
        (project_id, check_type),
    ).fetchone()

    if not row:
        return None

    return {
        "check_type": check_type,
        "pass": bool(row["pass"]),
        "result": json.loads(row["result_json"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def verify_pre_release(
    conn: sqlite3.Connection,
    project_id: str,
    required_checks: list[str] = None,
    max_age_sec: int = 3600,
) -> dict:
    """Verify all required gatekeeper checks pass.

    Args:
        required_checks: List of check types that must pass. Default: ["coverage_check"]
        max_age_sec: Max age of check result in seconds. Stale results → must re-run.

    Returns:
        {pass, checks: {type: {pass, age, detail}}, missing: [...], stale: [...]}
    """
    if required_checks is None:
        required_checks = ["coverage_check"]

    _ensure_table(conn)
    now_ts = time.time()
    checks = {}
    missing = []
    stale = []
    all_pass = True

    for check_type in required_checks:
        latest = get_latest_check(conn, project_id, check_type)

        if latest is None:
            missing.append(check_type)
            all_pass = False
            checks[check_type] = {"pass": False, "reason": "never_run"}
            continue

        # Check age
        try:
            import datetime
            check_dt = datetime.datetime.strptime(
                latest["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            age_sec = (datetime.datetime.now(datetime.timezone.utc) - check_dt).total_seconds()
        except Exception:
            age_sec = 0

        if age_sec > max_age_sec:
            stale.append(check_type)
            all_pass = False
            checks[check_type] = {
                "pass": False,
                "reason": "stale",
                "age_sec": int(age_sec),
                "max_age_sec": max_age_sec,
                "last_run": latest["created_at"],
            }
            continue

        if not latest["pass"]:
            all_pass = False

        checks[check_type] = {
            "pass": latest["pass"],
            "age_sec": int(age_sec),
            "last_run": latest["created_at"],
            "created_by": latest.get("created_by", ""),
        }

    return {
        "pass": all_pass,
        "checks": checks,
        "missing": missing,
        "stale": stale,
    }
