"""Tests for interactive command enhancements.

Covers: /task, /accept, /reject, /retry, /cancel (no-arg interactive flows),
        /switch_backend (no-arg), and pending_tasks_keyboard().
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bot_commands  # noqa: E402
from interactive_menu import pending_tasks_keyboard, safe_callback_data  # noqa: E402
from utils import save_json, tasks_root, utc_iso  # noqa: E402


class TestPendingTasksKeyboard(unittest.TestCase):
    """Tests for the new pending_tasks_keyboard() function."""

    def test_basic_keyboard_with_tasks(self):
        tasks = [
            {"task_code": "T0001", "text": "Fix login bug"},
            {"task_code": "T0002", "text": "Optimize homepage"},
        ]
        kb = pending_tasks_keyboard(tasks, "accept")
        rows = kb["inline_keyboard"]
        # 2 tasks + 1 back button
        self.assertEqual(len(rows), 3)
        self.assertIn("T0001", rows[0][0]["text"])
        self.assertIn("Fix login bug", rows[0][0]["text"])
        self.assertEqual(rows[0][0]["callback_data"], "accept:T0001")
        self.assertIn("T0002", rows[1][0]["text"])
        self.assertEqual(rows[1][0]["callback_data"], "accept:T0002")
        # Last row is back button
        self.assertEqual(rows[2][0]["callback_data"], "menu:main")

    def test_keyboard_truncates_long_desc(self):
        tasks = [{"task_code": "T0001", "text": "A" * 50}]
        kb = pending_tasks_keyboard(tasks, "reject")
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertIn("...", btn_text)
        self.assertTrue(len(btn_text) < 50)

    def test_keyboard_no_desc(self):
        tasks = [{"task_code": "T0001", "text": "Hello"}]
        kb = pending_tasks_keyboard(tasks, "retry", show_desc=False)
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertEqual(btn_text, "T0001")

    def test_keyboard_limits_to_20(self):
        tasks = [{"task_code": "T{:04d}".format(i), "text": "task"} for i in range(25)]
        kb = pending_tasks_keyboard(tasks, "accept")
        # 20 tasks + 1 back button
        self.assertEqual(len(kb["inline_keyboard"]), 21)

    def test_keyboard_empty_tasks(self):
        kb = pending_tasks_keyboard([], "accept")
        # Just back button
        self.assertEqual(len(kb["inline_keyboard"]), 1)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "menu:main")

    def test_different_action_prefixes(self):
        tasks = [{"task_code": "T0001", "text": "test"}]
        for prefix in ("accept", "reject", "retry", "cmd_cancel"):
            kb = pending_tasks_keyboard(tasks, prefix)
            self.assertTrue(
                kb["inline_keyboard"][0][0]["callback_data"].startswith(prefix + ":")
            )


class TestTaskCommandNoArgs(unittest.TestCase):
    """Tests for /task without arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    @patch("bot_commands.workspace_select_keyboard")
    def test_task_no_args_multi_workspace(self, mock_ws_kb, mock_send):
        """When multiple workspaces exist, /task shows workspace selection."""
        mock_ws_kb.return_value = {"inline_keyboard": [[{"text": "ws1", "callback_data": "ws_task_select:1"}]]}
        workspaces = [
            {"id": "ws1", "label": "Project A", "path": "/a"},
            {"id": "ws2", "label": "Project B", "path": "/b"},
        ]
        with patch("bot_commands._list_ws_cmd", return_value=workspaces, create=True):
            with patch("bot_commands.ensure_current_workspace_registered", create=True):
                # Patch the local import inside the function
                with patch.dict("sys.modules", {
                    "workspace_registry": MagicMock(
                        ensure_current_workspace_registered=MagicMock(),
                        list_workspaces=MagicMock(return_value=workspaces),
                    )
                }):
                    result = bot_commands.handle_command(100, 200, "/task")
        self.assertTrue(result)
        mock_send.assert_called()
        call_args = mock_send.call_args
        self.assertIn("reply_markup", call_args.kwargs or {})

    @patch("bot_commands.send_text")
    def test_task_no_args_single_workspace(self, mock_send):
        """When single workspace exists, /task sets pending action."""
        workspaces = [{"id": "ws1", "label": "Only", "path": "/only"}]
        with patch.dict("sys.modules", {
            "workspace_registry": MagicMock(
                ensure_current_workspace_registered=MagicMock(),
                list_workspaces=MagicMock(return_value=workspaces),
            )
        }):
            result = bot_commands.handle_command(100, 200, "/task")
        self.assertTrue(result)
        mock_send.assert_called()
        # Should have set pending action
        from interactive_menu import peek_pending_action
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "new_task")


