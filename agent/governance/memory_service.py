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
from typing import Dict

from .models import MemoryEntry
from . import audit_service
from .memory_backend import get_backend
from .db import get_connection

log = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_conflict_policy(conn: sqlite3.Connection, project_id: str, kind: str) -> str:
    """Look up conflict_policy for a kind from domain_packs table. Returns 'replace' if not found."""
    try:
        row = conn.execute(
            "SELECT conflict_policy FROM domain_packs WHERE project_id=? AND type_name=?",
            (project_id, kind),
        ).fetchone()
        if row:
            return row["conflict_policy"]
    except Exception:
        pass
    return "replace"


def write_memory(
    conn: sqlite3.Connection,
    project_id: str,
    entry: MemoryEntry,
    session: dict = None,
) -> dict:
    """Write a memory entry via the configured backend, applying domain pack conflict policy."""
    backend = get_backend()

    # Convert MemoryEntry to dict for backend
    entry_dict = entry.to_dict()
    kind = entry_dict.get("kind", "knowledge")
    module_id = entry_dict.get("module_id", "")
    content = entry_dict.get("content", "")

    # Look up conflict policy from domain pack
    conflict_policy = _get_conflict_policy(conn, project_id, kind)

    # --- append_set: skip if identical content for same module+kind already active ---
    if conflict_policy == "append_set":
        existing = conn.execute(
            "SELECT memory_id FROM memories WHERE project_id=? AND module_id=? AND kind=? "
            "AND status='active' AND content=? LIMIT 1",
            (project_id, module_id, kind, content),
        ).fetchone()
        if existing:
            log.debug("append_set: dedup skip for %s/%s (existing=%s)", module_id, kind, existing["memory_id"])
            return {"memory_id": existing["memory_id"], "skipped": True, "conflict_policy": "append_set"}

    # Map old field names to new backend expectations
    backend_entry = {
        "ref_id": entry_dict.get("id", ""),
        "kind": kind,
        "module": module_id,
        "content": content,
        "summary": entry_dict.get("applies_when", ""),
        "tags": ",".join(entry_dict.get("related_nodes", [])),
        "structured": {
            "created_by": entry_dict.get("created_by", ""),
            "related_nodes": entry_dict.get("related_nodes", []),
            "supersedes": entry_dict.get("supersedes"),
        },
    }

    # --- append / append_set: do NOT supersede existing entries, always create new ---
    if conflict_policy in ("append", "append_set"):
        backend_entry["ref_id"] = ""  # Force new ref_id generation

    # --- merge_object: merge structured JSON with latest active entry for same ref_id ---
    elif conflict_policy == "merge_object" and backend_entry.get("ref_id"):
        old = backend.get_latest(conn, project_id, backend_entry["ref_id"])
        if old:
            old_structured = old.get("structured") or {}
            if isinstance(old_structured, str):
                try:
                    old_structured = json.loads(old_structured)
                except Exception:
                    old_structured = {}
            new_structured = backend_entry.get("structured") or {}
            if isinstance(new_structured, dict):
                merged = {**old_structured, **new_structured}
                backend_entry["structured"] = merged

    # If supersedes is set, use it as ref_id to trigger version chain
    if entry.supersedes and conflict_policy not in ("append", "append_set"):
        old = backend.get_latest(conn, project_id, entry.supersedes)
        if old and old.get("ref_id"):
            backend_entry["ref_id"] = old["ref_id"]

    result = backend.write(conn, project_id, backend_entry)
    result["conflict_policy"] = conflict_policy

    # Audit
    audit_service.record(
        conn, project_id, "memory.written",
        actor=session.get("principal_id", "") if session else "",
        module_id=entry.module_id, kind=entry.kind,
    )

    # Note: dbservice forwarding is handled by DockerBackend.write() when
    # MEMORY_BACKEND=docker. No separate forwarding needed here.

    return result


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


# ------------------------------------------------------------------
# Phase 8: Cross-project memory sharing
# ------------------------------------------------------------------

_PROMOTABLE_KINDS = {
    "failure_pattern", "architecture", "pattern", "rule", "decision", "knowledge",
}


