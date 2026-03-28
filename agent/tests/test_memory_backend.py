"""Tests for Phase 2: Memory backend with SQLite + FTS5."""

import json
import os
import sys
import tempfile
import unittest

# Ensure agent/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestLocalBackend(unittest.TestCase):
    """Test LocalBackend (SQLite + FTS5)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        # Reset singleton
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ["MEMORY_BACKEND"] = "local"

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_conn(self, project_id="test-project"):
        from governance.db import get_connection
        return get_connection(project_id)

    def test_write_and_query(self):
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            result = backend.write(conn, "test-project", {
                "kind": "decision",
                "module": "auth",
                "content": "Use JWT tokens for API authentication",
                "summary": "JWT auth decision",
            })
            self.assertIn("memory_id", result)
            self.assertEqual(result["kind"], "decision")
            self.assertEqual(result["status"], "active")

            # Query back
            entries = backend.query(conn, "test-project", module="auth")
            self.assertEqual(len(entries), 1)
            self.assertIn("JWT", entries[0]["content"])
        finally:
            conn.close()

    def test_fts5_search(self):
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            backend.write(conn, "test-project", {
                "kind": "pitfall",
                "module": "database",
                "content": "SQLite WAL mode causes lock contention under heavy write load",
            })
            backend.write(conn, "test-project", {
                "kind": "pattern",
                "module": "auth",
                "content": "Always validate JWT expiration before processing requests",
            })

            # Search for "WAL lock"
            results = backend.search(conn, "test-project", "WAL lock")
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0]["module_id"], "database")
            self.assertEqual(results[0]["search_mode"], "fts5")

            # Search for "JWT"
            results = backend.search(conn, "test-project", "JWT")
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0]["module_id"], "auth")
        finally:
            conn.close()

    def test_version_chain_supersede(self):
        """Same ref_id, multiple writes → only latest is active."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            # Write v1
            v1 = backend.write(conn, "test-project", {
                "ref_id": "auth-decision-1",
                "kind": "decision",
                "module": "auth",
                "content": "Use session cookies",
            })
            self.assertEqual(v1["version"], 1)

            # Write v2 with same ref_id → supersedes v1
            v2 = backend.write(conn, "test-project", {
                "ref_id": "auth-decision-1",
                "kind": "decision",
                "module": "auth",
                "content": "Use JWT tokens instead of cookies",
            })
            self.assertEqual(v2["version"], 2)
            self.assertEqual(v2["superseded_id"], v1["memory_id"])

            # Query: only v2 should be active
            entries = backend.query(conn, "test-project", ref_id="auth-decision-1")
            self.assertEqual(len(entries), 1)
            self.assertIn("JWT", entries[0]["content"])

            # get_latest should return v2
            latest = backend.get_latest(conn, "test-project", "auth-decision-1")
            self.assertIsNotNone(latest)
            self.assertIn("JWT", latest["content"])
        finally:
            conn.close()

    def test_fts_excludes_superseded(self):
        """FTS search must NOT return superseded memories."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            backend.write(conn, "test-project", {
                "ref_id": "db-pitfall-1",
                "kind": "pitfall",
                "module": "database",
                "content": "Never use SELECT * in production queries",
            })
            # Supersede with new version
            backend.write(conn, "test-project", {
                "ref_id": "db-pitfall-1",
                "kind": "pitfall",
                "module": "database",
                "content": "Avoid SELECT * — always specify columns explicitly",
            })

            # Search should only return the latest version
            results = backend.search(conn, "test-project", "SELECT columns")
            active_results = [r for r in results if "specify columns" in r["content"]]
            superseded = [r for r in results if "Never use SELECT" in r["content"]]
            self.assertEqual(len(superseded), 0, "Superseded memory should not appear in search")
        finally:
            conn.close()

    def test_delete_archives(self):
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            result = backend.write(conn, "test-project", {
                "kind": "note",
                "module": "general",
                "content": "Temporary note to delete",
            })
            mid = result["memory_id"]

            deleted = backend.delete(conn, "test-project", mid)
            self.assertTrue(deleted)

            # Should not appear in active query
            entries = backend.query(conn, "test-project", active_only=True)
            self.assertEqual(len([e for e in entries if e["memory_id"] == mid]), 0)
        finally:
            conn.close()

    def test_scope_isolation(self):
        """Project A memory not visible to Project B query."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn_a = self._get_conn("project-a")
        conn_b = self._get_conn("project-b")
        try:
            backend.write(conn_a, "project-a", {
                "kind": "secret",
                "module": "auth",
                "content": "Project A secret data",
            })
            # Project B should see nothing
            entries = backend.query(conn_b, "project-b", module="auth")
            self.assertEqual(len(entries), 0)
        finally:
            conn_a.close()
            conn_b.close()

    def test_empty_search_returns_empty(self):
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            results = backend.search(conn, "test-project", "")
            self.assertEqual(results, [])
            results = backend.search(conn, "test-project", "   ")
            self.assertEqual(results, [])
        finally:
            conn.close()

    def test_memory_service_write_and_search(self):
        """Test through the memory_service public API."""
        from governance.models import MemoryEntry
        from governance import memory_service
        conn = self._get_conn()
        try:
            entry = MemoryEntry(
                module_id="executor",
                kind="pitfall",
                content="Executor timeout causes tasks to stay claimed forever",
            )
            result = memory_service.write_memory(conn, "test-project", entry)
            self.assertIn("memory_id", result)

            # Search via service
            results = memory_service.search_memories(conn, "test-project", "executor timeout")
            self.assertGreater(len(results), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