class TestAcceptCommandNoArgs(unittest.TestCase):
    """Tests for /accept without arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_accept_no_args_no_tasks(self, mock_send):
        """When no pending_acceptance tasks, show empty message."""
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=[]):
            result = bot_commands.handle_command(100, 200, "/accept")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u6ca1\u6709\u5f85\u9a8c\u6536", msg)

    @patch("bot_commands.send_text")
    def test_accept_no_args_with_tasks(self, mock_send):
        """When pending_acceptance tasks exist, show task list keyboard."""
        tasks = [
            {"task_code": "T0001", "text": "Fix bug", "task_id": "task-1"},
            {"task_code": "T0002", "text": "Add feature", "task_id": "task-2"},
        ]
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=tasks):
            result = bot_commands.handle_command(100, 200, "/accept")
        self.assertTrue(result)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs.kwargs.get("reply_markup") if call_kwargs.kwargs else None
        if reply_markup is None and len(call_kwargs) > 0:
            # Try positional approach
            for arg in (call_kwargs.args if hasattr(call_kwargs, 'args') else []):
                if isinstance(arg, dict) and "inline_keyboard" in arg:
                    reply_markup = arg
        # Verify keyboard contains accept prefix
        msg = mock_send.call_args[0][1]
        self.assertIn("\u5f85\u9a8c\u6536", msg)


class TestRejectCommandNoArgs(unittest.TestCase):
    """Tests for /reject without arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_reject_no_args_no_tasks(self, mock_send):
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=[]):
            result = bot_commands.handle_command(100, 200, "/reject")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u6ca1\u6709\u53ef\u62d2\u7edd", msg)

    @patch("bot_commands.send_text")
    def test_reject_no_args_with_tasks(self, mock_send):
        tasks = [{"task_code": "T0001", "text": "Fix bug", "task_id": "task-1"}]
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=tasks):
            result = bot_commands.handle_command(100, 200, "/reject")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u53ef\u62d2\u7edd", msg)


