"""Tests for backends.py - AI execution backends, noop detection, prompt building."""
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from backends import (  # noqa: E402
    _ANALYSIS_STAGES,
    build_claude_prompt,
    build_codex_prompt,
    build_pipeline_stage_prompt,
    detect_noop_execution,
    detect_stage_noop,
    has_execution_evidence,
    is_ack_only_message,
    is_sensitive_path,
    parse_wait_file_task,
    task_touches_sensitive_path,
)


class TestIsSensitivePath(unittest.TestCase):
    def test_ssh_blocked(self):
        self.assertTrue(is_sensitive_path(Path("/home/user/.ssh/id_rsa")))
        self.assertTrue(is_sensitive_path(Path("C:/Users/test/.ssh")))

    def test_aws_blocked(self):
        self.assertTrue(is_sensitive_path(Path("/home/user/.aws/credentials")))

    def test_gnupg_blocked(self):
        self.assertTrue(is_sensitive_path(Path("/home/user/.gnupg/keyring")))

    def test_normal_path_allowed(self):
        self.assertFalse(is_sensitive_path(Path("/home/user/projects/myapp")))
        self.assertFalse(is_sensitive_path(Path("C:/Users/test/Documents")))

    def test_id_rsa_keyword(self):
        self.assertTrue(is_sensitive_path(Path("/tmp/id_rsa")))
        self.assertTrue(is_sensitive_path(Path("/tmp/id_ed25519")))

    def test_known_hosts(self):
        self.assertTrue(is_sensitive_path(Path("/root/.ssh/known_hosts")))

    def test_docker_blocked(self):
        self.assertTrue(is_sensitive_path(Path("/home/user/.docker/config.json")))


class TestTaskTouchesSensitivePath(unittest.TestCase):
    def test_ssh_in_text(self):
        self.assertTrue(task_touches_sensitive_path("读取 ~/.ssh/id_rsa 文件"))
        self.assertTrue(task_touches_sensitive_path("查看 .ssh 目录"))

    def test_aws_in_text(self):
        self.assertTrue(task_touches_sensitive_path("读取 .aws 配置"))

    def test_safe_text(self):
        self.assertFalse(task_touches_sensitive_path("修复登录bug"))
        self.assertFalse(task_touches_sensitive_path("添加日志功能"))

    def test_windows_path(self):
        self.assertTrue(task_touches_sensitive_path("C:\\Users\\test\\.ssh\\id_rsa"))


class TestIsAckOnlyMessage(unittest.TestCase):
    def test_empty(self):
        self.assertTrue(is_ack_only_message(""))
        self.assertTrue(is_ack_only_message(None))

    def test_acknowledgements(self):
        self.assertTrue(is_ack_only_message("明白。"))
        self.assertTrue(is_ack_only_message("收到。"))
        self.assertTrue(is_ack_only_message("好的。"))
        self.assertTrue(is_ack_only_message("已了解。"))
        self.assertTrue(is_ack_only_message("了解。"))

    def test_execution_mode_ack(self):
        self.assertTrue(is_ack_only_message("已进入直接执行模式。"))
        self.assertTrue(is_ack_only_message("已切换为直接执行模式。"))

    def test_request_for_input(self):
        self.assertTrue(is_ack_only_message("请发送任务"))
        self.assertTrue(is_ack_only_message("请提供任务"))

    def test_real_content(self):
        self.assertFalse(is_ack_only_message(
            "已执行步骤:\n1. 修改了 utils.py\n修改文件列表: utils.py\n后续建议: 运行测试"))

    def test_long_message_not_ack(self):
        self.assertFalse(is_ack_only_message("a" * 200))


