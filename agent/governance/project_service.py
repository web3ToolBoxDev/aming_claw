"""Project service — project initialization, isolation, and routing.

Trust chain:
  1. Human calls POST /api/init {project, password} → gets coordinator token (one-time)
  2. Same project re-init → 403 (unless password provided for token reset)
  3. Human gives coordinator token to Coordinator agent
  4. Coordinator uses its token to assign roles to other agents via /api/role/assign
"""

import json
import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root
from .db import get_connection, _governance_root
from .graph import AcceptanceGraph
from . import state_service
from . import role_service
from . import audit_service
from .errors import ValidationError, AuthError, PermissionDeniedError


def _projects_file() -> Path:
    p = _governance_root() / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _load_projects() -> dict:
    path = _projects_file()
    if path.exists():
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "projects": {}}


def _save_projects(data: dict):
    path = _projects_file()
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# Project ID normalization
# ============================================================

def _normalize_project_id(raw: str) -> str:
    """Normalize project ID to lowercase kebab-case.
    toolBoxClient → toolbox-client
    My App → my-app
    aming_claw → aming-claw
    """
    import re
    s = raw.strip()
    # camelCase → kebab-case: insert hyphen before uppercase
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    # spaces and underscores → hyphens
    s = re.sub(r'[\s_]+', '-', s)
    # collapse multiple hyphens
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def _check_id_conflict(normalized: str, projects: dict) -> str | None:
    """Check if a normalized ID conflicts with existing projects.
    Returns the conflicting project_id or None.
    """
    for existing_id in projects.get("projects", {}):
        if _normalize_project_id(existing_id) == normalized and existing_id != normalized:
            return existing_id
    return None


# ============================================================
# /api/init — one-time project initialization
# ============================================================

def init_project(project_id: str, password: str, project_name: str = "", workspace_path: str = "") -> dict:
    """Initialize a project and return the coordinator token.

    Rules:
      - project_id is normalized to lowercase kebab-case
      - First call: creates project + coordinator session → returns token
      - Repeat call without password: 403 (project already initialized)
      - Repeat call with correct password: resets coordinator token → returns new token
      - Wrong password: 403

    Returns: {project, coordinator: {session_id, token}}
    """
    if not project_id:
        raise ValidationError("project_id is required")

    # Normalize ID
    original_id = project_id
    project_id = _normalize_project_id(project_id)

    if not project_id or not project_id.replace("-", "").isalnum():
        raise ValidationError(f"Invalid project_id: {original_id!r} (normalized: {project_id!r})")

    # Check for conflicting IDs
    projects = _load_projects()
    conflict = _check_id_conflict(project_id, projects)
    if conflict:
        raise ValidationError(
            f"Project ID conflict: {original_id!r} normalizes to {project_id!r} "
            f"which conflicts with existing project {conflict!r}"
        )
    if not password or len(password) < 6:
        raise ValidationError("Password must be at least 6 characters")

    password_hash = _hash_password(password)

    existing = projects["projects"].get(project_id)

    if existing and existing.get("initialized"):
        # Project already initialized — need password to reset
        if existing.get("password_hash") != password_hash:
            raise AuthError(
                "Project already initialized. Provide correct password to reset coordinator token.",
                "project_already_initialized",
            )
        # Password correct → reset coordinator token
        return _reset_coordinator_token(project_id, projects, existing)

    # First-time initialization
    project_dir = _governance_root() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "project_id": project_id,
        "name": project_name or project_id,
        "workspace_path": workspace_path,
        "password_hash": password_hash,
        "created_at": _utc_iso(),
        "initialized": True,
        "status": "active",
        "node_count": 0,
    }
    projects["projects"][project_id] = entry
    _save_projects(projects)

    # Create coordinator session
    conn = get_connection(project_id)
    try:
        coord_result = role_service.register(
            conn, "coordinator", project_id, "coordinator",
        )
        conn.commit()
    finally:
        conn.close()

    result = {
        "project": {
            "project_id": project_id,
            "name": entry["name"],
            "status": "active",
            "created_at": entry["created_at"],
        },
        "coordinator": {
            "session_id": coord_result["session_id"],
            "token": coord_result["token"],
        },
        "message": "Project initialized. Give this token to your Coordinator agent. "
                   "This token will not be shown again unless you reset with password.",
    }
    if original_id != project_id:
        result["normalized_from"] = original_id
        result["message"] += f" Note: project_id normalized from '{original_id}' to '{project_id}'."
    return result


