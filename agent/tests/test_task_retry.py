"""Tests for task_retry.py - Task retry/re-develop after rejection.

Covers TC-1 through TC-7 from acceptance criteria.
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

from utils import save_json, load_json, task_file, tasks_root, utc_iso  # noqa: E402
from task_state import (  # noqa: E402
    register_task_created,
    update_task_runtime,
    load_task_status,
    append_task_event,
    read_task_events,
    mark_task_finished,
)
from task_retry import (  # noqa: E402
    retry_task,
    build_retry_summary,
    get_max_retry_iterations,
    _append_rejection_history,
    _build_rejection_record,
)


def _make_rejected_task(task_id="task-test-001", task_code="T0001",
                        reason="输出格式不对", iteration_count=1,
                        rejection_history=None, extra=None):
    """Helper: create a rejected task dict in results/ stage."""
    task = {
        "task_id": task_id,
        "task_code": task_code,
        "chat_id": 123,
        "requested_by": 456,
        "action": "codex",
        "text": "实现一个计算器函数",
        "status": "rejected",
        "_stage": "results",
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
        "_git_checkpoint": "abc123",
        "executor": {
            "action": "codex",
            "elapsed_ms": 5000,
            "returncode": 0,
            "last_message": "已完成计算器函数实现",
            "workspace": "/tmp/ws",
            "git_changed_files": ["calc.py"],
        },
        "acceptance": {
            "state": "rejected",
            "acceptance_required": True,
            "archive_allowed": False,
            "gate_rule": "only_after_user_accept",
            "rejected_at": utc_iso(),
            "rejected_by": 456,
            "reason": reason,
            "iteration_count": iteration_count,
            "rejection_history": rejection_history or [],
            "updated_at": utc_iso(),
            "doc_file": "",
            "cases_file": "",
        },
    }
    if extra:
        task.update(extra)
    return task


class TestRetryTaskBasic(unittest.TestCase):
    """TC-1 & TC-2: Normal retry flow via button and command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Disable git operations for unit tests
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    def test_retry_resets_status_to_pending(self):
        """TC-1 step 3: After retry, status becomes pending."""
        task = _make_rejected_task()
        # Write task to results/
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)

        # Register task in runtime state
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)

        self.assertTrue(success)
        self.assertIn("pending", msg)
        self.assertEqual(updated["status"], "pending")
        # File should be in pending/
        pending_path = task_file("pending", task["task_id"])
        self.assertTrue(pending_path.exists())
        # File should NOT be in results/
        self.assertFalse(result_path.exists())

    def test_retry_increments_iteration_count(self):
        """TC-1 step 3: iteration_count=2 after first retry."""
        task = _make_rejected_task(iteration_count=1)
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        self.assertEqual(updated["acceptance"]["iteration_count"], 2)

    def test_retry_clears_executor_artifacts(self):
        """AC-4 constraint 6: executor_result, last_message, git_changed_files cleared."""
        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        self.assertNotIn("executor", updated)
        self.assertNotIn("completed_at", updated)
        self.assertNotIn("error", updated)

    def test_retry_preserves_rejection_history(self):
        """TC-2 step 4: rejection_history contains one record after first retry."""
        task = _make_rejected_task(reason="函数返回值类型错误")
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        history = updated["acceptance"]["rejection_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reason"], "函数返回值类型错误")
        self.assertEqual(history[0]["iteration"], 1)

    def test_retry_with_extra_instruction(self):
        """TC-2 step 3: enhanced prompt includes user supplement."""
        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(
            task, user_id=456,
            extra_instruction="请注意使用TypeScript而非JavaScript"
        )
        self.assertTrue(success)
        enhanced = updated.get("_retry_enhanced_text", "")
        self.assertIn("请注意使用TypeScript而非JavaScript", enhanced)
        self.assertIn("输出格式不对", enhanced)
        self.assertIn("实现一个计算器函数", enhanced)