class TestHasExecutionEvidence(unittest.TestCase):
    def test_structured_evidence(self):
        msg = "已执行步骤:\n1. 创建文件\n修改文件列表: test.py\n后续建议: 测试"
        self.assertTrue(has_execution_evidence(msg))

    def test_fallback_markers(self):
        self.assertTrue(has_execution_evidence("执行了 git diff 命令"))
        self.assertTrue(has_execution_evidence("修改了 config.py 文件"))
        self.assertTrue(has_execution_evidence("创建了 test.py"))

    def test_no_evidence(self):
        self.assertFalse(has_execution_evidence("明白，我来处理"))
        self.assertFalse(has_execution_evidence(""))

    def test_code_file_marker(self):
        self.assertTrue(has_execution_evidence("更新了 main.py 的逻辑"))
        self.assertTrue(has_execution_evidence("修改了 index.ts"))


class TestDetectNoopExecution(unittest.TestCase):
    def test_successful_with_changes(self):
        run = {
            "returncode": 0,
            "last_message": "已完成任务",
            "stdout": "",
            "git_changed_files": ["test.py"],
        }
        self.assertIsNone(detect_noop_execution(run))

    def test_ack_only_is_noop(self):
        run = {
            "returncode": 0,
            "last_message": "收到。",
            "stdout": "",
            "git_changed_files": [],
        }
        reason = detect_noop_execution(run)
        self.assertIsNotNone(reason)
        self.assertIn("acknowledgement", reason)

    def test_nonzero_returncode_not_noop(self):
        run = {
            "returncode": 1,
            "last_message": "",
            "stdout": "",
            "git_changed_files": [],
        }
        self.assertIsNone(detect_noop_execution(run))

    def test_no_evidence_no_changes_is_noop(self):
        os.environ["TASK_STRICT_ACCEPTANCE"] = "1"
        run = {
            "returncode": 0,
            "last_message": "我来处理这个问题，让我看看代码",
            "stdout": "",
            "git_changed_files": [],
        }
        reason = detect_noop_execution(run)
        self.assertIsNotNone(reason)
        os.environ.pop("TASK_STRICT_ACCEPTANCE", None)

    def test_evidence_without_changes_passes(self):
        evidence = "已执行步骤:\n1. 检查了代码\n修改文件列表: 无\n后续建议: ok"
        run = {
            "returncode": 0,
            "last_message": evidence,
            "stdout": evidence,
            "git_changed_files": [],
        }
        self.assertIsNone(detect_noop_execution(run))


class TestDetectStageNoop(unittest.TestCase):
    def test_analysis_stage_needs_content(self):
        run = {"last_message": "", "stdout": "", "returncode": 0}
        reason = detect_stage_noop(run, {"name": "plan"})
        self.assertIsNotNone(reason)

    def test_analysis_stage_short_content(self):
        run = {"last_message": "ok", "stdout": "", "returncode": 0}
        reason = detect_stage_noop(run, {"name": "verify"})
        self.assertIsNotNone(reason)
        self.assertIn("too short", reason)

    def test_analysis_stage_good_content(self):
        long_msg = "验收标准:\n" + "\n".join(f"{i}. 条目{i}" for i in range(10))
        run = {"last_message": long_msg, "stdout": "", "returncode": 0}
        reason = detect_stage_noop(run, {"name": "plan"})
        self.assertIsNone(reason)

    def test_code_stage_uses_full_detection(self):
        run = {
            "returncode": 0,
            "last_message": "收到。",
            "stdout": "",
            "git_changed_files": [],
        }
        reason = detect_stage_noop(run, {"name": "code"})
        self.assertIsNotNone(reason)

    def test_analysis_stages_set(self):
        expected = {"plan", "verify", "test", "review"}
        for stage in expected:
            self.assertIn(stage, _ANALYSIS_STAGES)