def _reset_coordinator_token(project_id: str, projects: dict, entry: dict) -> dict:
    """Reset coordinator token for an existing project."""
    conn = get_connection(project_id)
    try:
        # Re-register coordinator (will refresh existing session)
        coord_result = role_service.register(
            conn, "coordinator", project_id, "coordinator",
        )
        conn.commit()

        audit_service.record(
            conn, project_id, "coordinator_token_reset",
            actor="human",
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "project": {
            "project_id": project_id,
            "name": entry.get("name", project_id),
            "status": entry.get("status", "active"),
        },
        "coordinator": {
            "session_id": coord_result["session_id"],
            "token": coord_result["token"],
        },
        "message": "Coordinator token has been reset.",
    }


# ============================================================
# Role assignment (coordinator only)
# ============================================================

def assign_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    principal_id: str,
    role: str,
    scope: list = None,
) -> dict:
    """Coordinator assigns a role to another agent.

    Only coordinators can call this. Returns the new agent's token.
    """
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "assign_role",
            {"detail": "Only coordinator can assign roles"},
        )
    if role == "coordinator":
        raise PermissionDeniedError(
            "coordinator", "assign_role",
            {"detail": "Cannot assign coordinator role. Use /api/init to get coordinator token."},
        )

    result = role_service.register(
        conn, principal_id, project_id, role, scope=scope,
    )

    audit_service.record(
        conn, project_id, "role_assigned",
        actor=coordinator_session.get("principal_id", ""),
        assigned_principal=principal_id,
        assigned_role=role,
        session_id=coordinator_session.get("session_id", ""),
    )

    return {
        "principal_id": principal_id,
        "role": role,
        "session_id": result["session_id"],
        "token": result["token"],
        "scope": scope or [],
        "expires_at": result.get("expires_at", ""),
        "message": f"Give this token to {principal_id}. It grants {role} access to {project_id}.",
    }


def revoke_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    session_id: str,
) -> dict:
    """Coordinator revokes an agent's session."""
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "revoke_role",
        )

    result = role_service.deregister(conn, session_id)

    audit_service.record(
        conn, project_id, "role_revoked",
        actor=coordinator_session.get("principal_id", ""),
        revoked_session=session_id,
    )

    return result


# ============================================================
# Project query helpers
# ============================================================

def get_project(project_id: str) -> dict | None:
    projects = _load_projects()
    return projects["projects"].get(project_id)


def list_projects() -> list[dict]:
    projects = _load_projects()
    result = []
    for p in projects["projects"].values():
        # Never expose password_hash
        safe = {k: v for k, v in p.items() if k != "password_hash"}
        result.append(safe)
    return result


def project_exists(project_id: str) -> bool:
    return get_project(project_id) is not None


# ============================================================
# Graph import
# ============================================================

def import_graph(project_id: str, md_path: str) -> dict:
    """Import acceptance graph from markdown for a project."""
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = AcceptanceGraph()
    result = graph.import_from_markdown(md_path)

    graph_path = _governance_root() / project_id / "graph.json"
    graph.save(graph_path)

    conn = get_connection(project_id)
    try:
        count = state_service.init_node_states(conn, project_id, graph)
        conn.commit()
    finally:
        conn.close()

    projects = _load_projects()
    if project_id in projects["projects"]:
        projects["projects"][project_id]["node_count"] = graph.node_count()
        _save_projects(projects)

    result["node_states_initialized"] = count
    return result


def load_project_graph(project_id: str) -> AcceptanceGraph:
    project_id = _normalize_project_id(project_id) if project_id else project_id
    graph_path = _governance_root() / project_id / "graph.json"
    if not graph_path.exists():
        raise ValidationError(f"No graph found for project {project_id!r}. Run import-graph first.")
    graph = AcceptanceGraph()
    graph.load(graph_path)
    return graph