class TestRetryStateValidation(unittest.TestCase):
    """TC-3: State validation - non-rejected tasks cannot be retried."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_pending_acceptance_fails(self):
        """TC-3 step 1: pending_acceptance tasks cannot be retried."""
        task = _make_rejected_task()
        task["status"] = "pending_acceptance"
        task["acceptance"]["state"] = "pending"

        success, msg, _ = retry_task(task, user_id=456)
        self.assertFalse(success)
        self.assertIn("只能对验收拒绝的任务重新开发", msg)

    def test_retry_processing_fails(self):
        """TC-3 step 2: processing tasks cannot be retried."""
        task = _make_rejected_task()
        task["status"] = "processing"

        success, msg, _ = retry_task(task, user_id=456)
        self.assertFalse(success)
        self.assertIn("只能对验收拒绝的任务重新开发", msg)

    def test_retry_accepted_fails(self):
        """TC-3 step 3: accepted (archived) tasks cannot be retried."""
        task = _make_rejected_task()
        task["status"] = "accepted"

        success, msg, _ = retry_task(task, user_id=456)
        self.assertFalse(success)
        self.assertIn("已验收通过并归档", msg)

    def test_retry_wrong_stage_fails(self):
        """Tasks not in results stage cannot be retried."""
        task = _make_rejected_task()
        task["_stage"] = "processing"

        success, msg, _ = retry_task(task, user_id=456)
        self.assertFalse(success)
        self.assertIn("不在results阶段", msg)


class TestRetryIterationLimit(unittest.TestCase):
    """TC-4: Iteration limit protection (AC-7)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    def test_max_iterations_default_3(self):
        """Default MAX_RETRY_ITERATIONS is 3."""
        os.environ.pop("MAX_RETRY_ITERATIONS", None)
        self.assertEqual(get_max_retry_iterations(), 3)

    def test_max_iterations_from_env(self):
        """MAX_RETRY_ITERATIONS can be set via env."""
        os.environ["MAX_RETRY_ITERATIONS"] = "5"
        self.assertEqual(get_max_retry_iterations(), 5)
        os.environ.pop("MAX_RETRY_ITERATIONS", None)

    def test_max_iterations_zero_uses_default(self):
        """MAX_RETRY_ITERATIONS=0 uses default (3)."""
        os.environ["MAX_RETRY_ITERATIONS"] = "0"
        self.assertEqual(get_max_retry_iterations(), 3)
        os.environ.pop("MAX_RETRY_ITERATIONS", None)

    def test_retry_blocked_at_limit(self):
        """TC-4 step 2: When iteration_count >= MAX, retry is blocked."""
        os.environ["MAX_RETRY_ITERATIONS"] = "2"
        try:
            task = _make_rejected_task(iteration_count=2)
            result_path = task_file("results", task["task_id"])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(result_path, task)

            success, msg, _ = retry_task(task, user_id=456)
            self.assertFalse(success)
            self.assertIn("已达最大迭代次数(2)", msg)
        finally:
            os.environ.pop("MAX_RETRY_ITERATIONS", None)

    def test_retry_allowed_below_limit(self):
        """TC-4: When iteration_count < MAX, retry is allowed."""
        os.environ["MAX_RETRY_ITERATIONS"] = "2"
        try:
            task = _make_rejected_task(iteration_count=1)
            result_path = task_file("results", task["task_id"])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(result_path, task)
            register_task_created(task)
            update_task_runtime(task, status="rejected", stage="results")

            success, msg, updated = retry_task(task, user_id=456)
            self.assertTrue(success)
            self.assertEqual(updated["acceptance"]["iteration_count"], 2)
        finally:
            os.environ.pop("MAX_RETRY_ITERATIONS", None)

    def test_retry_status_unchanged_when_blocked(self):
        """TC-4 step 3: When blocked, task status remains rejected."""
        os.environ["MAX_RETRY_ITERATIONS"] = "2"
        try:
            task = _make_rejected_task(iteration_count=2)
            result_path = task_file("results", task["task_id"])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(result_path, task)

            success, msg, _ = retry_task(task, user_id=456)
            self.assertFalse(success)
            # Original task file should still be in results/
            self.assertTrue(result_path.exists())
            loaded = load_json(result_path)
            self.assertEqual(loaded["status"], "rejected")
        finally:
            os.environ.pop("MAX_RETRY_ITERATIONS", None)