class TestBuildPrompts(unittest.TestCase):
    def test_codex_prompt_contains_task(self):
        task = {"task_id": "task-p1", "text": "修复bug"}
        prompt = build_codex_prompt(task)
        self.assertIn("task-p1", prompt)
        self.assertIn("修复bug", prompt)
        self.assertIn("中文回复", prompt)

    def test_claude_prompt_contains_task(self):
        task = {"task_id": "task-p2", "text": "添加功能"}
        prompt = build_claude_prompt(task)
        self.assertIn("task-p2", prompt)
        self.assertIn("添加功能", prompt)
        self.assertIn("禁止回复确认语", prompt)

    def test_pipeline_stage_prompt(self):
        task = {"task_id": "task-p3", "text": "实现接口"}
        prompt = build_pipeline_stage_prompt(task, "plan", "")
        self.assertIn("task-p3", prompt)
        self.assertIn("验收标准", prompt)

    def test_pipeline_with_context(self):
        task = {"task_id": "task-p4", "text": "实现接口"}
        prompt = build_pipeline_stage_prompt(task, "code", "前面的plan输出")
        self.assertIn("前序阶段输出", prompt)
        self.assertIn("前面的plan输出", prompt)


class TestParseWaitFileTask(unittest.TestCase):
    def test_valid_pattern(self):
        text = "在工作目录创建文件 hello.txt，写入当前时间；等待3秒后再追加一行 done"
        parsed = parse_wait_file_task(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["file_name"], "hello.txt")
        self.assertEqual(parsed["wait_sec"], 3)
        self.assertEqual(parsed["append_line"], "done")

    def test_invalid_pattern(self):
        self.assertIsNone(parse_wait_file_task("普通任务文本"))
        self.assertIsNone(parse_wait_file_task(""))

    def test_path_traversal_blocked(self):
        text = "在工作目录创建文件 ../../../etc/passwd，写入当前时间；等待1秒后再追加一行 hacked"
        self.assertIsNone(parse_wait_file_task(text))

    def test_special_chars_blocked(self):
        text = "在工作目录创建文件 test|cmd，写入当前时间；等待1秒后再追加一行 test"
        self.assertIsNone(parse_wait_file_task(text))