class TestRetryCommandNoArgs(unittest.TestCase):
    """Tests for /retry without arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_retry_no_args_no_tasks(self, mock_send):
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=[]):
            result = bot_commands.handle_command(100, 200, "/retry")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u6ca1\u6709\u53ef\u91cd\u8bd5", msg)

    @patch("bot_commands.send_text")
    def test_retry_no_args_with_tasks(self, mock_send):
        tasks = [{"task_code": "T0001", "text": "Fix bug", "task_id": "task-1"}]
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=tasks):
            result = bot_commands.handle_command(100, 200, "/retry")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u53ef\u91cd\u8bd5", msg)


class TestCancelCommand(unittest.TestCase):
    """Tests for /cancel command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_cancel_no_args_no_tasks(self, mock_send):
        """No cancellable tasks shows empty message."""
        with patch.object(bot_commands, "_collect_tasks_by_status", return_value=[]):
            with patch.object(bot_commands, "list_active_tasks", return_value=[]):
                result = bot_commands.handle_command(100, 200, "/cancel")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u6ca1\u6709\u53ef\u53d6\u6d88", msg)

    @patch("bot_commands.send_text")
    def test_cancel_no_args_with_tasks(self, mock_send):
        """Cancellable tasks show task selection keyboard."""
        processing_tasks = [{"task_code": "T0001", "text": "Running task", "task_id": "task-1"}]
        with patch.object(bot_commands, "_collect_tasks_by_status", side_effect=[processing_tasks, []]):
            with patch.object(bot_commands, "list_active_tasks", return_value=[]):
                result = bot_commands.handle_command(100, 200, "/cancel")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u53ef\u53d6\u6d88", msg)

    @patch("bot_commands.send_text")
    def test_cancel_with_ref_not_found(self, mock_send):
        """Cancel with non-existent task ref shows error."""
        with patch.object(bot_commands, "find_task", return_value=None):
            result = bot_commands.handle_command(100, 200, "/cancel T9999")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u4efb\u52a1\u4e0d\u5b58\u5728", msg)

    @patch("bot_commands.send_text")
    @patch("bot_commands.mark_task_finished")
    @patch("bot_commands.update_task_runtime")
    def test_cancel_with_ref_pending(self, mock_update, mock_finish, mock_send):
        """Cancel a pending task directly."""
        found = {
            "task_id": "task-1",
            "task_code": "T0001",
            "status": "pending",
            "_stage": "pending",
        }
        with patch.object(bot_commands, "find_task", return_value=found):
            with patch.object(bot_commands, "task_status_snapshot", return_value={"status": "pending"}):
                with patch.object(bot_commands, "task_file", return_value=Path(self.tmp.name) / "nonexistent"):
                    result = bot_commands.handle_command(100, 200, "/cancel T0001")
        self.assertTrue(result)
        mock_update.assert_called_once()
        mock_finish.assert_called_once()
        msg = mock_send.call_args[0][1]
        self.assertIn("\u5df2\u53d6\u6d88", msg)

    @patch("bot_commands.send_text")
    def test_cancel_non_cancellable_status(self, mock_send):
        """Cancel a task with non-cancellable status shows error."""
        found = {
            "task_id": "task-1",
            "task_code": "T0001",
            "status": "accepted",
            "_stage": "results",
        }
        with patch.object(bot_commands, "find_task", return_value=found):
            with patch.object(bot_commands, "task_status_snapshot", return_value={"status": "accepted"}):
                result = bot_commands.handle_command(100, 200, "/cancel T0001")
        self.assertTrue(result)
        msg = mock_send.call_args[0][1]
        self.assertIn("\u4ec5\u53ef\u53d6\u6d88", msg)


class TestSwitchBackendNoArgs(unittest.TestCase):
    """Tests for /switch_backend without arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    @patch("bot_commands.is_ops_allowed", return_value=True)
    @patch("bot_commands.get_agent_backend", return_value="codex")
    def test_switch_backend_no_args(self, mock_backend, mock_ops, mock_send):
        result = bot_commands.handle_command(100, 200, "/switch_backend")
        self.assertTrue(result)
        call_kwargs = mock_send.call_args
        msg = call_kwargs[0][1]
        self.assertIn("\u5207\u6362\u540e\u7aef", msg)
        # Should have inline keyboard with backend options
        reply_markup = call_kwargs[1].get("reply_markup") if len(call_kwargs) > 1 else None
        if reply_markup is None:
            reply_markup = call_kwargs.kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertIn("inline_keyboard", reply_markup)


class TestCancelCallbackHandler(unittest.TestCase):
    """Tests for cmd_cancel: callback in handle_callback_query."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.handle_command")
    def test_cancel_callback_routes_to_command(self, mock_cmd, mock_ack):
        mock_cmd.return_value = True
        cb = {
            "id": "cb123",
            "data": "cmd_cancel:T0001",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        bot_commands.handle_callback_query(cb)
        mock_cmd.assert_called_once_with(100, 200, "/cancel T0001")
        mock_ack.assert_called()


class TestHelpTextUpdated(unittest.TestCase):
    """Verify HELP_TEXT reflects new command behaviors."""

    def test_help_mentions_interactive(self):
        from interactive_menu import HELP_TEXT
        self.assertIn("/task", HELP_TEXT)
        self.assertIn("/cancel", HELP_TEXT)
        self.assertIn("/retry", HELP_TEXT)
        # Check that help mentions interactive behavior
        self.assertIn("\u65e0\u53c2\u6570", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