class TestRetryWorkspaceQueue(unittest.TestCase):
    """TC-5: Workspace queue conflict handling (AC-6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    @patch("bot_commands.answer_callback_query")
    def test_retry_enters_queue_when_workspace_busy(self, mock_acq, mock_send):
        """TC-5 step 2: When workspace has processing task, retry task enters queue."""
        from workspace_queue import should_queue_task, queue_length
        from task_state import load_runtime_state, save_runtime_state
        import bot_commands

        # Create a rejected task with workspace
        task = _make_rejected_task(extra={"target_workspace_id": "ws-test-1"})
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        # Create a processing task in the same workspace
        processing_task = {
            "task_id": "task-processing-001",
            "task_code": "T0002",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "other task",
            "status": "processing",
            "target_workspace_id": "ws-test-1",
        }
        register_task_created(processing_task)
        update_task_runtime(processing_task, status="processing", stage="processing")
        # Manually add target_workspace_id to active entry (not copied by update_task_runtime)
        state = load_runtime_state()
        state["active"]["task-processing-001"]["target_workspace_id"] = "ws-test-1"
        save_runtime_state(state)

        # Verify workspace is busy
        self.assertTrue(should_queue_task("ws-test-1"))

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            with patch("workspace_registry.get_default_workspace",
                       return_value={"id": "ws-test-1", "label": "test"}):
                bot_commands.handle_command(123, 456, "/retry T0001")

        # Check that send_text was called with queue info
        calls = mock_send.call_args_list
        found_queue_msg = any("队列" in str(c) for c in calls)
        self.assertTrue(found_queue_msg, "Should mention queue in message")


class TestRetrySummary(unittest.TestCase):
    """TC-6: Rejection reason summary correctness (AC-3)."""

    def test_summary_contains_original_description(self):
        """TC-6 step 3-1: Summary includes original task description."""
        task = _make_rejected_task(reason="函数返回值类型错误，应该返回 list 而非 dict")
        summary = build_retry_summary(task)
        self.assertIn("实现一个计算器函数", summary)

    def test_summary_contains_rejection_reason(self):
        """TC-6 step 3-2: Summary includes rejection reason."""
        task = _make_rejected_task(reason="函数返回值类型错误，应该返回 list 而非 dict")
        summary = build_retry_summary(task)
        self.assertIn("函数返回值类型错误", summary)

    def test_summary_contains_iteration_tag(self):
        """TC-6 step 3-3: Summary includes iteration tag."""
        task = _make_rejected_task(reason="test reason", iteration_count=1)
        summary = build_retry_summary(task)
        self.assertIn("第2轮重新开发", summary)

    def test_summary_with_extra_instruction(self):
        """TC-2: Summary includes user supplement."""
        task = _make_rejected_task()
        summary = build_retry_summary(task, extra_instruction="请使用TypeScript")
        self.assertIn("请使用TypeScript", summary)
        self.assertIn("用户补充说明", summary)

    def test_summary_includes_last_message(self):
        """Summary includes executor last_message preview."""
        task = _make_rejected_task()
        summary = build_retry_summary(task)
        self.assertIn("已完成计算器函数实现", summary)

    def test_summary_max_length(self):
        """Summary is limited to _SUMMARY_MAX_CHARS."""
        task = _make_rejected_task(reason="x" * 2000)
        summary = build_retry_summary(task)
        self.assertLessEqual(len(summary), 1100)  # 1000 + truncation suffix


class TestRetryHistoryAccumulation(unittest.TestCase):
    """TC-7: Multi-iteration rejection history accumulation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    def test_two_rejections_accumulate_history(self):
        """TC-7 step 2: Two rejections produce two history records."""
        # First rejection and retry
        task = _make_rejected_task(
            reason="原因A",
            iteration_count=1,
            rejection_history=[],
        )
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        self.assertEqual(len(updated["acceptance"]["rejection_history"]), 1)
        self.assertEqual(updated["acceptance"]["rejection_history"][0]["reason"], "原因A")

        # Simulate second execution → rejection
        updated["status"] = "rejected"
        updated["_stage"] = "results"
        updated["acceptance"]["state"] = "rejected"
        updated["acceptance"]["reason"] = "原因B"
        updated["acceptance"]["rejected_at"] = utc_iso()
        updated["acceptance"]["rejected_by"] = 456
        updated["executor"] = {
            "action": "codex",
            "elapsed_ms": 3000,
            "last_message": "second execution output",
        }

        # Move back to results for second retry
        pending_path = task_file("pending", task["task_id"])
        result_path2 = task_file("results", task["task_id"])
        if pending_path.exists():
            import shutil
            shutil.move(str(pending_path), str(result_path2))
        else:
            save_json(result_path2, updated)
        update_task_runtime(updated, status="rejected", stage="results")

        success2, msg2, updated2 = retry_task(updated, user_id=456)
        self.assertTrue(success2)
        history = updated2["acceptance"]["rejection_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["reason"], "原因A")
        self.assertEqual(history[0]["iteration"], 1)
        self.assertEqual(history[1]["reason"], "原因B")
        self.assertEqual(history[1]["iteration"], 2)

    def test_third_round_prompt_includes_both_reasons(self):
        """TC-7 step 3: Third round enhanced prompt includes both prior rejection reasons."""
        history = [
            {"reason": "原因A", "rejected_at": utc_iso(), "rejected_by": 456, "iteration": 1},
            {"reason": "原因B", "rejected_at": utc_iso(), "rejected_by": 456, "iteration": 2},
        ]
        task = _make_rejected_task(
            reason="原因B",
            iteration_count=2,
            rejection_history=history,
        )
        summary = build_retry_summary(task)
        self.assertIn("原因A", summary)
        self.assertIn("原因B", summary)
        self.assertIn("第3轮重新开发", summary)


class TestRetryEventLog(unittest.TestCase):
    """AC-8: Event log recording."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    def test_retry_writes_event_log(self):
        """Retry creates a task_retry event in events.jsonl."""
        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=789)
        self.assertTrue(success)

        events = read_task_events(task["task_id"], limit=50)
        retry_events = [e for e in events if e.get("event") == "task_retry"]
        self.assertGreaterEqual(len(retry_events), 1)

        evt = retry_events[-1]
        self.assertEqual(evt["data"]["iteration"], 2)
        self.assertEqual(evt["data"]["triggered_by"], 789)
        self.assertIn("输出格式不对", evt["data"]["reason_summary"])


class TestRetryGitCheckpoint(unittest.TestCase):
    """AC-5: Git checkpoint handling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    @patch("task_retry.pre_task_checkpoint")
    def test_retry_creates_new_checkpoint(self, mock_ckpt):
        """AC-5: Retry creates a new git checkpoint."""
        mock_ckpt.return_value = {
            "checkpoint_commit": "new-checkpoint-sha",
            "auto_committed": False,
            "committed_files": [],
            "error": "",
        }
        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        self.assertEqual(updated["_git_checkpoint"], "new-checkpoint-sha")
        mock_ckpt.assert_called_once()

    @patch("task_retry.pre_task_checkpoint")
    def test_retry_handles_git_error_gracefully(self, mock_ckpt):
        """AC-5: Git errors don't block retry."""
        mock_ckpt.side_effect = Exception("git not found")
        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)
        self.assertIn("Git检查点异常", msg)


