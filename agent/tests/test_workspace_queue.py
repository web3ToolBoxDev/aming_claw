"""Tests for workspace_queue.py - workspace task queuing and auto-launch."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from workspace_queue import (  # noqa: E402
    _load_queue,
    _save_queue,
    enqueue_task,
    dequeue_task,
    peek_queue,
    list_queue,
    queue_length,
    remove_from_queue,
    list_all_queues,
    should_queue_task,
)


class TestWorkspaceQueueBasic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_empty_queue(self):
        self.assertEqual(queue_length("ws-test"), 0)
        self.assertIsNone(peek_queue("ws-test"))
        self.assertIsNone(dequeue_task("ws-test"))
        self.assertEqual(list_queue("ws-test"), [])

    def test_enqueue_and_dequeue(self):
        task_info = {
            "task_id": "task-001",
            "task_code": "T0001",
            "chat_id": 100,
            "user_id": 200,
            "text": "test task 1",
            "action": "codex",
        }
        pos = enqueue_task("ws-a", task_info)
        self.assertEqual(pos, 1)
        self.assertEqual(queue_length("ws-a"), 1)

        peeked = peek_queue("ws-a")
        self.assertIsNotNone(peeked)
        self.assertEqual(peeked["task_id"], "task-001")
        # peek should NOT remove
        self.assertEqual(queue_length("ws-a"), 1)

        dequeued = dequeue_task("ws-a")
        self.assertIsNotNone(dequeued)
        self.assertEqual(dequeued["task_id"], "task-001")
        self.assertEqual(queue_length("ws-a"), 0)

    def test_fifo_order(self):
        for i in range(3):
            enqueue_task("ws-b", {
                "task_id": "task-{:03d}".format(i),
                "text": "task {}".format(i),
            })
        self.assertEqual(queue_length("ws-b"), 3)

        for i in range(3):
            entry = dequeue_task("ws-b")
            self.assertEqual(entry["task_id"], "task-{:03d}".format(i))
        self.assertEqual(queue_length("ws-b"), 0)

    def test_remove_from_queue(self):
        enqueue_task("ws-c", {"task_id": "t1", "text": "first"})
        enqueue_task("ws-c", {"task_id": "t2", "text": "second"})
        enqueue_task("ws-c", {"task_id": "t3", "text": "third"})

        self.assertTrue(remove_from_queue("ws-c", "t2"))
        self.assertEqual(queue_length("ws-c"), 2)

        remaining = list_queue("ws-c")
        ids = [t["task_id"] for t in remaining]
        self.assertEqual(ids, ["t1", "t3"])

    def test_remove_nonexistent(self):
        self.assertFalse(remove_from_queue("ws-d", "nonexistent"))

    def test_multiple_workspaces(self):
        enqueue_task("ws-1", {"task_id": "a1"})
        enqueue_task("ws-2", {"task_id": "b1"})
        enqueue_task("ws-1", {"task_id": "a2"})

        self.assertEqual(queue_length("ws-1"), 2)
        self.assertEqual(queue_length("ws-2"), 1)

        all_q = list_all_queues()
        self.assertIn("ws-1", all_q)
        self.assertIn("ws-2", all_q)
        self.assertEqual(len(all_q["ws-1"]), 2)
        self.assertEqual(len(all_q["ws-2"]), 1)

    def test_enqueue_stores_fields(self):
        info = {
            "task_id": "task-field",
            "task_code": "T9999",
            "chat_id": 42,
            "requested_by": 99,
            "text": "do something",
            "action": "claude",
        }
        enqueue_task("ws-f", info)
        entry = peek_queue("ws-f")
        self.assertEqual(entry["task_id"], "task-field")
        self.assertEqual(entry["task_code"], "T9999")
        self.assertEqual(entry["chat_id"], 42)
        self.assertEqual(entry["user_id"], 99)
        self.assertEqual(entry["text"], "do something")
        self.assertEqual(entry["action"], "claude")
        self.assertIn("queued_at", entry)


class TestShouldQueueTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_no_active_tasks(self):
        self.assertFalse(should_queue_task("ws-empty"))

    def test_with_processing_task(self):
        from task_state import load_runtime_state, save_runtime_state
        state = load_runtime_state()
        state["active"]["task-abc"] = {
            "task_id": "task-abc",
            "status": "processing",
            "target_workspace_id": "ws-busy",
        }
        save_runtime_state(state)
        self.assertTrue(should_queue_task("ws-busy"))
        self.assertFalse(should_queue_task("ws-other"))

    def test_with_pending_acceptance_task(self):
        from task_state import load_runtime_state, save_runtime_state
        state = load_runtime_state()
        state["active"]["task-pa"] = {
            "task_id": "task-pa",
            "status": "pending_acceptance",
            "target_workspace_id": "ws-pa",
        }
        save_runtime_state(state)
        self.assertTrue(should_queue_task("ws-pa"))

    def test_accepted_task_not_blocking(self):
        from task_state import load_runtime_state, save_runtime_state
        state = load_runtime_state()
        state["active"]["task-done"] = {
            "task_id": "task-done",
            "status": "accepted",
            "target_workspace_id": "ws-free",
        }
        save_runtime_state(state)
        self.assertFalse(should_queue_task("ws-free"))


class TestQueuePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_persist_across_load(self):
        enqueue_task("ws-p", {"task_id": "t-persist"})
        # Reload queue from disk
        data = _load_queue()
        self.assertIn("ws-p", data.get("queues", {}))
        self.assertEqual(len(data["queues"]["ws-p"]), 1)
        self.assertEqual(data["queues"]["ws-p"][0]["task_id"], "t-persist")


if __name__ == "__main__":
    unittest.main()
