"""Memory backend — pluggable storage interface with local SQLite + FTS5 default.

Three backends:
  - LocalBackend  : SQLite + FTS5 (default, zero-dependency)
  - DockerBackend : SQLite + dbservice mem0 for vector search (Phase 6)
  - CloudBackend  : Future cloud API (Phase 7, stub)

Select via MEMORY_BACKEND env var: "local" (default) | "docker" | "cloud"
"""

import json
import logging
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_memory_id() -> str:
    import hashlib
    import time
    ts = str(int(time.time() * 1000))
    h = hashlib.sha256(ts.encode() + os.urandom(8)).hexdigest()[:8]
    return f"mem-{ts}-{h}"


# ------------------------------------------------------------------
# Abstract interface
# ------------------------------------------------------------------

class MemoryBackend(ABC):
    """Abstract memory storage backend."""

    @abstractmethod
    def write(self, conn: sqlite3.Connection, project_id: str, entry: dict) -> dict:
        """Write a memory entry. Returns the stored entry dict."""

    @abstractmethod
    def search(self, conn: sqlite3.Connection, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        """Search memories. Returns list of {ref_id, score, search_mode, matched_text, metadata}."""

    @abstractmethod
    def query(self, conn: sqlite3.Connection, project_id: str, *,
              module: str = None, kind: str = None, ref_id: str = None,
              active_only: bool = True) -> list[dict]:
        """Structured query by module/kind/ref_id."""

    @abstractmethod
    def delete(self, conn: sqlite3.Connection, project_id: str, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if deleted."""

    @abstractmethod
    def get_latest(self, conn: sqlite3.Connection, project_id: str, ref_id: str) -> Optional[dict]:
        """Get the latest active version for a ref_id."""


# ------------------------------------------------------------------
# Local backend: SQLite + FTS5
# ------------------------------------------------------------------

class LocalBackend(MemoryBackend):
    """SQLite + FTS5 full-text search backend (default, zero external deps)."""

    def write(self, conn: sqlite3.Connection, project_id: str, entry: dict) -> dict:
        now = _utc_iso()
        memory_id = entry.get("memory_id") or _gen_memory_id()
        ref_id = entry.get("ref_id", "")
        kind = entry.get("kind", "knowledge")
        module_id = entry.get("module", entry.get("module_id", ""))
        content = entry.get("content", "")
        summary = entry.get("summary", content[:200] if content else "")
        scope = entry.get("scope", "project")
        metadata = entry.get("metadata_json") or entry.get("structured") or {}
        tags = entry.get("tags", "")
        if isinstance(tags, list):
            tags = ",".join(tags)
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata, ensure_ascii=False)
        status = "active"
        version = 1

        # If ref_id provided and existing active entry exists, supersede it
        superseded_id = None
        if ref_id:
            existing = conn.execute(
                "SELECT memory_id, version FROM memories "
                "WHERE project_id=? AND ref_id=? AND status='active' "
                "ORDER BY version DESC LIMIT 1",
                (project_id, ref_id),
            ).fetchone()
            if existing:
                superseded_id = existing["memory_id"]
                version = existing["version"] + 1
                conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by_memory_id=?, updated_at=? "
                    "WHERE memory_id=?",
                    (memory_id, now, superseded_id),
                )

        if not ref_id:
            ref_id = f"{module_id}:{kind}:{memory_id}"

        conn.execute("""
            INSERT INTO memories (
                memory_id, project_id, ref_id, kind, module_id, scope, content, summary,
                metadata_json, tags, version, status, superseded_by_memory_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """, (
            memory_id, project_id, ref_id, kind, module_id, scope, content, summary,
            metadata, tags, version, status, now, now,
        ))
        conn.commit()

        result = {
            "memory_id": memory_id,
            "ref_id": ref_id,
            "kind": kind,
            "module_id": module_id,
            "content": content,
            "summary": summary,
            "version": version,
            "status": status,
            "superseded_id": superseded_id,
            "created_at": now,
        }
        return result

    def search(self, conn: sqlite3.Connection, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        if not query or not query.strip():
            return []

        # FTS5 query: tokenize the query for MATCH
        fts_query = self._build_fts_query(query)
        try:
            rows = conn.execute("""
                SELECT m.memory_id, m.ref_id, m.kind, m.module_id, m.content, m.summary,
                       m.metadata_json, m.version, m.created_at,
                       rank AS score
                FROM memories_fts fts
                JOIN memories m ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ?
                  AND m.project_id = ?
                  AND m.status = 'active'
                ORDER BY rank
                LIMIT ?
            """, (fts_query, project_id, top_k)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("FTS5 search failed (query=%r): %s", fts_query, e)
            # Fallback to LIKE search
            return self._like_search(conn, project_id, query, top_k)

        return [
            {
                "memory_id": r["memory_id"],
                "ref_id": r["ref_id"],
                "kind": r["kind"],
                "module_id": r["module_id"],
                "content": r["content"],
                "summary": r["summary"],
                "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                "version": r["version"],
                "score": r["score"],
                "search_mode": "fts5",
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def _build_fts_query(self, query: str) -> str:
        """Build FTS5 MATCH expression from natural-language query."""
        # Split into tokens, quote each, join with OR for broad matching
        tokens = query.strip().split()
        if not tokens:
            return '""'
        # Use OR to match any token; each token quoted to handle special chars
        parts = []
        for t in tokens:
            clean = t.replace('"', '').strip()
            if clean:
                parts.append(f'"{clean}"')
        if not parts:
            return '""'
        return " OR ".join(parts)

    def _like_search(self, conn: sqlite3.Connection, project_id: str, query: str, top_k: int) -> list[dict]:
        """Fallback LIKE-based search when FTS5 fails."""
        pattern = f"%{query}%"
        rows = conn.execute("""
            SELECT memory_id, ref_id, kind, module_id, content, summary,
                   metadata_json, version, created_at
            FROM memories
            WHERE project_id = ? AND status = 'active'
              AND (content LIKE ? OR summary LIKE ? OR module_id LIKE ?)
            ORDER BY created_at DESC
            LIMIT ?
        """, (project_id, pattern, pattern, pattern, top_k)).fetchall()
        return [
            {
                "memory_id": r["memory_id"],
                "ref_id": r["ref_id"],
                "kind": r["kind"],
                "module_id": r["module_id"],
                "content": r["content"],
                "summary": r["summary"],
                "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                "version": r["version"],
                "score": 0.0,
                "search_mode": "like",
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def query(self, conn: sqlite3.Connection, project_id: str, *,
              module: str = None, kind: str = None, ref_id: str = None,
              active_only: bool = True) -> list[dict]:
        conditions = ["project_id = ?"]
        params: list = [project_id]

        if active_only:
            conditions.append("status = 'active'")
        if module:
            conditions.append("module_id = ?")
            params.append(module)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if ref_id:
            conditions.append("ref_id = ?")
            params.append(ref_id)

        sql = f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, conn: sqlite3.Connection, project_id: str, memory_id: str) -> bool:
        cur = conn.execute(
            "UPDATE memories SET status='archived', updated_at=? WHERE project_id=? AND memory_id=?",
            (_utc_iso(), project_id, memory_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def get_latest(self, conn: sqlite3.Connection, project_id: str, ref_id: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM memories WHERE project_id=? AND ref_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (project_id, ref_id),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        if "metadata_json" in d and d["metadata_json"]:
            try:
                d["metadata"] = json.loads(d["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
        else:
            d["metadata"] = {}
        return d


# ------------------------------------------------------------------
# Docker backend: SQLite + dbservice mem0 (Phase 6 stub)
# ------------------------------------------------------------------

class DockerBackend(LocalBackend):
    """SQLite + dbservice mem0 for vector search. Falls back to FTS5 if unavailable."""

    def __init__(self):
        self.dbservice_url = os.environ.get("DBSERVICE_URL", "http://localhost:40002")

    def write(self, conn: sqlite3.Connection, project_id: str, entry: dict) -> dict:
        result = super().write(conn, project_id, entry)
        # Phase 6: Forward to dbservice for vector indexing (best-effort)
        self._index_to_dbservice(result)
        return result

    def search(self, conn: sqlite3.Connection, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        # Phase 6: Try semantic search via dbservice first, fallback to FTS5
        return super().search(conn, project_id, query, top_k)

    def _index_to_dbservice(self, entry: dict) -> None:
        """Forward to dbservice for semantic indexing (Phase 6 implementation)."""
        pass  # Stub — will be implemented in Phase 6


# ------------------------------------------------------------------
# Cloud backend: future placeholder (Phase 7 stub)
# ------------------------------------------------------------------

class CloudBackend(LocalBackend):
    """Future cloud API backend. Falls back to local FTS5."""

    def __init__(self):
        self.cloud_url = os.environ.get("MEMORY_CLOUD_URL", "")
        # Phase 7: implement cloud write/search

    def search(self, conn: sqlite3.Connection, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        # Phase 7: Try cloud search first, fallback to FTS5
        return super().search(conn, project_id, query, top_k)


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

_backend_instance: Optional[MemoryBackend] = None


def get_backend() -> MemoryBackend:
    """Get the configured memory backend (singleton)."""
    global _backend_instance
    if _backend_instance is None:
        backend_type = os.environ.get("MEMORY_BACKEND", "local").lower()
        if backend_type == "docker":
            _backend_instance = DockerBackend()
        elif backend_type == "cloud":
            _backend_instance = CloudBackend()
        else:
            _backend_instance = LocalBackend()
        log.info("Memory backend: %s", type(_backend_instance).__name__)
    return _backend_instance