class TestRetryBotCommand(unittest.TestCase):
    """Test /retry command integration in bot_commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch("task_retry.pre_task_checkpoint",
                                return_value={"checkpoint_commit": "def456", "error": ""})
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_retry_command_not_found(self, mock_send):
        """Retry with non-existent task ref."""
        import bot_commands
        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/retry NONEXIST")
        calls = mock_send.call_args_list
        found_err = any("不存在" in str(c) for c in calls)
        self.assertTrue(found_err)

    @patch("bot_commands.send_text")
    def test_retry_command_no_args(self, mock_send):
        """Retry with no arguments shows usage."""
        import bot_commands
        bot_commands.handle_command(123, 456, "/retry")
        calls = mock_send.call_args_list
        found_usage = any("用法" in str(c) for c in calls)
        self.assertTrue(found_usage)

    @patch("bot_commands.send_text")
    def test_retry_command_success(self, mock_send):
        """Retry succeeds for a rejected task."""
        import bot_commands

        task = _make_rejected_task()
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            with patch("workspace_registry.get_default_workspace",
                       return_value={"id": "ws-1", "label": "test"}):
                with patch("bot_commands.should_queue_task", return_value=False):
                    bot_commands.handle_command(123, 456, "/retry T0001")

        calls = mock_send.call_args_list
        found_success = any("重新提交" in str(c) for c in calls)
        self.assertTrue(found_success, "Should contain retry success message")


class TestRetryCallback(unittest.TestCase):
    """Test retry: callback query in handle_callback_query."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.handle_command")
    @patch("bot_commands._requires_acceptance_2fa", return_value=False)
    def test_retry_callback_calls_command(self, mock_2fa, mock_cmd, mock_acq):
        """retry: callback triggers /retry command."""
        import bot_commands
        cb = {
            "id": "cb-123",
            "data": "retry:T0001",
            "from": {"id": 456},
            "message": {"chat": {"id": 123}},
        }
        bot_commands.handle_callback_query(cb)
        mock_cmd.assert_called_once_with(123, 456, "/retry T0001")
        mock_acq.assert_called_with("cb-123", "重新开发已提交")

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    @patch("bot_commands._requires_acceptance_2fa", return_value=True)
    def test_retry_callback_with_2fa(self, mock_2fa, mock_send, mock_acq):
        """retry: callback with 2FA required sets pending action."""
        import bot_commands
        cb = {
            "id": "cb-123",
            "data": "retry:T0001",
            "from": {"id": 456},
            "message": {"chat": {"id": 123}},
        }
        bot_commands.handle_callback_query(cb)
        # Should prompt for OTP
        calls = mock_send.call_args_list
        found_otp = any("2FA" in str(c) for c in calls)
        self.assertTrue(found_otp)


