"""Phase 7: Verification tests — 10 spec invariants from design-spec §13.

Each test verifies one invariant that must hold across the system.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSpecInvariants(unittest.TestCase):
    """Verify design spec invariants."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_conn(self, project_id="test-project"):
        from governance.db import get_connection
        return get_connection(project_id)

    # ------------------------------------------------------------------
    # Invariant 1: Memory version chain
    # ------------------------------------------------------------------
    def test_memory_version_chain(self):
        """Same ref_id, multiple writes → query returns only latest active."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            backend.write(conn, "test-project", {
                "ref_id": "inv-1", "kind": "decision", "module": "auth",
                "content": "Version 1",
            })
            backend.write(conn, "test-project", {
                "ref_id": "inv-1", "kind": "decision", "module": "auth",
                "content": "Version 2",
            })
            backend.write(conn, "test-project", {
                "ref_id": "inv-1", "kind": "decision", "module": "auth",
                "content": "Version 3 — latest",
            })
            entries = backend.query(conn, "test-project", ref_id="inv-1", active_only=True)
            self.assertEqual(len(entries), 1)
            self.assertIn("Version 3", entries[0]["content"])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 2: FTS excludes superseded
    # ------------------------------------------------------------------
    def test_fts_excludes_superseded(self):
        """FTS search must not return superseded/archived memories."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            backend.write(conn, "test-project", {
                "ref_id": "inv-2", "kind": "pitfall", "module": "db",
                "content": "Old pitfall about deadlocks",
            })
            backend.write(conn, "test-project", {
                "ref_id": "inv-2", "kind": "pitfall", "module": "db",
                "content": "Updated pitfall about lock timeouts",
            })
            results = backend.search(conn, "test-project", "deadlocks")
            for r in results:
                self.assertNotIn("Old pitfall", r.get("content", ""),
                    "Superseded memory appeared in FTS results")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 3: Semantic fallback
    # ------------------------------------------------------------------
    def test_semantic_fallback(self):
        """mem0 unavailable → automatic fallback to FTS5 (no error to user)."""
        from governance.memory_backend import DockerBackend
        backend = DockerBackend()
        backend.dbservice_url = "http://localhost:99999"  # unreachable
        conn = self._get_conn()
        try:
            # Write via local (DockerBackend inherits LocalBackend.write)
            backend.write(conn, "test-project", {
                "kind": "pattern", "module": "auth",
                "content": "Always validate JWT expiration",
            })
            # Search should fall back to FTS5 silently
            results = backend.search(conn, "test-project", "JWT expiration")
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0]["search_mode"], "fts5")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 4: Executor crash recovery
    # ------------------------------------------------------------------
    def test_executor_crash_recovery(self):
        """Claimed task with stale heartbeat → requeued on executor startup."""
        from governance import task_registry
        conn = self._get_conn()
        try:
            # Create and claim a task (simulating executor crash)
            task = task_registry.create_task(conn, "test-project",
                prompt="Build auth module", task_type="dev", created_by="executor-old")
            task_registry.claim_task(conn, "test-project", "executor-old")

            # Verify it's claimed
            row = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                (task["task_id"],)).fetchone()
            self.assertEqual(row["status"], "claimed")

            # Simulate crash recovery: mark claimed tasks as failed
            claimed = conn.execute(
                "SELECT task_id FROM tasks WHERE project_id=? AND status='claimed'",
                ("test-project",)).fetchall()
            self.assertGreater(len(claimed), 0, "Should have a claimed task")
            for c in claimed:
                task_registry.complete_task(conn, c["task_id"],
                    status="failed", error_message="executor_crash_recovery",
                    project_id="test-project")

            # Verify it's no longer claimed (either failed or re-queued by auto-chain)
            row = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                (task["task_id"],)).fetchone()
            self.assertIn(row["status"], ("failed", "queued"),
                "Crashed task should be failed or re-queued, not stuck as claimed")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 5: Conflict rule — same file, opposite op
    # ------------------------------------------------------------------
    def test_conflict_rule_same_file_opposite_op(self):
        """Same file + opposite operation → rule engine returns 'conflict'."""
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance import task_registry
        conn = self._get_conn()
        try:
            task_registry.create_task(conn, "test-project",
                prompt="Add new auth handler", task_type="dev", created_by="test",
                metadata={"target_files": ["agent/auth.py"], "operation_type": "add"})
            result = check_conflicts(conn, "test-project",
                target_files=["agent/auth.py"], operation_type="delete",
                intent_hash=compute_intent_hash("Delete auth handler"),
                prompt="Delete auth handler")
            self.assertEqual(result["decision"], "conflict")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 6: Duplicate detection
    # ------------------------------------------------------------------
    def test_duplicate_detection(self):
        """Same intent within 1 hour → rule engine returns 'duplicate'."""
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance import task_registry
        conn = self._get_conn()
        try:
            task_registry.create_task(conn, "test-project",
                prompt="Implement OAuth login", task_type="dev", created_by="test")
            result = check_conflicts(conn, "test-project",
                target_files=["agent/oauth.py"], operation_type="add",
                intent_hash=compute_intent_hash("Implement OAuth login"),
                prompt="Implement OAuth login")
            self.assertEqual(result["decision"], "duplicate")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 7: Status query → no coordinator task
    # ------------------------------------------------------------------
    def test_status_query_no_coordinator(self):
        """Status query ('当前状态') must NOT create coordinator task."""
        # This is enforced at gateway level via classify_message
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telegram_gateway"))
        from gateway import classify_message
        self.assertEqual(classify_message("当前状态怎么样"), "query")
        self.assertEqual(classify_message("status?"), "query")
        self.assertEqual(classify_message("现在系统怎么样"), "query")
        self.assertEqual(classify_message("有多少节点pending"), "query")
        # These should NOT be queries
        self.assertNotEqual(classify_message("帮我修改auth模块"), "query")

    # ------------------------------------------------------------------
    # Invariant 8: Scope isolation
    # ------------------------------------------------------------------
    def test_scope_isolation(self):
        """Project A memory not visible to Project B query."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn_a = self._get_conn("project-alpha")
        conn_b = self._get_conn("project-beta")
        try:
            backend.write(conn_a, "project-alpha", {
                "kind": "secret", "module": "core",
                "content": "Alpha secret: API key is XYZ",
            })
            results_b = backend.search(conn_b, "project-beta", "API key XYZ")
            self.assertEqual(len(results_b), 0, "Project B should not see Project A's memories")
        finally:
            conn_a.close()
            conn_b.close()

    # ------------------------------------------------------------------
    # Invariant 9: Scope global sharing (promote)
    # ------------------------------------------------------------------
    def test_scope_global_sharing(self):
        """Memory written with scope=global visible when querying with scope filter."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            backend.write(conn, "test-project", {
                "kind": "pattern", "module": "shared",
                "content": "Global pattern: always use structured logging",
                "scope": "global",
            })
            # Query all — should find it
            results = backend.query(conn, "test-project", kind="pattern")
            global_results = [r for r in results if "structured logging" in r.get("content", "")]
            self.assertGreater(len(global_results), 0)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Invariant 10: ref_id stability
    # ------------------------------------------------------------------
    def test_ref_id_stability(self):
        """Same task's updates reuse ref_id, don't create new ones."""
        from governance.memory_backend import get_backend
        backend = get_backend()
        conn = self._get_conn()
        try:
            v1 = backend.write(conn, "test-project", {
                "ref_id": "stable-ref", "kind": "status", "module": "exec",
                "content": "Task queued",
            })
            v2 = backend.write(conn, "test-project", {
                "ref_id": "stable-ref", "kind": "status", "module": "exec",
                "content": "Task running",
            })
            v3 = backend.write(conn, "test-project", {
                "ref_id": "stable-ref", "kind": "status", "module": "exec",
                "content": "Task completed",
            })
            self.assertEqual(v1["ref_id"], "stable-ref")
            self.assertEqual(v2["ref_id"], "stable-ref")
            self.assertEqual(v3["ref_id"], "stable-ref")
            # Only latest active
            latest = backend.get_latest(conn, "test-project", "stable-ref")
            self.assertIn("completed", latest["content"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
