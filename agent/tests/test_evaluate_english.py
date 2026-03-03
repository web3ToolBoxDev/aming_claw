"""Tests for _evaluate_english_text: model param, claude_bin resolution, timeout, JSON parse."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


class TestEvaluateEnglishTextWithModel(unittest.TestCase):
    """When get_claude_model returns a model, cmd includes --model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CLAUDE_BIN", None)
        self.tmp.cleanup()

    @patch("backends._base_claude_cmd")
    @patch("bot_commands.subprocess.run")
    def test_with_model_configured(self, mock_run, mock_base_cmd):
        """_base_claude_cmd is called and its result used as the command base."""
        mock_base_cmd.return_value = ["/usr/bin/claude", "-p", "--model", "sonnet"]
        ai_response = json.dumps({
            "original": "Add button",
            "corrected": "Add a button",
            "issues": [{"type": "grammar", "original": "Add button",
                        "suggestion": "Add a button", "explanation": "Missing article"}],
            "chinese_meaning": "添加一个按钮",
        })
        mock_run.return_value = MagicMock(stdout=ai_response, returncode=0)

        from bot_commands import _evaluate_english_text
        result = _evaluate_english_text("Add button")

        self.assertIsNotNone(result)
        self.assertEqual(result["corrected"], "Add a button")
        # Verify _base_claude_cmd was called
        mock_base_cmd.assert_called_once()
        # Verify subprocess.run received cmd from _base_claude_cmd (no prompt appended)
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd, ["/usr/bin/claude", "-p", "--model", "sonnet"])
        # Verify prompt passed via stdin (input= parameter)
        called_kwargs = mock_run.call_args[1]
        self.assertIn("input", called_kwargs)
        self.assertIn("Add button", called_kwargs["input"])


class TestEvaluateEnglishTextNoModel(unittest.TestCase):
    """When no model is configured, cmd does NOT include --model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CLAUDE_BIN", None)
        os.environ.pop("CLAUDE_MODEL", None)
        self.tmp.cleanup()

    @patch("backends._base_claude_cmd")
    @patch("bot_commands.subprocess.run")
    def test_no_model_configured(self, mock_run, mock_base_cmd):
        """Without model, _base_claude_cmd returns cmd without --model."""
        mock_base_cmd.return_value = ["/usr/bin/claude", "-p"]
        ai_response = json.dumps({
            "original": "Fix bug",
            "corrected": "Fix the bug",
            "issues": [],
            "chinese_meaning": "修复这个缺陷",
        })
        mock_run.return_value = MagicMock(stdout=ai_response, returncode=0)

        from bot_commands import _evaluate_english_text
        result = _evaluate_english_text("Fix bug")

        self.assertIsNotNone(result)
        called_cmd = mock_run.call_args[0][0]
        self.assertNotIn("--model", called_cmd)
        # Verify prompt passed via stdin
        called_kwargs = mock_run.call_args[1]
        self.assertIn("input", called_kwargs)
        self.assertIn("Fix bug", called_kwargs["input"])


class TestEvaluateEnglishTextTimeout(unittest.TestCase):
    """subprocess.TimeoutExpired returns None and logs warning."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("backends._base_claude_cmd", return_value=["claude", "-p"])
    @patch("bot_commands.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60))
    def test_timeout_returns_none(self, mock_run, mock_base_cmd):
        from bot_commands import _evaluate_english_text
        with self.assertLogs("bot_commands", level="WARNING") as cm:
            result = _evaluate_english_text("Some text")
        self.assertIsNone(result)
        self.assertTrue(any("[eng_eval] failed" in msg for msg in cm.output))


class TestEvaluateEnglishTextBadJson(unittest.TestCase):
    """Non-JSON response returns None and logs warning."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("backends._base_claude_cmd", return_value=["claude", "-p"])
    @patch("bot_commands.subprocess.run")
    def test_non_json_response_returns_none(self, mock_run, mock_base_cmd):
        mock_run.return_value = MagicMock(stdout="This is not valid JSON at all",
                                          stderr="", returncode=0)

        from bot_commands import _evaluate_english_text
        with self.assertLogs("bot_commands", level="WARNING") as cm:
            result = _evaluate_english_text("Hello world")
        self.assertIsNone(result)
        self.assertTrue(any("[eng_eval] failed" in msg for msg in cm.output))


class TestEvaluateEnglishEnvFiltering(unittest.TestCase):
    """Verify _evaluate_english_text strips interfering env vars."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        os.environ.pop("CLAUDE_CODE_SSE_PORT", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.tmp.cleanup()

    @patch("backends._base_claude_cmd")
    @patch("bot_commands.subprocess.run")
    def test_env_vars_filtered(self, mock_run, mock_base_cmd):
        """CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_SSE_PORT, ANTHROPIC_API_KEY are stripped."""
        mock_base_cmd.return_value = ["claude", "-p"]
        # Set interfering env vars
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "test"
        os.environ["CLAUDE_CODE_SSE_PORT"] = "12345"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"

        ai_response = json.dumps({
            "original": "Test text",
            "corrected": "Test text",
            "issues": [],
            "chinese_meaning": "测试文本",
        })
        mock_run.return_value = MagicMock(stdout=ai_response, returncode=0)

        from bot_commands import _evaluate_english_text
        _evaluate_english_text("Test text")

        # Verify env= was passed and does not contain the filtered vars
        called_kwargs = mock_run.call_args[1]
        self.assertIn("env", called_kwargs)
        passed_env = called_kwargs["env"]
        for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY"):
            self.assertNotIn(var, passed_env,
                             f"env should not contain {var}")


class TestBaseClaudeCmd(unittest.TestCase):
    """Tests for _base_claude_cmd helper in backends.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CLAUDE_BIN", None)
        os.environ.pop("CLAUDE_MODEL", None)
        self.tmp.cleanup()

    @patch("config.get_claude_model", return_value="opus")
    def test_with_model_and_claude_bin_env(self, mock_model):
        os.environ["CLAUDE_BIN"] = "/custom/claude"
        from backends import _base_claude_cmd
        cmd = _base_claude_cmd()
        self.assertEqual(cmd, ["/custom/claude", "-p", "--model", "opus"])

    @patch("config.get_claude_model", return_value="")
    def test_no_model(self, mock_model):
        os.environ.pop("CLAUDE_MODEL", None)
        os.environ["CLAUDE_BIN"] = "/custom/claude"
        from backends import _base_claude_cmd
        cmd = _base_claude_cmd()
        self.assertEqual(cmd, ["/custom/claude", "-p"])

    @patch("config.get_claude_model", return_value="sonnet")
    def test_extra_flags(self, mock_model):
        os.environ["CLAUDE_BIN"] = "/custom/claude"
        from backends import _base_claude_cmd
        cmd = _base_claude_cmd(extra_flags=["--output-format", "json"])
        self.assertEqual(cmd, ["/custom/claude", "-p", "--model", "sonnet",
                               "--output-format", "json"])


if __name__ == "__main__":
    unittest.main()