class TestRetryPromptIntegration(unittest.TestCase):
    """Test that enhanced text flows through to prompt builders."""

    def test_codex_prompt_uses_enhanced_text(self):
        """build_codex_prompt uses _retry_enhanced_text when available."""
        from backends import build_codex_prompt
        task = {
            "task_id": "task-1",
            "text": "原始任务",
            "_retry_enhanced_text": "增强内容\n\n原始任务",
        }
        prompt = build_codex_prompt(task)
        self.assertIn("增强内容", prompt)

    def test_claude_prompt_uses_enhanced_text(self):
        """build_claude_prompt uses _retry_enhanced_text when available."""
        from backends import build_claude_prompt
        task = {
            "task_id": "task-1",
            "text": "原始任务",
            "_retry_enhanced_text": "增强内容\n\n原始任务",
        }
        prompt = build_claude_prompt(task)
        self.assertIn("增强内容", prompt)

    def test_codex_prompt_falls_back_to_text(self):
        """build_codex_prompt falls back to text when no _retry_enhanced_text."""
        from backends import build_codex_prompt
        task = {"task_id": "task-1", "text": "普通任务"}
        prompt = build_codex_prompt(task)
        self.assertIn("普通任务", prompt)
        self.assertNotIn("增强", prompt)


class TestRejectionHistoryHelpers(unittest.TestCase):
    """Unit tests for rejection history helper functions."""

    def test_build_rejection_record(self):
        acceptance = {
            "reason": "测试原因",
            "rejected_at": "2025-03-02T10:00:00Z",
            "rejected_by": 789,
        }
        record = _build_rejection_record(acceptance, iteration=2)
        self.assertEqual(record["reason"], "测试原因")
        self.assertEqual(record["iteration"], 2)
        self.assertEqual(record["rejected_by"], 789)

    def test_append_rejection_history_creates_list(self):
        acceptance = {"reason": "first reason", "rejected_at": utc_iso(), "rejected_by": 1}
        _append_rejection_history(acceptance, iteration=1)
        self.assertEqual(len(acceptance["rejection_history"]), 1)

    def test_append_rejection_history_appends(self):
        acceptance = {
            "reason": "second reason",
            "rejected_at": utc_iso(),
            "rejected_by": 1,
            "rejection_history": [
                {"reason": "first reason", "iteration": 1}
            ],
        }
        _append_rejection_history(acceptance, iteration=2)
        self.assertEqual(len(acceptance["rejection_history"]), 2)
        self.assertEqual(acceptance["rejection_history"][1]["reason"], "second reason")


