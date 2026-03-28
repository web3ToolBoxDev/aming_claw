"""Memory service — structured development knowledge base.

Phase 2: Migrated from JSON file storage to SQLite + FTS5 via pluggable backend.
The backend is selected via MEMORY_BACKEND env var (default: "local").

Public API (unchanged for backward compatibility):
  - write_memory(conn, project_id, entry, session) -> dict
  - query_by_module(project_id, module_id) -> list
  - query_by_kind(project_id, kind, module_id) -> list
  - query_by_related_node(project_id, node_id) -> list
  - query_all(project_id) -> list

New API:
  - search_memories(conn, project_id, query, top_k) -> list
  - get_latest_by_ref(conn, project_id, ref_id) -> dict | None
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from .models import MemoryEntry
from . import audit_service
from .memory_backend import get_backend
from .db import get_connection

log = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_memory(
    conn: sqlite3.Connection,
    project_id: str,
    entry: MemoryEntry,
    session: dict = None,
) -> dict:
    """Write a memory entry via the configured backend."""
    backend = get_backend()

    # Convert MemoryEntry to dict for backend
    entry_dict = entry.to_dict()
    # Map old field names to new backend expectations
    backend_entry = {
        "ref_id": entry_dict.get("id", ""),
        "kind": entry_dict.get("kind", "knowledge"),
        "module": entry_dict.get("module_id", ""),
        "content": entry_dict.get("content", ""),
        "summary": entry_dict.get("applies_when", ""),
        "tags": ",".join(entry_dict.get("related_nodes", [])),
        "structured": {
            "created_by": entry_dict.get("created_by", ""),
            "related_nodes": entry_dict.get("related_nodes", []),
            "supersedes": entry_dict.get("supersedes"),
        },
    }

    # If supersedes is set, use it as ref_id to trigger version chain
    if entry.supersedes:
        # Look up the ref_id of the superseded memory
        old = backend.get_latest(conn, project_id, entry.supersedes)
        if old and old.get("ref_id"):
            backend_entry["ref_id"] = old["ref_id"]

    result = backend.write(conn, project_id, backend_entry)

    # Audit
    audit_service.record(
        conn, project_id, "memory.written",
        actor=session.get("principal_id", "") if session else "",
        module_id=entry.module_id, kind=entry.kind,
    )

    # Forward to dbservice (best-effort, for existing docker integration)
    _forward_to_dbservice(project_id, entry_dict)

    return result


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


# ------------------------------------------------------------------
# Query functions — now backed by SQLite instead of JSON
# ------------------------------------------------------------------

def query_by_module(project_id: str, module_id: str, active_only: bool = True) -> list[dict]:
    """Query memories by module."""
    backend = get_backend()
    conn = get_connection(project_id)
    try:
        return backend.query(conn, project_id, module=module_id, active_only=active_only)
    finally:
        conn.close()


def query_by_kind(project_id: str, kind: str, module_id: str = None) -> list[dict]:
    """Query memories by kind (pitfall, pattern, etc.)."""
    backend = get_backend()
    conn = get_connection(project_id)
    try:
        results = backend.query(conn, project_id, kind=kind, active_only=True)
        if module_id:
            results = [r for r in results if r.get("module_id") == module_id]
        return results
    finally:
        conn.close()


def query_by_related_node(project_id: str, node_id: str) -> list[dict]:
    """Query memories related to a specific acceptance graph node."""
    backend = get_backend()
    conn = get_connection(project_id)
    try:
        # Search for node_id in tags or metadata
        all_active = backend.query(conn, project_id, active_only=True)
        results = []
        for entry in all_active:
            tags = entry.get("tags", "")
            metadata = entry.get("metadata", {})
            related = metadata.get("related_nodes", []) if isinstance(metadata, dict) else []
            if node_id in tags or node_id in related:
                results.append(entry)
        return results
    finally:
        conn.close()


def query_all(project_id: str, active_only: bool = True) -> list[dict]:
    """Get all memories for a project."""
    backend = get_backend()
    conn = get_connection(project_id)
    try:
        return backend.query(conn, project_id, active_only=active_only)
    finally:
        conn.close()


# ------------------------------------------------------------------
# New Phase 2 API
# ------------------------------------------------------------------

def search_memories(conn: sqlite3.Connection, project_id: str, query: str, top_k: int = 5) -> list[dict]:
    """Full-text search across memories."""
    backend = get_backend()
    return backend.search(conn, project_id, query, top_k)


def get_latest_by_ref(conn: sqlite3.Connection, project_id: str, ref_id: str) -> dict | None:
    """Get latest active version for a ref_id."""
    backend = get_backend()
    return backend.get_latest(conn, project_id, ref_id)
