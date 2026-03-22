"""Memory service — structured development knowledge base.

Enhanced with kind, applies_when, supersedes for lifecycle tracking.
Stored in SQLite alongside other runtime state.
"""

import json
import sqlite3
from datetime import datetime, timezone

from .models import MemoryEntry
from . import audit_service


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# We store memories in a JSON file per project (matches original design)
# If needed, can migrate to SQLite table later

def _memories_path(project_id: str) -> str:
    from .db import _governance_root
    p = _governance_root() / project_id / "memories.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _load_memories(project_id: str) -> dict:
    import os
    path = _memories_path(project_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "entries": []}


def _save_memories(project_id: str, data: dict):
    path = _memories_path(project_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_memory(
    conn: sqlite3.Connection,
    project_id: str,
    entry: MemoryEntry,
    session: dict = None,
) -> dict:
    """Write a memory entry."""
    data = _load_memories(project_id)

    # Handle supersedes
    if entry.supersedes:
        for existing in data["entries"]:
            if existing["id"] == entry.supersedes:
                existing["is_active"] = False

    entry_dict = entry.to_dict()
    data["entries"].append(entry_dict)
    data["version"] = data.get("version", 0) + 1
    _save_memories(project_id, data)

    # Audit
    audit_service.record(
        conn, project_id, "memory.written",
        actor=session.get("principal_id", "") if session else "",
        module_id=entry.module_id, kind=entry.kind,
    )

    # Forward to dbservice (async, best-effort)
    _forward_to_dbservice(project_id, entry_dict)

    return entry_dict


def _forward_to_dbservice(project_id: str, entry: dict) -> None:
    """Forward memory write to dbservice for semantic search. Best-effort."""
    import os
    dbservice_url = os.environ.get("DBSERVICE_URL", "")
    if not dbservice_url:
        return
    try:
        import requests
        ref_id = entry.get("id", f"{entry.get('module_id', '')}:{entry.get('kind', '')}:{entry.get('created_at', '')}")
        requests.post(
            f"{dbservice_url}/knowledge/upsert",
            json={
                "refId": ref_id,
                "type": entry.get("kind", "knowledge"),
                "title": f"{entry.get('module_id', '')}: {entry.get('kind', '')}",
                "body": entry.get("content", ""),
                "tags": [entry.get("module_id", ""), entry.get("kind", "")] + entry.get("related_nodes", []),
                "scope": project_id,
                "status": "active",
            },
            timeout=3,
        )
    except Exception:
        pass  # Best-effort


def query_by_module(project_id: str, module_id: str, active_only: bool = True) -> list[dict]:
    """Query memories by module."""
    data = _load_memories(project_id)
    results = []
    for entry in data["entries"]:
        if entry.get("module_id") == module_id:
            if active_only and not entry.get("is_active", True):
                continue
            results.append(entry)
    return results


def query_by_kind(project_id: str, kind: str, module_id: str = None) -> list[dict]:
    """Query memories by kind (pitfall, pattern, etc.)."""
    data = _load_memories(project_id)
    results = []
    for entry in data["entries"]:
        if entry.get("kind") == kind:
            if module_id and entry.get("module_id") != module_id:
                continue
            if not entry.get("is_active", True):
                continue
            results.append(entry)
    return results


def query_by_related_node(project_id: str, node_id: str) -> list[dict]:
    """Query memories related to a specific acceptance graph node."""
    data = _load_memories(project_id)
    results = []
    for entry in data["entries"]:
        if node_id in entry.get("related_nodes", []):
            if not entry.get("is_active", True):
                continue
            results.append(entry)
    return results


def query_all(project_id: str, active_only: bool = True) -> list[dict]:
    """Get all memories for a project."""
    data = _load_memories(project_id)
    if active_only:
        return [e for e in data["entries"] if e.get("is_active", True)]
    return data["entries"]