class TestAcceptanceIterationCount(unittest.TestCase):
    """Test iteration_count in to_pending_acceptance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ["TASK_GENERATE_ACCEPTANCE_FILES"] = "0"

    def tearDown(self):
        os.environ.pop("TASK_GENERATE_ACCEPTANCE_FILES", None)
        self.tmp.cleanup()

    def test_first_acceptance_sets_iteration_1(self):
        """First execution sets iteration_count=1."""
        from task_accept import to_pending_acceptance
        task = {"task_id": "t-1", "task_code": "T0001", "text": "test"}
        result = {"status": "completed"}
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["acceptance"]["iteration_count"], 1)

    def test_retry_preserves_iteration_count(self):
        """Retry iteration_count is preserved through to_pending_acceptance."""
        from task_accept import to_pending_acceptance
        task = {"task_id": "t-1", "task_code": "T0001", "text": "test"}
        result = {
            "status": "completed",
            "acceptance": {
                "iteration_count": 3,
                "rejection_history": [{"reason": "a", "iteration": 1}],
            },
        }
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["acceptance"]["iteration_count"], 3)
        self.assertEqual(len(out["acceptance"]["rejection_history"]), 1)


class TestRejectKeyboard(unittest.TestCase):
    """AC-1: Reject response includes retry button."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    @patch("bot_commands.rollback_to_checkpoint")
    def test_reject_response_has_retry_button(self, mock_rollback, mock_send):
        """After reject, the response includes a 重新开发 button."""
        import bot_commands

        mock_rollback.return_value = {"success": True, "current_commit": "abc", "reverted_commit": "def"}

        task = _make_rejected_task()
        task["status"] = "pending_acceptance"
        task["acceptance"]["state"] = "pending"
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="pending_acceptance", stage="results")

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001 输出格式不对")

        # Find the call that has reply_markup
        calls = mock_send.call_args_list
        retry_button_found = False
        for call in calls:
            kwargs = call[1] if len(call) > 1 else {}
            if not isinstance(kwargs, dict):
                kwargs = call.kwargs if hasattr(call, 'kwargs') else {}
            markup = kwargs.get("reply_markup", {})
            if isinstance(markup, dict):
                rows = markup.get("inline_keyboard", [])
                for row in rows:
                    for btn in row:
                        if "retry:" in str(btn.get("callback_data", "")):
                            retry_button_found = True
                            self.assertEqual(btn["text"], "重新开发")
        self.assertTrue(retry_button_found, "Reject response should include retry button")


