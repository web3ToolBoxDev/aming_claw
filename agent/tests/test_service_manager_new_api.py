"""Tests for new service_manager API: reload(), status(), reload_async()."""
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

import service_manager  # noqa: E402


class StatusTests(unittest.TestCase):
    """Tests for service_manager.status()."""

    def test_status_returns_required_keys(self):
        with patch.object(service_manager, "get_service_status", return_value={"coordinator": "running"}):
            with patch.object(service_manager, "_count_active_tasks", return_value=2):
                with patch.object(service_manager, "_count_queued_tasks", return_value=3):
                    result = service_manager.status()

        self.assertIn("pid", result)
        self.assertIn("uptime_sec", result)
        self.assertIn("active_tasks", result)
        self.assertIn("queued_tasks", result)
        self.assertIn("services", result)

    def test_status_pid_is_current_process(self):
        with patch.object(service_manager, "get_service_status", return_value={}):
            with patch.object(service_manager, "_count_active_tasks", return_value=0):
                with patch.object(service_manager, "_count_queued_tasks", return_value=0):
                    result = service_manager.status()

        self.assertEqual(result["pid"], os.getpid())

    def test_status_uptime_is_positive(self):
        with patch.object(service_manager, "get_service_status", return_value={}):
            with patch.object(service_manager, "_count_active_tasks", return_value=0):
                with patch.object(service_manager, "_count_queued_tasks", return_value=0):
                    result = service_manager.status()

        self.assertGreaterEqual(result["uptime_sec"], 0)

    def test_status_task_counts(self):
        with patch.object(service_manager, "get_service_status", return_value={}):
            with patch.object(service_manager, "_count_active_tasks", return_value=5):
                with patch.object(service_manager, "_count_queued_tasks", return_value=7):
                    result = service_manager.status()

        self.assertEqual(result["active_tasks"], 5)
        self.assertEqual(result["queued_tasks"], 7)


class ReloadTests(unittest.TestCase):
    """Tests for service_manager.reload()."""

    def test_reload_immediate_when_no_active_tasks(self):
        """When active==0, reload() calls run_restart() immediately."""
        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True) as mock_restart:
                result = service_manager.reload()

        mock_restart.assert_called_once()
        self.assertTrue(result)

    def test_reload_returns_false_on_restart_failure(self):
        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=False):
                result = service_manager.reload()

        self.assertFalse(result)

    def test_reload_waits_for_active_tasks(self):
        """reload() should wait when active > 0 and restart once drained."""
        call_count = [0]

        def fake_active():
            call_count[0] += 1
            # Return 1 on first two polls, then 0
            return 1 if call_count[0] < 3 else 0

        with patch.object(service_manager, "_count_active_tasks", side_effect=fake_active):
            with patch.object(service_manager, "run_restart", return_value=True) as mock_restart:
                with patch.object(service_manager, "RELOAD_POLL_SEC", 0.01):
                    result = service_manager.reload()

        self.assertTrue(result)
        mock_restart.assert_called_once()
        self.assertGreaterEqual(call_count[0], 3)

    def test_reload_timeout_when_tasks_never_drain(self):
        """reload() should return False when active tasks never reach 0."""
        with patch.object(service_manager, "_count_active_tasks", return_value=1):
            with patch.object(service_manager, "run_restart", return_value=True) as mock_restart:
                with patch.object(service_manager, "RELOAD_WAIT_TIMEOUT_SEC", 0):
                    with patch.object(service_manager, "RELOAD_POLL_SEC", 0.01):
                        result = service_manager.reload()

        mock_restart.assert_not_called()
        self.assertFalse(result)

    def test_reload_calls_callback_on_success(self):
        """Callback should be called with (True, message) on success."""
        received = []

        def cb(success, msg):
            received.append((success, msg))

        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True):
                service_manager.reload(callback=cb)

        self.assertEqual(len(received), 1)
        self.assertTrue(received[0][0])
        self.assertIsInstance(received[0][1], str)

    def test_reload_calls_callback_on_failure(self):
        """Callback should be called with (False, message) on restart failure."""
        received = []

        def cb(success, msg):
            received.append((success, msg))

        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=False):
                service_manager.reload(callback=cb)

        self.assertEqual(len(received), 1)
        self.assertFalse(received[0][0])

    def test_reload_calls_callback_on_timeout(self):
        """Callback should be called with (False, ...) when timeout occurs."""
        received = []

        def cb(success, msg):
            received.append((success, msg))

        with patch.object(service_manager, "_count_active_tasks", return_value=1):
            with patch.object(service_manager, "RELOAD_WAIT_TIMEOUT_SEC", 0):
                with patch.object(service_manager, "RELOAD_POLL_SEC", 0.01):
                    service_manager.reload(callback=cb)

        self.assertEqual(len(received), 1)
        self.assertFalse(received[0][0])

    def test_reload_without_callback_does_not_raise(self):
        """reload(callback=None) should work without errors."""
        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True):
                result = service_manager.reload()  # no callback
        self.assertTrue(result)

    def test_reload_callback_exception_does_not_propagate(self):
        """An exception raised inside the callback should not abort reload()."""
        def bad_cb(success, msg):
            raise RuntimeError("callback error")

        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True):
                # Should not raise
                result = service_manager.reload(callback=bad_cb)

        self.assertTrue(result)


class ReloadAsyncTests(unittest.TestCase):
    """Tests for service_manager.reload_async()."""

    def test_reload_async_returns_thread(self):
        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True):
                t = service_manager.reload_async()
                t.join(timeout=5)

        self.assertIsInstance(t, threading.Thread)
        self.assertFalse(t.is_alive())

    def test_reload_async_callback_invoked(self):
        event = threading.Event()
        received = []

        def cb(success, msg):
            received.append((success, msg))
            event.set()

        with patch.object(service_manager, "_count_active_tasks", return_value=0):
            with patch.object(service_manager, "run_restart", return_value=True):
                t = service_manager.reload_async(callback=cb)
                event.wait(timeout=5)
                t.join(timeout=5)

        self.assertTrue(received[0][0])


class CountActiveTasksTests(unittest.TestCase):
    """Tests for _count_active_tasks()."""

    def test_returns_zero_on_exception(self):
        mock_ts = MagicMock()
        mock_ts.list_active_tasks.side_effect = RuntimeError("fail")
        with patch.dict("sys.modules", {"task_state": mock_ts}):
            result = service_manager._count_active_tasks()
        self.assertEqual(result, 0)

    def test_counts_only_processing_tasks(self):
        mock_ts = MagicMock()
        mock_ts.list_active_tasks.return_value = [
            {"status": "processing"},
            {"status": "processing"},
            {"status": "pending_acceptance"},
        ]
        with patch.dict("sys.modules", {"task_state": mock_ts}):
            result = service_manager._count_active_tasks()
        self.assertEqual(result, 2)


class CountQueuedTasksTests(unittest.TestCase):
    """Tests for _count_queued_tasks()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_zero_when_no_queue_file(self):
        result = service_manager._count_queued_tasks()
        self.assertEqual(result, 0)

    def test_counts_across_workspaces(self):
        from utils import save_json, tasks_root
        queue_path = tasks_root() / "state" / "workspace_task_queue.json"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(queue_path, {
            "version": 1,
            "queues": {
                "ws1": [{"task_id": "t1"}, {"task_id": "t2"}],
                "ws2": [{"task_id": "t3"}],
            },
        })
        result = service_manager._count_queued_tasks()
        self.assertEqual(result, 3)


if __name__ == "__main__":
    unittest.main()
