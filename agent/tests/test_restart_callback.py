"""Tests for restart/skip_restart callback handling in bot_commands."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bot_commands  # noqa: E402
from utils import save_json, tasks_root, utc_iso  # noqa: E402


class RestartCallbackTests(unittest.TestCase):
    """Tests for restart: and skip_restart: callback routes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.is_ops_allowed", return_value=True)
    def test_restart_callback_calls_run_restart(self, mock_ops, mock_answer, mock_send):
        """Clicking restart button should call service_manager.run_restart."""
        mock_sm = MagicMock(run_restart=MagicMock(return_value=True))
        with patch.dict("sys.modules", {"service_manager": mock_sm}):
            cb = {
                "id": "cb123",
                "data": "restart:T42",
                "message": {"chat": {"id": 100}},
                "from": {"id": 200},
            }
            bot_commands.handle_callback_query(cb)

        mock_answer.assert_called_once_with("cb123", "正在重启服务...")
        mock_sm.run_restart.assert_called_once()
        self.assertTrue(mock_send.called)
        call_text = mock_send.call_args[0][1]
        self.assertIn("T42", call_text)
        self.assertIn("重启已执行", call_text)

    @patch("bot_commands.send_text")
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.is_ops_allowed", return_value=True)
    def test_restart_callback_failure(self, mock_ops, mock_answer, mock_send):
        """When run_restart returns False, show failure message."""
        with patch.dict("sys.modules", {"service_manager": MagicMock(run_restart=MagicMock(return_value=False))}):
            cb = {
                "id": "cb456",
                "data": "restart:T99",
                "message": {"chat": {"id": 100}},
                "from": {"id": 200},
            }
            bot_commands.handle_callback_query(cb)

        call_text = mock_send.call_args[0][1]
        self.assertIn("重启失败", call_text)

    @patch("bot_commands.send_text")
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.is_ops_allowed", return_value=False)
    def test_restart_callback_unauthorized(self, mock_ops, mock_answer, mock_send):
        """Unauthorized user should get 'no permission' alert."""
        cb = {
            "id": "cb789",
            "data": "restart:T1",
            "message": {"chat": {"id": 100}},
            "from": {"id": 300},
        }
        bot_commands.handle_callback_query(cb)

        mock_answer.assert_called_once_with("cb789", "无权限", show_alert=True)
        mock_send.assert_not_called()

    @patch("bot_commands.send_text")
    @patch("bot_commands.answer_callback_query")
    def test_skip_restart_callback(self, mock_answer, mock_send):
        """Clicking skip restart should send confirmation message."""
        cb = {
            "id": "cb_skip",
            "data": "skip_restart:T42",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        bot_commands.handle_callback_query(cb)

        mock_answer.assert_called_once_with("cb_skip", "已跳过重启")
        mock_send.assert_called_once()
        call_text = mock_send.call_args[0][1]
        self.assertIn("跳过重启", call_text)
        self.assertIn("T42", call_text)


class AcceptMessageRestartButtonTests(unittest.TestCase):
    """Tests for restart button presence/absence in acceptance message."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create a task in results stage with pending_acceptance status
        self.task = {
            "task_id": "task-restart-test",
            "task_code": "TR01",
            "chat_id": 100,
            "requested_by": 200,
            "action": "codex",
            "text": "test task",
            "status": "pending_acceptance",
            "_stage": "results",
            "created_at": utc_iso(),
            "updated_at": utc_iso(),
            "acceptance": {"state": "pending", "acceptance_required": True, "archive_allowed": False},
            "executor": {"elapsed_ms": 100, "last_message": "done"},
        }
        results_dir = tasks_root() / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        save_json(results_dir / "task-restart-test.json", self.task)
        # Register in active tasks
        from task_state import register_task_created, update_task_runtime
        register_task_created(self.task)
        update_task_runtime(self.task, status="pending_acceptance", stage="results")

    def tearDown(self):
        self.tmp.cleanup()

    @patch("bot_commands._auto_launch_queued_task")
    @patch("bot_commands.archive_task_result", return_value={"archive_id": "arc-1"})
    @patch("bot_commands.commit_after_acceptance", return_value={
        "success": True, "commit_sha": "abc1234",
        "committed_files": ["agent/executor.py"], "needs_restart": True, "error": "",
    })
    @patch("bot_commands.run_post_acceptance_tests", return_value={"skipped": True})
    @patch("bot_commands._requires_acceptance_2fa", return_value=False)
    @patch("bot_commands.send_text")
    def test_accept_with_restart_needed_shows_buttons(
        self, mock_send, mock_2fa, mock_test, mock_commit, mock_archive, mock_launch
    ):
        """When needs_restart=True, acceptance message should include restart buttons."""
        bot_commands.handle_command(100, 200, "/accept TR01")

        # Find the acceptance message call (contains task_accepted text)
        accept_calls = [c for c in mock_send.call_args_list if "TR01" in str(c)]
        self.assertTrue(len(accept_calls) > 0, "Expected at least one send_text with TR01")

        # The acceptance call should have reply_markup with restart buttons
        found_restart_kb = False
        for call in mock_send.call_args_list:
            kwargs = call[1] if len(call) > 1 else {}
            markup = kwargs.get("reply_markup")
            if markup and "inline_keyboard" in (markup if isinstance(markup, dict) else {}):
                buttons = markup["inline_keyboard"][0]
                button_data = [b["callback_data"] for b in buttons]
                self.assertTrue(any("restart:" in d for d in button_data),
                                "Expected restart: callback_data in buttons")
                self.assertTrue(any("skip_restart:" in d for d in button_data),
                                "Expected skip_restart: callback_data in buttons")
                found_restart_kb = True
                # Verify message text contains core_module_changed hint
                msg_text = call[0][1]
                self.assertIn("核心模块", msg_text)
                break
        self.assertTrue(found_restart_kb, "Expected inline keyboard with restart buttons")

    @patch("bot_commands._auto_launch_queued_task")
    @patch("bot_commands.archive_task_result", return_value={"archive_id": "arc-2"})
    @patch("bot_commands.commit_after_acceptance", return_value={
        "success": True, "commit_sha": "def5678",
        "committed_files": ["README.md"], "needs_restart": False, "error": "",
    })
    @patch("bot_commands.run_post_acceptance_tests", return_value={"skipped": True})
    @patch("bot_commands._requires_acceptance_2fa", return_value=False)
    @patch("bot_commands.send_text")
    def test_accept_without_restart_no_buttons(
        self, mock_send, mock_2fa, mock_test, mock_commit, mock_archive, mock_launch
    ):
        """When needs_restart=False, acceptance message should NOT include restart buttons."""
        bot_commands.handle_command(100, 200, "/accept TR01")

        # No call should have reply_markup with inline_keyboard
        for call in mock_send.call_args_list:
            kwargs = call[1] if len(call) > 1 else {}
            markup = kwargs.get("reply_markup")
            if markup and isinstance(markup, dict) and "inline_keyboard" in markup:
                buttons = markup["inline_keyboard"][0]
                button_data = [b.get("callback_data", "") for b in buttons]
                self.assertFalse(
                    any("restart:" in d for d in button_data),
                    "Should NOT have restart buttons when needs_restart=False",
                )


if __name__ == "__main__":
    unittest.main()
