"""State service — runtime state management (Layer 2).

Manages node verify_status transitions with permission/evidence/gate checks.
All mutations go through SQLite transactions with audit logging.
"""

import json
import sqlite3
from datetime import datetime, timezone

from .enums import VerifyStatus, Role
from .models import Evidence
from .errors import (
    NodeNotFoundError, ConflictError, ValidationError,
    ReleaseBlockedError,
)
from .permissions import check_transition, check_nodes_scope
from .evidence import validate_evidence
from .gate_policy import check_gates_or_raise
from . import audit_service
from . import event_bus


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_node_states(conn: sqlite3.Connection, project_id: str, graph, sync_status: bool = True) -> int:
    """Initialize node_state rows from graph.

    Reads parsed_verify_status and parsed_build_status from graph node data
    (set during markdown import). Falls back to 'pending'/'impl:missing'.

    If sync_status=True (default), also updates existing nodes whose markdown
    status differs from the DB (e.g., markdown says 'pass' but DB says 'pending').
    """
    count = 0
    now = _utc_iso()
    for node_id in graph.list_nodes():
        node_data = graph.get_node(node_id)
        verify = node_data.get("parsed_verify_status", "pending")
        build = node_data.get("parsed_build_status",
                              node_data.get("build_status", "impl:missing"))

        existing = conn.execute(
            "SELECT verify_status, build_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()

        if not existing:
            conn.execute(
                """INSERT INTO node_state
                   (project_id, node_id, verify_status, build_status, updated_at, version)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (project_id, node_id, verify, build, now),
            )
            count += 1
        elif sync_status and verify != "pending":
            # Sync: if markdown declares a non-pending status and DB is still pending, update
            db_status = existing["verify_status"]
            if db_status == "pending" and verify != "pending":
                # Map markdown status to enum
                status_map = {
                    "pass": "qa_pass",
                    "T2-pass": "t2_pass",
                    "fail": "failed",
                    "skipped": "skipped",
                }
                mapped = status_map.get(verify, verify)
                conn.execute(
                    """UPDATE node_state SET verify_status = ?, build_status = ?,
                       updated_at = ?, updated_by = 'import-sync'
                       WHERE project_id = ? AND node_id = ?""",
                    (mapped, build, now, project_id, node_id),
                )
                count += 1
    return count


def set_baseline(
    conn: sqlite3.Connection,
    project_id: str,
    node_statuses: dict[str, str],
    session: dict,
    reason: str = "",
) -> dict:
    """Coordinator batch-sets historical node states, bypassing permission/evidence checks.

    Used for one-time import of verified state from legacy acceptance graphs.
    Only coordinator can call this. All changes are audited as 'baseline_import'.

    Args:
        node_statuses: {"L0.1": "qa_pass", "L0.2": "t2_pass", "L3.7": "pending"}
        reason: Human-readable reason for the baseline import.

    Returns: {updated: int, skipped: int, details: [...]}
    """
    if session.get("role") != "coordinator":
        from .errors import PermissionDeniedError
        raise PermissionDeniedError(session.get("role", ""), "set_baseline",
                                    {"detail": "Only coordinator can set baseline"})

    now = _utc_iso()
    updated = 0
    skipped = 0
    details = []

    for node_id, target_status_str in node_statuses.items():
        target = VerifyStatus.from_str(target_status_str)

        current = get_node_status(conn, project_id, node_id)
        if current is None:
            details.append({"node": node_id, "action": "skipped", "reason": "not in graph"})
            skipped += 1
            continue

        current_status = current["verify_status"]
        if current_status == target.value:
            details.append({"node": node_id, "action": "skipped", "reason": "already at target"})
            skipped += 1
            continue

        new_version = current["version"] + 1
        conn.execute(
            """UPDATE node_state
               SET verify_status = ?, updated_by = ?, updated_at = ?, version = ?
               WHERE project_id = ? AND node_id = ?""",
            (target.value, session.get("session_id", ""), now, new_version,
             project_id, node_id),
        )

        # History
        conn.execute(
            """INSERT INTO node_history
               (project_id, node_id, from_status, to_status, role, evidence_json,
                session_id, ts, version)
               VALUES (?, ?, ?, ?, 'coordinator', '{"type":"baseline_import"}', ?, ?, ?)""",
            (project_id, node_id, current_status, target.value,
             session.get("session_id", ""), now, new_version),
        )

        details.append({"node": node_id, "action": "updated", "from": current_status, "to": target.value})
        updated += 1

    # Audit
    audit_service.record(
        conn, project_id, "baseline_import",
        actor=session.get("principal_id", ""),
        nodes_updated=updated, nodes_skipped=skipped,
        reason=reason,
    )

    return {"updated": updated, "skipped": skipped, "details": details}


def get_node_status(conn: sqlite3.Connection, project_id: str, node_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, node_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result.get("evidence_json"):
        try:
            result["evidence"] = json.loads(result["evidence_json"])
        except (json.JSONDecodeError, TypeError):
            result["evidence"] = None
    return result


def _get_status_fn(conn: sqlite3.Connection, project_id: str):
    """Return a function that gets VerifyStatus for a node_id."""
    def _fn(node_id: str) -> VerifyStatus:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            return VerifyStatus.PENDING
        return VerifyStatus.from_str(row["verify_status"])
    return _fn


def verify_update(
    conn: sqlite3.Connection,
    project_id: str,
    graph,
    node_ids: list[str],
    target_status: str,
    session: dict,
    evidence_dict: dict = None,
) -> dict:
    """Core verify-update operation.

    Flow:
    1. Permission check (role from session, not body)
    2. Evidence validation
    3. Gate check
    4. State mutation + history + audit
    5. Event publish

    Returns dict with updated_nodes and affected_downstream.
    """
    role = Role.from_str(session["role"])
    scope = session.get("scope", [])
    target = VerifyStatus.from_str(target_status)
    evidence = Evidence.from_dict(evidence_dict or {})
    evidence.producer = session.get("session_id", "")

    # Scope check
    if scope:
        check_nodes_scope(node_ids, scope)

    updated = []
    now = _utc_iso()

    for node_id in node_ids:
        # Verify node exists in graph
        if not graph.has_node(node_id):
            raise NodeNotFoundError(node_id)

        # Get current state
        current = get_node_status(conn, project_id, node_id)
        if current is None:
            raise NodeNotFoundError(node_id)

        from_status = VerifyStatus.from_str(current["verify_status"])
        if from_status == target:
            continue  # No-op, already at target

        # 1. Permission check
        check_transition(from_status, target, role)

        # 2. Evidence validation
        validate_evidence(from_status, target, evidence)

        # 3. Gate check (only for forward transitions)
        gates = graph.get_gates(node_id)
        if gates and target in (VerifyStatus.T2_PASS, VerifyStatus.QA_PASS):
            check_gates_or_raise(node_id, gates, _get_status_fn(conn, project_id))

        # 4. Mutate state
        new_version = current["version"] + 1
        conn.execute(
            """UPDATE node_state
               SET verify_status = ?, evidence_json = ?, updated_by = ?,
                   updated_at = ?, version = ?
               WHERE project_id = ? AND node_id = ? AND version = ?""",
            (
                target.value, evidence.to_json(), session.get("session_id", ""),
                now, new_version,
                project_id, node_id, current["version"],
            ),
        )

        # Check optimistic lock
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise ConflictError(details={
                "node_id": node_id,
                "expected_version": current["version"],
            })

        # 5. Write history
        conn.execute(
            """INSERT INTO node_history
               (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, node_id, from_status.value, target.value,
                role.value, evidence.to_json(), session.get("session_id", ""),
                now, new_version,
            ),
        )

        # 6. Audit
        audit_service.record(
            conn, project_id, "verify_update",
            actor=session.get("principal_id", ""),
            node_ids=[node_id],
            from_status=from_status.value,
            to_status=target.value,
            session_id=session.get("session_id", ""),
        )

        updated.append(node_id)

        # 7. Event (in-process + outbox for reliable delivery)
        event_payload = {
            "project_id": project_id,
            "node_id": node_id,
            "from": from_status.value,
            "to": target.value,
            "role": role.value,
        }
        event_bus.publish("node.status_changed", event_payload)
        try:
            from .outbox import write_outbox
            write_outbox(conn, "node.status_changed", event_payload, project_id)
        except Exception:
            pass  # Outbox table may not exist on older DBs

    # Compute downstream impact
    downstream = set()
    for nid in updated:
        downstream |= graph.descendants(nid)

    return {
        "updated_nodes": updated,
        "affected_downstream": sorted(downstream - set(updated)),
        "version": new_version if updated else None,
    }


# Named release profiles
RELEASE_PROFILES = {
    "full": {"scope": ["*"], "min_status": "qa_pass", "description": "All nodes must be qa_pass"},
    "hotfix": {"scope": ["L0.*", "L1.*"], "min_status": "t2_pass", "description": "Core layers T2-pass only"},
    "foundation": {"scope": ["L0.*"], "min_status": "qa_pass", "description": "Foundation layer only"},
    "governance": {"scope": ["L4.*", "L5.*", "L6.*"], "min_status": "t2_pass", "description": "Governance stack"},
}


def release_gate(
    conn: sqlite3.Connection,
    project_id: str,
    graph,
    scope: list[str] = None,
    profile: str = None,
    min_status: str = "qa_pass",
) -> dict:
    """Release gate check with profile support.

    Args:
        scope: Node patterns to check (e.g., ["L3.*", "L4.1"]). Default: all nodes.
        profile: Named profile (full, hotfix, foundation, governance). Overrides scope/min_status.
        min_status: Minimum required status. Default: qa_pass.
    """
    import fnmatch

    # Apply named profile
    if profile and profile in RELEASE_PROFILES:
        p = RELEASE_PROFILES[profile]
        scope = p.get("scope", scope)
        min_status = p.get("min_status", min_status)
    elif profile and profile not in RELEASE_PROFILES:
        from .errors import ValidationError
        raise ValidationError(
            f'Unknown profile: {profile}. Available: {list(RELEASE_PROFILES.keys())}'
        )

    # Determine acceptable statuses based on min_status
    status_order = ["pending", "testing", "t2_pass", "qa_pass"]
    min_idx = status_order.index(min_status) if min_status in status_order else 3
    acceptable = set(status_order[min_idx:]) | {"waived"}

    all_nodes = graph.list_nodes()
    check_nodes = all_nodes

    if scope:
        check_nodes = [n for n in all_nodes if any(fnmatch.fnmatch(n, p) for p in scope)]

    blockers = []
    summary = {"qa_pass": 0, "t2_pass": 0, "pending": 0, "testing": 0, "failed": 0, "waived": 0, "other": 0}

    for node_id in check_nodes:
        state = get_node_status(conn, project_id, node_id)
        status = state["verify_status"] if state else "pending"

        if status in summary:
            summary[status] += 1
        else:
            summary["other"] += 1

        if status not in acceptable:
            blockers.append({
                "node_id": node_id,
                "status": status,
                "required": min_status,
            })

    result = {
        "release": len(blockers) == 0,
        "profile": profile,
        "min_status": min_status,
        "checked_nodes": len(check_nodes),
        "total_nodes": len(all_nodes),
        "summary": summary,
        "available_profiles": list(RELEASE_PROFILES.keys()),
    }

    if blockers:
        raise ReleaseBlockedError(blockers, summary)

    return result


def get_summary(conn: sqlite3.Connection, project_id: str) -> dict:
    """Get summary statistics."""
    rows = conn.execute(
        "SELECT verify_status, COUNT(*) as cnt FROM node_state WHERE project_id = ? GROUP BY verify_status",
        (project_id,),
    ).fetchall()

    by_status = {row["verify_status"]: row["cnt"] for row in rows}
    total = sum(by_status.values())

    return {
        "project_id": project_id,
        "total_nodes": total,
        "by_status": by_status,
    }


def create_snapshot(conn: sqlite3.Connection, project_id: str, created_by: str = "") -> int:
    """Create a snapshot of current node_state. Returns version number."""
    rows = conn.execute(
        "SELECT * FROM node_state WHERE project_id = ?", (project_id,),
    ).fetchall()

    snapshot = [dict(r) for r in rows]

    # Get next version
    row = conn.execute(
        "SELECT MAX(version) as max_v FROM snapshots WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    next_version = (row["max_v"] or 0) + 1

    conn.execute(
        "INSERT INTO snapshots (project_id, version, snapshot_json, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
        (project_id, next_version, json.dumps(snapshot, ensure_ascii=False), _utc_iso(), created_by),
    )

    return next_version


def rollback(conn: sqlite3.Connection, project_id: str, target_version: int, session: dict) -> dict:
    """Rollback to a specific snapshot version."""
    row = conn.execute(
        "SELECT snapshot_json FROM snapshots WHERE project_id = ? AND version = ?",
        (project_id, target_version),
    ).fetchone()

    if row is None:
        raise ValidationError(f"Snapshot version {target_version} not found")

    snapshot = json.loads(row["snapshot_json"])

    # Get current version for audit
    current_row = conn.execute(
        "SELECT MAX(version) as max_v FROM snapshots WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    current_version = current_row["max_v"] or 0

    # Restore state
    changes = []
    for node_state in snapshot:
        old = get_node_status(conn, project_id, node_state["node_id"])
        if old and old["verify_status"] != node_state["verify_status"]:
            changes.append({
                "node_id": node_state["node_id"],
                "from": old["verify_status"],
                "to": node_state["verify_status"],
            })

        conn.execute(
            """INSERT OR REPLACE INTO node_state
               (project_id, node_id, verify_status, build_status, evidence_json,
                updated_by, updated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, node_state["node_id"],
                node_state["verify_status"], node_state.get("build_status", "impl:missing"),
                node_state.get("evidence_json"), session.get("session_id", ""),
                _utc_iso(), node_state.get("version", 1),
            ),
        )

    audit_service.record(
        conn, project_id, "rollback",
        actor=session.get("principal_id", ""),
        from_version=current_version, to_version=target_version,
        nodes_affected=len(changes),
    )

    rollback_payload = {
        "project_id": project_id,
        "from_version": current_version,
        "to_version": target_version,
    }
    event_bus.publish("rollback.executed", rollback_payload)
    try:
        from .outbox import write_outbox
        write_outbox(conn, "rollback.executed", rollback_payload, project_id)
    except Exception:
        pass

    return {
        "rolled_back_from": current_version,
        "rolled_back_to": target_version,
        "nodes_affected": len(changes),
        "changes": changes,
    }