class TestRejectReasonRequired(unittest.TestCase):
    """AC: /reject must require a reason parameter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_pending_task(self, task_id="task-test-001", task_code="T0001"):
        task = _make_rejected_task(task_id=task_id, task_code=task_code)
        task["status"] = "pending_acceptance"
        task["acceptance"]["state"] = "pending"
        result_path = task_file("results", task["task_id"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="pending_acceptance", stage="results")
        return task

    @patch("bot_commands.send_text")
    def test_reject_without_reason_non2fa_rejected(self, mock_send):
        """TC-01: /reject T0001 without reason is rejected with usage prompt."""
        import bot_commands

        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001")

        text = mock_send.call_args_list[-1][0][1]
        self.assertIn("必须提供原因", text)
        self.assertIn("<原因>", text)

        # Verify task status unchanged
        st = load_task_status("task-test-001")
        self.assertEqual(st.get("status"), "pending_acceptance")

    @patch("bot_commands.send_text")
    def test_reject_whitespace_only_reason_rejected(self, mock_send):
        """TC: /reject T0001 followed by only whitespace is rejected."""
        import bot_commands

        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001   ")

        text = mock_send.call_args_list[-1][0][1]
        self.assertIn("必须提供原因", text)

    @patch("bot_commands.send_text")
    @patch("bot_commands.rollback_to_checkpoint")
    def test_reject_with_reason_succeeds(self, mock_rollback, mock_send):
        """TC-02: /reject T0001 UI样式不符合预期 works and stores reason."""
        import bot_commands

        mock_rollback.return_value = {"success": True, "current_commit": "a", "reverted_commit": "b"}
        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001 UI样式不符合预期")

        # Load the result file to check stored reason
        result_path = task_file("results", "task-test-001")
        data = load_json(result_path)
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["acceptance"]["reason"], "UI样式不符合预期")
        self.assertEqual(data["acceptance"]["state"], "rejected")
        # Reason must not be "(未提供)" or "(not provided)"
        self.assertNotEqual(data["acceptance"]["reason"], "(未提供)")
        self.assertNotEqual(data["acceptance"]["reason"], "(not provided)")

    @patch("bot_commands.send_text")
    @patch("bot_commands.rollback_to_checkpoint")
    def test_reject_multiword_reason(self, mock_rollback, mock_send):
        """TC: Multi-word reason is preserved as a single string."""
        import bot_commands

        mock_rollback.return_value = {"success": True, "current_commit": "a", "reverted_commit": "b"}
        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001 这是 一个 多词原因")

        result_path = task_file("results", "task-test-001")
        data = load_json(result_path)
        self.assertEqual(data["acceptance"]["reason"], "这是 一个 多词原因")

    @patch("bot_commands.send_text")
    @patch("bot_commands.verify_otp")
    def test_reject_2fa_without_reason_rejected(self, mock_otp, mock_send):
        """TC-03: 2FA mode /reject T0001 123456 (valid OTP, no reason) is rejected."""
        import bot_commands

        mock_otp.return_value = True
        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=True):
            bot_commands.handle_command(123, 456, "/reject T0001 123456")

        text = mock_send.call_args_list[-1][0][1]
        self.assertIn("必须提供原因", text)
        self.assertIn("<OTP> <原因>", text)

        # Verify task status unchanged
        st = load_task_status("task-test-001")
        self.assertEqual(st.get("status"), "pending_acceptance")

    @patch("bot_commands.send_text")
    @patch("bot_commands.verify_otp")
    def test_reject_2fa_no_otp_shows_usage(self, mock_otp, mock_send):
        """TC: 2FA mode /reject T0001 (no OTP) shows usage with <原因>."""
        import bot_commands

        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=True):
            bot_commands.handle_command(123, 456, "/reject T0001")

        text = mock_send.call_args_list[-1][0][1]
        self.assertIn("<OTP> <原因>", text)

    @patch("bot_commands.send_text")
    def test_reject_no_args_shows_usage(self, mock_send):
        """TC: /reject without any args shows usage with <原因>."""
        import bot_commands

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject")

        text = mock_send.call_args_list[-1][0][1]
        self.assertIn("<原因>", text)
        self.assertNotIn("[原因]", text)

    @patch("bot_commands.send_text")
    @patch("bot_commands.rollback_to_checkpoint")
    def test_reject_event_logged(self, mock_rollback, mock_send):
        """TC-04: Reject records a 'rejected' event in events.jsonl."""
        import bot_commands

        mock_rollback.return_value = {"success": True, "current_commit": "a", "reverted_commit": "b"}
        self._setup_pending_task()

        with patch("bot_commands._requires_acceptance_2fa", return_value=False):
            bot_commands.handle_command(123, 456, "/reject T0001 测试原因")

        events = read_task_events("task-test-001", limit=0)
        rejected_events = [e for e in events if e.get("event") == "rejected"]
        self.assertEqual(len(rejected_events), 1)
        evt = rejected_events[0]
        self.assertEqual(evt["data"]["status"], "rejected")
        self.assertEqual(evt["data"]["stage"], "results")
        self.assertEqual(evt["data"]["reason"], "测试原因")

    def test_help_text_uses_required_reason(self):
        """TC-05: HELP_TEXT uses <原因> not [原因] for /reject."""
        from interactive_menu import HELP_TEXT
        # Find the /reject line in help text
        for line in HELP_TEXT.splitlines():
            if "/reject" in line:
                self.assertIn("<原因>", line)
                self.assertNotIn("[原因]", line)
                break
        else:
            self.fail("/reject not found in HELP_TEXT")


if __name__ == "__main__":
    unittest.main()
