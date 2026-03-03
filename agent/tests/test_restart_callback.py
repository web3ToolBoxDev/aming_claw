"""Tests for restart/skip_restart callback handling in bot_commands."""
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


if __name__ == "__main__":
    unittest.main()