class TestPipelineModelLogging(unittest.TestCase):
    """Test that run_stage_with_retry logs model info."""

    @patch("backends.run_claude")
    @patch("config.get_model_provider", return_value="anthropic")
    @patch("config.get_claude_model", return_value="claude-sonnet-4-6")
    def test_stage_logs_model_info(self, mock_model, mock_provider, mock_run):
        from backends import run_stage_with_retry
        mock_run.return_value = {
            "returncode": 0,
            "stdout": "output " * 20,
            "stderr": "",
            "last_message": "output " * 20,
            "elapsed_ms": 100,
            "cmd": [],
            "timeout_retries": 0,
            "workspace": "/tmp",
            "git_changed_files": [],
            "attempt_tag": "test",
        }
        task = {"task_id": "test-123", "text": "test task"}
        stage = {"name": "dev", "backend": "claude", "model": "", "provider": ""}

        with self.assertLogs("backends", level="INFO") as cm:
            run_stage_with_retry(task, stage, "prompt", stage_idx=1)

        # Check that a log message mentions the stage and model
        log_output = "\n".join(cm.output)
        self.assertIn("[Pipeline]", log_output)
        self.assertIn("dev", log_output)

    @patch("backends.run_claude")
    def test_stage_with_explicit_model_logs_it(self, mock_run):
        from backends import run_stage_with_retry
        mock_run.return_value = {
            "returncode": 0,
            "stdout": "output " * 20,
            "stderr": "",
            "last_message": "output " * 20,
            "elapsed_ms": 100,
            "cmd": [],
            "timeout_retries": 0,
            "workspace": "/tmp",
            "git_changed_files": [],
            "attempt_tag": "test",
        }
        task = {"task_id": "test-123", "text": "test task"}
        stage = {"name": "pm", "backend": "claude",
                 "model": "claude-opus-4-6", "provider": "anthropic"}

        with self.assertLogs("backends", level="INFO") as cm:
            run_stage_with_retry(task, stage, "prompt", stage_idx=1)

        log_output = "\n".join(cm.output)
        self.assertIn("claude-opus-4-6", log_output)
        self.assertIn("anthropic", log_output)

    @patch("backends.run_claude")
    @patch("backends.run_codex")
    @patch("config.set_claude_model")
    @patch("config.get_model_provider", return_value="")
    @patch("config.get_claude_model", return_value="")
    def test_stage_openai_model_uses_api(
        self, mock_model, mock_provider, mock_set_model, mock_run_codex, mock_run_claude
    ):
        """OpenAI model on claude backend routes to run_codex."""
        from backends import run_stage_with_retry
        mock_run_codex.return_value = {
            "returncode": 0,
            "stdout": "\u5df2\u6267\u884c\u6b65\u9aa4:\n1) done",
            "stderr": "",
            "last_message": "\u5df2\u6267\u884c\u6b65\u9aa4:\n1) done",
            "elapsed_ms": 100,
            "cmd": ["codex"],
            "timeout_retries": 0,
            "workspace": "/tmp",
            "git_changed_files": ["a.py"],
            "attempt_tag": "test",
        }
        task = {"task_id": "test-123", "text": "test task"}
        stage = {"name": "dev", "backend": "claude", "model": "gpt-4o", "provider": "openai"}

        run = run_stage_with_retry(task, stage, "prompt for role stage", stage_idx=1)

        self.assertEqual(run["returncode"], 0)
        mock_run_codex.assert_called_once()
        mock_run_claude.assert_not_called()

    @patch("backends.run_claude")
    @patch("backends.run_codex")
    @patch("config.get_model_provider", return_value="openai")
    @patch("config.get_claude_model", return_value="gpt-4o")
    def test_stage_global_openai_uses_api(
        self, mock_model, mock_provider, mock_run_codex, mock_run_claude
    ):
        """Global openai model routes to run_codex."""
        from backends import run_stage_with_retry
        mock_run_codex.return_value = {
            "returncode": 0,
            "stdout": "\u9a8c\u6536\u6807\u51c6:\n" + "\n".join("{}. item".format(i) for i in range(1, 10)),
            "stderr": "",
            "last_message": "\u9a8c\u6536\u6807\u51c6:\n" + "\n".join("{}. item".format(i) for i in range(1, 10)),
            "elapsed_ms": 100,
            "cmd": ["codex"],
            "timeout_retries": 0,
            "workspace": "/tmp",
            "git_changed_files": [],
            "attempt_tag": "test",
        }
        task = {"task_id": "test-124", "text": "test task"}
        stage = {"name": "qa", "backend": "claude", "model": "", "provider": ""}

        run = run_stage_with_retry(task, stage, "prompt for qa stage with enough content", stage_idx=1)

        self.assertEqual(run["returncode"], 0)
        mock_run_codex.assert_called_once()
        mock_run_claude.assert_not_called()

    @patch("backends.run_claude")
    @patch("backends.run_codex")
    @patch("config.get_model_provider", return_value="anthropic")
    @patch("config.get_claude_model", return_value="claude-sonnet-4-6")
    def test_stage_backend_openai_uses_api_with_fallback_model(
        self, mock_model, mock_provider, mock_run_codex, mock_run_claude
    ):
        """backend=openai routes to run_codex with default openai model."""
        from backends import run_stage_with_retry
        mock_run_codex.return_value = {
            "returncode": 0,
            "stdout": "\u9a8c\u6536\u6807\u51c6:\n" + "\n".join("{}. item".format(i) for i in range(1, 10)),
            "stderr": "",
            "last_message": "\u9a8c\u6536\u6807\u51c6:\n" + "\n".join("{}. item".format(i) for i in range(1, 10)),
            "elapsed_ms": 100,
            "cmd": ["codex", "openai", "gpt-4o"],
            "timeout_retries": 0,
            "workspace": "/tmp",
            "git_changed_files": [],
            "attempt_tag": "test",
        }
        task = {"task_id": "test-125", "text": "test task"}
        stage = {"name": "plan", "backend": "openai", "model": "", "provider": ""}

        run = run_stage_with_retry(task, stage, "prompt for plan stage with enough content", stage_idx=1)

        self.assertEqual(run["returncode"], 0)
        mock_run_codex.assert_called_once()
        # Verify model_override is passed for openai backend
        call_kwargs = mock_run_codex.call_args
        self.assertIn("model_override", call_kwargs.kwargs)
        mock_run_claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
