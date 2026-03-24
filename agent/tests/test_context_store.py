"""Tests for context_store.py — context_audit table, insert_audit, query_audit_by_session."""

import os
import sys
import tempfile
import unittest

agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, agent_dir)

from context_store import ContextStore


class TestContextAuditSchema(unittest.TestCase):
    """context_audit table is created during _init_schema."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = ContextStore(db_path=self.tmp.name)

    def tearDown(self):
        self.store._conn.close()
        os.unlink(self.tmp.name)

    def test_table_exists(self):
        cur = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='context_audit'"
        )
        self.assertIsNotNone(cur.fetchone())

    def test_columns_present(self):
        cur = self.store._conn.execute("PRAGMA table_info(context_audit)")
        cols = {row[1] for row in cur.fetchall()}
        expected = {
            "id", "session_id", "project_id", "role", "prompt",
            "ai_stdout", "status", "duration_ms", "created_at",
        }
        self.assertTrue(expected.issubset(cols))


class TestInsertAudit(unittest.TestCase):
    """insert_audit writes a record to context_audit."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = ContextStore(db_path=self.tmp.name)

    def tearDown(self):
        self.store._conn.close()
        os.unlink(self.tmp.name)

    def test_insert_full_record(self):
        self.store.insert_audit(
            session_id="sess-001",
            project_id="proj-A",
            role="user",
            prompt="hello",
            ai_stdout="world",
            status="ok",
            duration_ms=123,
        )
        cur = self.store._conn.execute(
            "SELECT * FROM context_audit WHERE session_id='sess-001'"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["session_id"], "sess-001")
        self.assertEqual(row["project_id"], "proj-A")
        self.assertEqual(row["role"], "user")
        self.assertEqual(row["prompt"], "hello")
        self.assertEqual(row["ai_stdout"], "world")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["duration_ms"], 123)

    def test_insert_nullable_fields(self):
        self.store.insert_audit(
            session_id="sess-002",
            project_id=None,
            role=None,
            prompt=None,
            ai_stdout=None,
            status=None,
            duration_ms=None,
        )
        cur = self.store._conn.execute(
            "SELECT * FROM context_audit WHERE session_id='sess-002'"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["project_id"])

    def test_insert_multiple_rows_same_session(self):
        for i in range(3):
            self.store.insert_audit(
                session_id="sess-multi",
                project_id=None,
                role="assistant",
                prompt=f"q{i}",
                ai_stdout=f"a{i}",
                status="ok",
                duration_ms=i * 10,
            )
        cur = self.store._conn.execute(
            "SELECT COUNT(*) FROM context_audit WHERE session_id='sess-multi'"
        )
        self.assertEqual(cur.fetchone()[0], 3)


class TestQueryAuditBySession(unittest.TestCase):
    """query_audit_by_session returns dicts ordered by created_at ASC."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = ContextStore(db_path=self.tmp.name)

    def tearDown(self):
        self.store._conn.close()
        os.unlink(self.tmp.name)

    def _insert(self, session_id, prompt, status="ok"):
        self.store.insert_audit(
            session_id=session_id,
            project_id="proj-X",
            role="user",
            prompt=prompt,
            ai_stdout="",
            status=status,
            duration_ms=0,
        )

    def test_returns_list_of_dicts(self):
        self._insert("s1", "p1")
        result = self.store.query_audit_by_session("s1")
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], dict)

    def test_empty_session_returns_empty_list(self):
        result = self.store.query_audit_by_session("no-such-session")
        self.assertEqual(result, [])

    def test_filters_by_session(self):
        self._insert("sA", "qa")
        self._insert("sB", "qb")
        result = self.store.query_audit_by_session("sA")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["session_id"], "sA")

    def test_dict_keys(self):
        self._insert("sk", "prompt-text")
        row = self.store.query_audit_by_session("sk")[0]
        for key in ("session_id", "project_id", "role", "prompt",
                    "ai_stdout", "status", "duration_ms", "created_at"):
            self.assertIn(key, row)

    def test_ordering_ascending(self):
        # Insert with explicit created_at values to guarantee order
        with self.store._conn:
            self.store._conn.executemany(
                """
                INSERT INTO context_audit
                    (session_id, project_id, role, prompt, ai_stdout, status, duration_ms, created_at)
                VALUES (?, NULL, NULL, ?, NULL, NULL, NULL, ?)
                """,
                [
                    ("s-ord", "first",  "2024-01-01T00:00:01Z"),
                    ("s-ord", "second", "2024-01-01T00:00:02Z"),
                    ("s-ord", "third",  "2024-01-01T00:00:03Z"),
                ],
            )
        result = self.store.query_audit_by_session("s-ord")
        prompts = [r["prompt"] for r in result]
        self.assertEqual(prompts, ["first", "second", "third"])


if __name__ == "__main__":
    unittest.main()
