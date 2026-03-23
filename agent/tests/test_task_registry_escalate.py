"""Tests for task_registry escalate_task and retry_round/parent_task_id fields."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from governance.db import get_connection
    conn = get_connection("proj")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


class TestCreateTaskNewFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_create_task_default_retry_fields(self):
        from governance.task_registry import create_task
        result = create_task(self.conn, "proj", "do something")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT retry_round, parent_task_id FROM tasks WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()
        self.assertEqual(row["retry_round"], 0)
        self.assertIsNone(row["parent_task_id"])

    def test_create_task_with_retry_fields(self):
        from governance.task_registry import create_task
        result = create_task(
            self.conn, "proj", "retry task",
            parent_task_id="task-parent-abc",
            retry_round=2,
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT retry_round, parent_task_id FROM tasks WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()
        self.assertEqual(row["retry_round"], 2)
        self.assertEqual(row["parent_task_id"], "task-parent-abc")


class TestEscalateTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _create(self, **kwargs):
        from governance.task_registry import create_task
        result = create_task(self.conn, "proj", "test prompt", **kwargs)
        self.conn.commit()
        return result["task_id"]

    def test_escalate_round_0_creates_child(self):
        from governance.task_registry import escalate_task
        parent_id = self._create()
        new_id = escalate_task(self.conn, parent_id)
        self.conn.commit()
        self.assertIsNotNone(new_id)
        row = self.conn.execute(
            "SELECT retry_round, parent_task_id FROM tasks WHERE task_id = ?",
            (new_id,),
        ).fetchone()
        self.assertEqual(row["retry_round"], 1)
        self.assertEqual(row["parent_task_id"], parent_id)

    def test_escalate_round_1_creates_round_2(self):
        from governance.task_registry import escalate_task
        task_id = self._create(retry_round=1, parent_task_id="task-root")
        new_id = escalate_task(self.conn, task_id)
        self.conn.commit()
        self.assertIsNotNone(new_id)
        row = self.conn.execute(
            "SELECT retry_round FROM tasks WHERE task_id = ?", (new_id,)
        ).fetchone()
        self.assertEqual(row["retry_round"], 2)

    def test_escalate_round_2_creates_round_3(self):
        from governance.task_registry import escalate_task
        task_id = self._create(retry_round=2)
        new_id = escalate_task(self.conn, task_id)
        self.conn.commit()
        self.assertIsNotNone(new_id)
        row = self.conn.execute(
            "SELECT retry_round FROM tasks WHERE task_id = ?", (new_id,)
        ).fetchone()
        self.assertEqual(row["retry_round"], 3)

    def test_escalate_round_3_marks_design_mismatch(self):
        from governance.task_registry import escalate_task
        task_id = self._create(retry_round=3)
        result = escalate_task(self.conn, task_id)
        self.conn.commit()
        self.assertIsNone(result)
        row = self.conn.execute(
            "SELECT execution_status, status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["execution_status"], "design_mismatch")
        self.assertEqual(row["status"], "design_mismatch")

    def test_escalate_not_found_raises(self):
        from governance.task_registry import escalate_task
        from governance.errors import GovernanceError
        with self.assertRaises(GovernanceError):
            escalate_task(self.conn, "task-nonexistent-xyz")

    def test_escalate_chain_reaches_design_mismatch(self):
        """Full chain: round 0 → 1 → 2 → 3 → design_mismatch."""
        from governance.task_registry import escalate_task
        task_id = self._create()

        for expected_round in range(1, 4):
            new_id = escalate_task(self.conn, task_id)
            self.conn.commit()
            self.assertIsNotNone(new_id)
            row = self.conn.execute(
                "SELECT retry_round FROM tasks WHERE task_id = ?", (new_id,)
            ).fetchone()
            self.assertEqual(row["retry_round"], expected_round)
            task_id = new_id

        # task_id now has retry_round=3 → design_mismatch
        result = escalate_task(self.conn, task_id)
        self.conn.commit()
        self.assertIsNone(result)
        row = self.conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        self.assertEqual(row["execution_status"], "design_mismatch")


if __name__ == "__main__":
    unittest.main()