def promote_memory(
    conn: sqlite3.Connection,
    project_id: str,
    memory_id: str,
    target_scope: str = "global",
    reason: str = "",
    actor_id: str = "",
) -> dict:
    """Promote a memory to a different scope (creates a copy with same ref_id).

    Used for cross-project knowledge sharing. The original stays project-scoped,
    the new entry gets the target scope (typically "global").
    """
    # 1. Fetch original
    row = conn.execute(
        "SELECT * FROM memories WHERE memory_id=? AND project_id=?",
        (memory_id, project_id),
    ).fetchone()
    if not row:
        from .errors import GovernanceError
        raise GovernanceError("NOT_FOUND", f"Memory {memory_id} not found", status=404)

    original = dict(row)
    kind = original.get("kind", "knowledge")
    if kind not in _PROMOTABLE_KINDS:
        from .errors import GovernanceError
        raise GovernanceError(
            "INVALID_KIND",
            f"Kind '{kind}' is not promotable. Allowed: {sorted(_PROMOTABLE_KINDS)}",
            status=400,
        )

    # 2. Create promoted copy via backend
    backend = get_backend()
    promoted = backend.write(conn, project_id, {
        "ref_id": original.get("ref_id", ""),
        "entity_id": original.get("entity_id", ""),
        "kind": kind,
        "module": original.get("module_id", ""),
        "content": original.get("content", ""),
        "summary": original.get("summary", ""),
        "scope": target_scope,
        "tags": original.get("tags", ""),
        "metadata_json": json.dumps({
            **(json.loads(original["metadata_json"]) if original.get("metadata_json") else {}),
            "promoted_from": memory_id,
            "promote_reason": reason,
        }, ensure_ascii=False),
    })

    # 3. Record event
    now = _utc_iso()
    conn.execute(
        "INSERT INTO memory_events (ref_id, event_type, actor_id, detail, metadata_json, created_at) "
        "VALUES (?, 'promoted', ?, ?, ?, ?)",
        (original.get("ref_id", ""), actor_id, reason,
         json.dumps({"target_scope": target_scope, "original_memory_id": memory_id}), now),
    )
    conn.commit()

    # 4. Audit
    audit_service.record(
        conn, project_id, "memory.promoted",
        actor=actor_id, module_id=original.get("module_id", ""), kind=kind,
    )

    log.info("memory promoted: %s → scope=%s (new=%s)", memory_id, target_scope, promoted.get("memory_id"))
    return promoted


# ------------------------------------------------------------------
# Phase 8: Domain Pack registration
# ------------------------------------------------------------------

_VALID_DURABILITIES = {"permanent", "durable", "session", "transient"}
_VALID_CONFLICT_POLICIES = {"replace", "append", "append_set", "temporal_replace", "merge_object"}


def register_domain_pack(
    conn: sqlite3.Connection,
    project_id: str,
    domain: str,
    types: dict,
    actor_id: str = "",
) -> dict:
    """Register or update a DomainPack for a project.

    Args:
        domain: Domain name (e.g., "development")
        types: Dict of {kind_name: {"durability": str, "conflictPolicy": str}}
    """
    now = _utc_iso()

    # Ensure domain_packs table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_packs (
            project_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            type_name TEXT NOT NULL,
            durability TEXT NOT NULL DEFAULT 'durable',
            conflict_policy TEXT NOT NULL DEFAULT 'replace',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, domain, type_name)
        )
    """)

    registered = 0
    for type_name, config in types.items():
        durability = config.get("durability", "durable")
        conflict_policy = config.get("conflictPolicy", config.get("conflict_policy", "replace"))

        if durability not in _VALID_DURABILITIES:
            from .errors import GovernanceError
            raise GovernanceError("INVALID_DURABILITY", f"Invalid durability '{durability}'. Allowed: {sorted(_VALID_DURABILITIES)}", status=400)
        if conflict_policy not in _VALID_CONFLICT_POLICIES:
            from .errors import GovernanceError
            raise GovernanceError("INVALID_CONFLICT_POLICY", f"Invalid conflictPolicy '{conflict_policy}'. Allowed: {sorted(_VALID_CONFLICT_POLICIES)}", status=400)

        conn.execute("""
            INSERT OR REPLACE INTO domain_packs
            (project_id, domain, type_name, durability, conflict_policy, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (project_id, domain, type_name, durability, conflict_policy, now, now))
        registered += 1

    conn.commit()

    audit_service.record(
        conn, project_id, "memory.pack_registered",
        actor=actor_id, metadata={"domain": domain, "type_count": registered},
    )

    log.info("domain pack registered: %s/%s (%d types)", project_id, domain, registered)
    return {"domain": domain, "types_registered": registered, "registered_at": now}


# ------------------------------------------------------------------
# TTL auto-archive
# ------------------------------------------------------------------

# Duration thresholds (seconds) per durability level
_TTL_SECONDS: Dict[str, int] = {
    "transient": 3_600,        # 1 hour
    "session":   86_400,       # 24 hours
    "durable":   7_776_000,    # 90 days
    # "permanent" → never archived
}


def archive_expired_memories(conn: sqlite3.Connection, project_id: str) -> dict:
    """Archive active memories whose durability TTL has elapsed.

    Reads domain_packs to find per-kind durability, then archives memories
    of matching kinds that were created before the TTL cutoff.

    Returns {"archived": N, "checked_kinds": N}
    """
    now_dt = datetime.now(timezone.utc)
    archived = 0
    checked_kinds = 0

    try:
        packs = conn.execute(
            "SELECT type_name, durability FROM domain_packs WHERE project_id=?",
            (project_id,),
        ).fetchall()
    except Exception:
        packs = []

    for row in packs:
        kind = row["type_name"]
        durability = row["durability"]
        ttl_secs = _TTL_SECONDS.get(durability)
        if ttl_secs is None:  # permanent — skip
            continue
        checked_kinds += 1
        from datetime import timedelta
        cutoff = (now_dt - timedelta(seconds=ttl_secs)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = conn.execute(
            "UPDATE memories SET status='archived', updated_at=? "
            "WHERE project_id=? AND kind=? AND status='active' AND created_at < ?",
            (_utc_iso(), project_id, kind, cutoff),
        )
        archived += result.rowcount

    if archived:
        conn.commit()
        log.info("TTL cleanup: archived %d memories for project %s", archived, project_id)

    return {"archived": archived, "checked_kinds": checked_kinds}
