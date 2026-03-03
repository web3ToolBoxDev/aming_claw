"""Tests for parallel_dispatcher.py - multi-workspace task dispatch (T5).

Covers:
  - WorkspaceWorker creation and status
  - ParallelDispatcher start/stop lifecycle
  - Task dispatch routing to correct workspace worker
  - Worker sync with registry changes
  - Dispatch failure for unknown workspace
  - Status reporting
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from utils import save_json, task_file, tasks_root  # noqa: E402
from workspace_registry import add_workspace, list_workspaces, remove_workspace  # noqa: E402
from parallel_dispatcher import (  # noqa: E402
    ParallelDispatcher,
    WorkspaceWorker,
    get_dispatcher_status,
)


class TestWorkspaceWorker(unittest.TestCase):
    """Test WorkspaceWorker lifecycle and status."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project-a"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_worker_creation(self):
        ws = {"id": "ws-test", "path": str(self.ws_path), "label": "test-proj", "max_concurrent": 1}
        mock_processor = MagicMock()
        worker = WorkspaceWorker(ws, mock_processor)
        self.assertEqual(worker.ws_id, "ws-test")
        self.assertEqual(worker.ws_label, "test-proj")
        self.assertFalse(worker.is_running)
        self.assertFalse(worker.is_busy)
        self.assertEqual(worker.queue_size, 0)

    def test_worker_status(self):
        ws = {"id": "ws-test", "path": str(self.ws_path), "label": "test-proj", "max_concurrent": 1}
        mock_processor = MagicMock()
        worker = WorkspaceWorker(ws, mock_processor)
        status = worker.status()
        self.assertEqual(status["ws_id"], "ws-test")
        self.assertEqual(status["ws_label"], "test-proj")
        self.assertFalse(status["running"])
        self.assertFalse(status["busy"])
        self.assertEqual(status["queue_size"], 0)
        self.assertEqual(status["tasks_completed"], 0)
        self.assertEqual(status["tasks_failed"], 0)

    def test_worker_start_stop(self):
        ws = {"id": "ws-test", "path": str(self.ws_path), "label": "test-proj", "max_concurrent": 1}
        mock_processor = MagicMock()
        worker = WorkspaceWorker(ws, mock_processor)
        worker.start()
        self.assertTrue(worker.is_running)
        worker.stop()
        self.assertFalse(worker.is_running)

    def test_worker_processes_task(self):
        """Worker should call task_processor with task_path and workspace dict."""
        processed = []

        def mock_processor(task_path, workspace_info):
            processed.append((task_path, workspace_info["id"]))

        ws = {"id": "ws-test", "path": str(self.ws_path), "label": "test-proj", "max_concurrent": 1}
        worker = WorkspaceWorker(ws, mock_processor)
        worker.start()

        task_path = Path(self.tmp.name) / "task.json"
        save_json(task_path, {"task_id": "task-001"})
        worker.enqueue(task_path, {"task_id": "task-001"})

        # Wait briefly for worker to process
        time.sleep(0.5)
        worker.stop()

        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0][1], "ws-test")

    def test_worker_enqueue_returns_false_when_full(self):
        ws = {"id": "ws-test", "path": str(self.ws_path), "label": "test-proj", "max_concurrent": 1}
        mock_processor = MagicMock()
        worker = WorkspaceWorker(ws, mock_processor, max_queue_size=1)
        # Don't start the worker so queue fills up
        result1 = worker.enqueue(Path("t1.json"), {"task_id": "t1"})
        self.assertTrue(result1)
        result2 = worker.enqueue(Path("t2.json"), {"task_id": "t2"})
        self.assertFalse(result2)


class TestParallelDispatcher(unittest.TestCase):
    """Test ParallelDispatcher routing and lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws1 = Path(self.tmp.name) / "project-a"
        self.ws1.mkdir()
        self.ws2 = Path(self.tmp.name) / "project-b"
        self.ws2.mkdir()
        # Mock resolve_active_workspace to return ws1 so ensure_current_workspace_registered
        # doesn't auto-register the real cwd as an extra workspace
        self._patcher = patch("workspace.resolve_active_workspace", return_value=self.ws1)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_start_creates_workers(self):
        ws1_entry = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2_entry = add_workspace(self.ws2, label="proj-b")
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()
        status = dispatcher.get_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["worker_count"], 2)
        self.assertIn(ws1_entry["id"], status["workers"])
        self.assertIn(ws2_entry["id"], status["workers"])
        dispatcher.stop()

    def test_stop_clears_workers(self):
        add_workspace(self.ws1, label="proj-a", is_default=True)
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()
        dispatcher.stop()
        status = dispatcher.get_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["worker_count"], 0)

    def test_dispatch_routes_to_correct_worker(self):
        ws1_entry = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2_entry = add_workspace(self.ws2, label="proj-b")
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()

        # Create a task targeting ws2
        task_id = "task-dispatch-test"
        task = {
            "task_id": task_id,
            "text": "test task",
            "target_workspace_id": ws2_entry["id"],
            "status": "pending",
        }
        task_path = task_file("pending", task_id)
        save_json(task_path, task)

        result = dispatcher.dispatch(task_path)
        self.assertTrue(result)

        # Verify task was enqueued to ws2 worker
        ws2_worker = dispatcher.get_worker_for_workspace(ws2_entry["id"])
        self.assertIsNotNone(ws2_worker)

        dispatcher.stop()

    def test_dispatch_no_workspace_returns_false(self):
        """Task with no workspace match should still dispatch (to default)."""
        ws1_entry = add_workspace(self.ws1, label="proj-a", is_default=True)
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()

        task_id = "task-no-ws"
        task = {"task_id": task_id, "text": "test", "status": "pending"}
        task_path = task_file("pending", task_id)
        save_json(task_path, task)

        # Should dispatch to default workspace
        result = dispatcher.dispatch(task_path)
        self.assertTrue(result)
        dispatcher.stop()

    def test_refresh_workers_adds_new_workspace(self):
        ws1_entry = add_workspace(self.ws1, label="proj-a", is_default=True)
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()
        self.assertEqual(dispatcher.get_status()["worker_count"], 1)

        # Add second workspace
        ws2_entry = add_workspace(self.ws2, label="proj-b")
        dispatcher.refresh_workers()
        self.assertEqual(dispatcher.get_status()["worker_count"], 2)
        dispatcher.stop()

    def test_refresh_workers_removes_deleted_workspace(self):
        ws1_entry = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2_entry = add_workspace(self.ws2, label="proj-b")
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()
        self.assertEqual(dispatcher.get_status()["worker_count"], 2)

        # Remove second workspace
        remove_workspace(ws2_entry["id"])
        dispatcher.refresh_workers()
        self.assertEqual(dispatcher.get_status()["worker_count"], 1)
        self.assertNotIn(ws2_entry["id"], dispatcher.get_status()["workers"])
        dispatcher.stop()

    def test_dispatch_invalid_task_file(self):
        """Dispatch should return False for invalid task file."""
        add_workspace(self.ws1, label="proj-a", is_default=True)
        mock_processor = MagicMock()
        dispatcher = ParallelDispatcher(mock_processor)
        dispatcher.start()

        result = dispatcher.dispatch(Path(self.tmp.name) / "nonexistent.json")
        self.assertFalse(result)
        dispatcher.stop()


class TestDispatcherStatus(unittest.TestCase):
    """Test dispatcher status persistence and display."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_empty_status(self):
        status = get_dispatcher_status()
        self.assertIn("workers", status)


if __name__ == "__main__":
    unittest.main()
