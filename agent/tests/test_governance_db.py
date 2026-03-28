"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create required directory structure
        os.makedirs(os.path.join(self.tmp.name, "codex-tasks", "state", "governance", "test-project"), exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_get_connection_creates_db(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        self.assertIsNotNone(conn)
        # Verify tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("node_state", table_names)
        self.assertIn("sessions", table_names)
        self.assertIn("tasks", table_names)
        self.assertIn("audit_index", table_names)
        self.assertIn("snapshots", table_names)
        self.assertIn("idempotency_keys", table_names)
        self.assertIn("node_history", table_names)
        close_connection(conn)

    def test_schema_version_tracking(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(row["value"], "7")
        close_connection(conn)

    def test_wal_mode(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(mode[0], "wal")
        close_connection(conn)

    def test_db_context(self):
        from governance.db import DBContext
        with DBContext("test-project") as conn:
            conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, updated_at) VALUES (?, ?, ?, ?)",
                ("test-project", "L0.1", "pending", "2026-01-01"),
            )
        # Verify committed
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        row = conn.execute("SELECT * FROM node_state WHERE node_id = 'L0.1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["verify_status"], "pending")
        close_connection(conn)


if __name__ == "__main__":
    unittest.main()
