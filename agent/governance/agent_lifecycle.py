"""Agent Lifecycle API — register, heartbeat, deregister, orphan detection.

Each agent gets a lease (TTL-based). Without heartbeat, lease expires → orphan.
Coordinator can query orphans and clean up stale routes/sessions.
"""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone, timedelta

from .redis_client import get_redis

log = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SEC = 300  # 5 minutes
HEARTBEAT_INTERVAL_SEC = 120  # Expected: every 2 minutes


def register_agent(
    conn: sqlite3.Connection,
    project_id: str,
    session: dict,
    expected_duration_sec: int = 0,
) -> dict:
    """Register an agent and issue a lease.

    Args:
        conn: SQLite connection
        project_id: Project scope
        session: Authenticated session dict
        expected_duration_sec: How long the agent expects to run (0 = indefinite)

    Returns: {lease_id, expires_at, heartbeat_interval_sec}
    """
    lease_id = f"lease-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    lease_ttl = max(DEFAULT_LEASE_TTL_SEC, expected_duration_sec)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=lease_ttl)
    expires_str = expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Store lease in Redis (ephemeral, TTL-based)
    rc = get_redis()
    token_hash = session.get("token_hash", "")
    if not token_hash:
        from .role_service import _hash_token
        # Try to derive from session
        token_hash = session.get("session_id", "")[:16]

    lease_data = {
        "lease_id": lease_id,
        "session_id": session.get("session_id", ""),
        "principal_id": session.get("principal_id", ""),
        "project_id": project_id,
        "role": session.get("role", ""),
        "status": "active",
        "registered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_heartbeat": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_str,
    }

    rc.set_json(f"lease:{lease_id}", lease_data, lease_ttl)
    # Also index by session for quick lookup
    rc.set(f"agent:session:{session.get('session_id', '')}", lease_id, lease_ttl)

    log.info("Agent registered: %s (role: %s, lease: %s, ttl: %ds)",
             session.get("principal_id"), session.get("role"), lease_id, lease_ttl)

    return {
        "lease_id": lease_id,
        "expires_at": expires_str,
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
        "lease_ttl_sec": lease_ttl,
    }


def heartbeat(lease_id: str, status: str = "idle") -> dict:
    """Renew an agent's lease.

    Args:
        lease_id: The lease to renew
        status: Agent status (idle, busy, processing)

    Returns: {ok, lease_renewed_until}
    """
    rc = get_redis()
    lease_data = rc.get_json(f"lease:{lease_id}")

    if not lease_data:
        from .errors import ValidationError
        raise ValidationError(f"Lease {lease_id} not found or expired")

    now = datetime.now(timezone.utc)
    new_expires = now + timedelta(seconds=DEFAULT_LEASE_TTL_SEC)
    expires_str = new_expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    lease_data["last_heartbeat"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lease_data["status"] = status
    lease_data["expires_at"] = expires_str

    rc.set_json(f"lease:{lease_id}", lease_data, DEFAULT_LEASE_TTL_SEC)

    # Also refresh session index
    session_id = lease_data.get("session_id", "")
    if session_id:
        rc.set(f"agent:session:{session_id}", lease_id, DEFAULT_LEASE_TTL_SEC)

    return {
        "ok": True,
        "lease_renewed_until": expires_str,
        "status": status,
    }


def deregister(lease_id: str) -> dict:
    """Deregister an agent, releasing its lease.

    Returns: {ok, lease_id}
    """
    rc = get_redis()
    lease_data = rc.get_json(f"lease:{lease_id}")

    if lease_data:
        session_id = lease_data.get("session_id", "")
        rc.delete(f"agent:session:{session_id}")

    rc.delete(f"lease:{lease_id}")

    log.info("Agent deregistered: lease %s", lease_id)
    return {"ok": True, "lease_id": lease_id}


def list_active_agents(project_id: str = None) -> list[dict]:
    """List all agents with active leases.

    Args:
        project_id: Optional filter by project
    """
    rc = get_redis()
    if not rc or not rc.available:
        return []

    agents = []
    # Scan for lease keys
    for key in rc._safe(lambda: list(rc._client.scan_iter("lease:lease-*")), []):
        data = rc.get_json(key)
        if data:
            if project_id and data.get("project_id") != project_id:
                continue
            agents.append(data)

    return agents


def find_orphans(project_id: str = None) -> list[dict]:
    """Find agents whose leases have expired but weren't properly deregistered.

    Since Redis auto-expires keys, truly expired leases are gone.
    This checks for sessions that claim to be active but have no lease.
    """
    from .db import get_connection

    orphans = []
    rc = get_redis()

    if project_id:
        projects = [project_id]
    else:
        from . import project_service
        projects = [p["project_id"] for p in project_service.list_projects()]

    for pid in projects:
        try:
            conn = get_connection(pid)
            rows = conn.execute(
                """SELECT session_id, principal_id, role, last_heartbeat
                   FROM sessions WHERE project_id = ? AND status = 'active'""",
                (pid,),
            ).fetchall()
            conn.close()

            for row in rows:
                session_id = row["session_id"]
                lease_id = rc.get(f"agent:session:{session_id}") if rc.available else None
                if lease_id:
                    # Has active lease, not orphan
                    lease = rc.get_json(f"lease:{lease_id}")
                    if lease:
                        continue

                # No lease found — potential orphan
                orphans.append({
                    "session_id": session_id,
                    "principal_id": row["principal_id"],
                    "project_id": pid,
                    "role": row["role"],
                    "last_heartbeat": row["last_heartbeat"],
                    "reason": "no_active_lease",
                })
        except Exception as e:
            log.warning("Error checking orphans for %s: %s", pid, e)

    return orphans


def cleanup_orphans(project_id: str) -> dict:
    """Clean up orphaned agents: expire sessions, invalidate routes."""
    orphans = find_orphans(project_id)
    cleaned = 0

    from .db import get_connection
    conn = get_connection(project_id)

    try:
        for orphan in orphans:
            if orphan["project_id"] != project_id:
                continue
            session_id = orphan["session_id"]
            conn.execute(
                "UPDATE sessions SET status = 'expired' WHERE session_id = ? AND status = 'active'",
                (session_id,),
            )
            cleaned += 1
            log.info("Cleaned orphan: %s (%s)", session_id, orphan["principal_id"])

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"cleaned": cleaned, "orphans_found": len(orphans)}
