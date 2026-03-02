"""Tests for task management menu system (sub-menus, filtering, detail pages)."""
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

from interactive_menu import (  # noqa: E402
    TASK_STATUS_LABELS,
    TASK_STATUS_EMPTY_LABELS,
    SUBMENU_TEXTS,
    archive_detail_keyboard,
    main_menu_keyboard,
    task_detail_keyboard,
    task_mgmt_menu_keyboard,
    task_status_list_keyboard,
    tasks_overview_keyboard,
)


class TestTaskMgmtMenuKeyboard(unittest.TestCase):
    """Tests for task_mgmt_menu_keyboard."""

    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        rows = kb["inline_keyboard"]
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIsInstance(row, list)
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_keyboard_valid(self):
        kb = task_mgmt_menu_keyboard()
        self._assert_valid_keyboard(kb)

    def test_has_all_status_buttons(self):
        kb = task_mgmt_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        expected = [
            "menu:tasks_pending",
            "menu:tasks_processing",
            "menu:tasks_pending_acceptance",
            "menu:tasks_rejected",
            "menu:tasks_accepted",
            "menu:tasks_failed",
            "menu:tasks_archived",
            "menu:tasks_overview",
            "menu:main",
        ]
        for cb in expected:
            self.assertIn(cb, all_data, "Missing callback: {}".format(cb))

    def test_has_9_buttons(self):
        """8 status categories + 1 back button = 9 total buttons."""
        kb = task_mgmt_menu_keyboard()
        all_btns = [btn for row in kb["inline_keyboard"] for btn in row]
        self.assertEqual(len(all_btns), 9)


class TestMainMenuHasTaskMgmt(unittest.TestCase):
    """Test that main menu has task management entry."""

    def test_main_menu_has_sub_task_mgmt(self):
        kb = main_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("menu:sub_task_mgmt", all_data)

    def test_main_menu_no_old_task_list(self):
        """Old menu:task_list should be replaced."""
        kb = main_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertNotIn("menu:task_list", all_data)


class TestTaskStatusListKeyboard(unittest.TestCase):
    """Tests for task_status_list_keyboard with pagination."""

    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def _make_tasks(self, count):
        return [
            {"task_code": "T{:04d}".format(i + 1), "text": "Task {} description".format(i + 1)}
            for i in range(count)
        ]

    def test_empty_list(self):
        kb = task_status_list_keyboard([], "pending")
        self._assert_valid_keyboard(kb)
        # Only back button
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("menu:sub_task_mgmt", all_data)

    def test_single_task(self):
        tasks = self._make_tasks(1)
        kb = task_status_list_keyboard(tasks, "pending")
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("task_detail:T0001", all_data)
        # No pagination
        self.assertFalse(any("tasks_page:" in d for d in all_data))

    def test_exactly_5_tasks_no_pagination(self):
        tasks = self._make_tasks(5)
        kb = task_status_list_keyboard(tasks, "processing")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertEqual(len([d for d in all_data if d.startswith("task_detail:")]), 5)
        self.assertFalse(any("tasks_page:" in d for d in all_data))

    def test_6_tasks_has_pagination(self):
        tasks = self._make_tasks(6)
        kb = task_status_list_keyboard(tasks, "rejected", page=0)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        # First page: 5 tasks + next page button
        self.assertEqual(len([d for d in all_data if d.startswith("task_detail:")]), 5)
        self.assertIn("tasks_page:rejected:1", all_data)
        # No prev page on page 0
        self.assertFalse(any("tasks_page:rejected:-1" in d for d in all_data))

    def test_second_page(self):
        tasks = self._make_tasks(8)
        kb = task_status_list_keyboard(tasks, "pending", page=1)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        # Second page: 3 tasks
        self.assertEqual(len([d for d in all_data if d.startswith("task_detail:")]), 3)
        # Has prev page
        self.assertIn("tasks_page:pending:0", all_data)
        # No next page (only 8 items, page 1 covers 5-7)
        self.assertFalse(any("tasks_page:pending:2" in d for d in all_data))

    def test_archived_uses_archive_detail_callback(self):
        tasks = [{"task_code": "T0001", "text": "archived task", "archive_id": "arc-test-123"}]
        kb = task_status_list_keyboard(tasks, "archived")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("archive_detail:arc-test-123", all_data)
        self.assertFalse(any(d.startswith("task_detail:") for d in all_data))

    def test_text_truncation(self):
        tasks = [{"task_code": "T0001", "text": "A" * 100}]
        kb = task_status_list_keyboard(tasks, "pending")
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertIn("...", btn_text)
        self.assertLessEqual(len(btn_text), 35)  # [T0001] + 20 chars + ...

    def test_back_button_goes_to_task_mgmt(self):
        kb = task_status_list_keyboard([], "failed")
        last_row = kb["inline_keyboard"][-1]
        self.assertEqual(last_row[0]["callback_data"], "menu:sub_task_mgmt")


class TestTaskDetailKeyboard(unittest.TestCase):
    """Tests for task_detail_keyboard with status-specific actions."""

    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def _get_all_data(self, kb):
        return [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]

    def test_pending_actions(self):
        kb = task_detail_keyboard("T0001", "pending")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("task_doc:T0001", data)
        self.assertIn("task_cancel:T0001", data)
        self.assertIn("menu:tasks_pending", data)

    def test_processing_actions(self):
        kb = task_detail_keyboard("T0002", "processing")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("status:T0002", data)
        self.assertIn("events:T0002", data)
        self.assertIn("menu:tasks_processing", data)

    def test_pending_acceptance_actions(self):
        kb = task_detail_keyboard("T0003", "pending_acceptance")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("accept:T0003", data)
        self.assertIn("reject:T0003", data)
        self.assertIn("task_doc:T0003", data)
        self.assertIn("events:T0003", data)
        self.assertIn("menu:tasks_pending_acceptance", data)

    def test_rejected_actions(self):
        kb = task_detail_keyboard("T0004", "rejected")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("retry:T0004", data)
        self.assertIn("accept:T0004", data)
        self.assertIn("task_doc:T0004", data)
        self.assertIn("task_delete:T0004", data)
        self.assertIn("menu:tasks_rejected", data)

    def test_accepted_actions(self):
        kb = task_detail_keyboard("T0005", "accepted")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("task_doc:T0005", data)
        self.assertIn("events:T0005", data)
        self.assertIn("menu:tasks_accepted", data)

    def test_completed_actions(self):
        """completed status should have same actions as accepted."""
        kb = task_detail_keyboard("T0006", "completed")
        data = self._get_all_data(kb)
        self.assertIn("task_doc:T0006", data)
        self.assertIn("events:T0006", data)
        self.assertIn("menu:tasks_accepted", data)

    def test_failed_actions(self):
        kb = task_detail_keyboard("T0007", "failed")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("retry:T0007", data)
        self.assertIn("task_doc:T0007", data)
        self.assertIn("task_delete:T0007", data)
        self.assertIn("menu:tasks_failed", data)

    def test_timeout_actions(self):
        """timeout status should have same actions as failed."""
        kb = task_detail_keyboard("T0008", "timeout")
        data = self._get_all_data(kb)
        self.assertIn("retry:T0008", data)
        self.assertIn("task_doc:T0008", data)
        self.assertIn("menu:tasks_failed", data)

    def test_unknown_status_fallback(self):
        kb = task_detail_keyboard("T0009", "unknown_status")
        self._assert_valid_keyboard(kb)
        data = self._get_all_data(kb)
        self.assertIn("task_doc:T0009", data)
        self.assertIn("menu:sub_task_mgmt", data)


class TestTasksOverviewKeyboard(unittest.TestCase):
    """Tests for tasks_overview_keyboard."""

    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_overview_valid(self):
        counts = {
            "pending": 2, "processing": 1, "pending_acceptance": 3,
            "rejected": 1, "accepted": 5, "failed": 0, "archived": 12,
        }
        kb = tasks_overview_keyboard(counts)
        self._assert_valid_keyboard(kb)

    def test_overview_has_all_categories(self):
        counts = {
            "pending": 2, "processing": 1, "pending_acceptance": 3,
            "rejected": 1, "accepted": 5, "failed": 0, "archived": 12,
        }
        kb = tasks_overview_keyboard(counts)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("menu:tasks_pending", all_data)
        self.assertIn("menu:tasks_processing", all_data)
        self.assertIn("menu:tasks_pending_acceptance", all_data)
        self.assertIn("menu:tasks_rejected", all_data)
        self.assertIn("menu:tasks_accepted", all_data)
        self.assertIn("menu:tasks_failed", all_data)
        self.assertIn("menu:tasks_archived", all_data)
        self.assertIn("menu:sub_task_mgmt", all_data)

    def test_overview_shows_counts_in_text(self):
        counts = {"pending": 2, "processing": 0, "pending_acceptance": 3,
                   "rejected": 0, "accepted": 5, "failed": 1, "archived": 10}
        kb = tasks_overview_keyboard(counts)
        all_texts = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
        # Check some counts appear
        self.assertTrue(any(": 2" in t for t in all_texts))
        self.assertTrue(any(": 5" in t for t in all_texts))
        self.assertTrue(any(": 10" in t for t in all_texts))

    def test_overview_button_count(self):
        """7 status categories + 1 back = 8 buttons."""
        counts = {"pending": 0, "processing": 0, "pending_acceptance": 0,
                   "rejected": 0, "accepted": 0, "failed": 0, "archived": 0}
        kb = tasks_overview_keyboard(counts)
        all_btns = [btn for row in kb["inline_keyboard"] for btn in row]
        self.assertEqual(len(all_btns), 8)


class TestArchiveDetailKeyboard(unittest.TestCase):
    """Tests for archive_detail_keyboard."""

    def test_valid(self):
        kb = archive_detail_keyboard("arc-test-123")
        self.assertIn("inline_keyboard", kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("task_doc:arc-test-123", all_data)
        self.assertIn("archive_delete:arc-test-123", all_data)
        self.assertIn("menu:tasks_archived", all_data)


class TestSubmenuTexts(unittest.TestCase):
    """Test that task_mgmt submenu text is defined."""

    def test_task_mgmt_text_exists(self):
        self.assertIn("task_mgmt", SUBMENU_TEXTS)

    def test_task_mgmt_text_has_placeholder(self):
        text = SUBMENU_TEXTS["task_mgmt"]
        self.assertIn("{active_count}", text)

    def test_task_mgmt_text_format(self):
        text = SUBMENU_TEXTS["task_mgmt"].format(active_count=5)
        self.assertIn("5", text)
        self.assertIn("\u4efb\u52a1\u7ba1\u7406", text)


class TestStatusLabels(unittest.TestCase):
    """Test that status label dicts are properly defined."""

    def test_all_statuses_in_labels(self):
        expected = ["pending", "processing", "pending_acceptance",
                     "rejected", "accepted", "failed", "archived"]
        for s in expected:
            self.assertIn(s, TASK_STATUS_LABELS, "Missing label for: {}".format(s))
            self.assertIn(s, TASK_STATUS_EMPTY_LABELS, "Missing empty label for: {}".format(s))


class TestCallbackHandlerRouting(unittest.TestCase):
    """Test that new callback_data values are routed correctly in handle_callback_query."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create state dirs
        state_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_sub_task_mgmt_callback(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb1",
            "data": "menu:sub_task_mgmt",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        call_args = mock_send.call_args
        self.assertIn("\u4efb\u52a1\u7ba1\u7406", call_args[0][1])
        # Check keyboard is task_mgmt_menu
        kb = call_args[1].get("reply_markup") or (call_args[0][2] if len(call_args[0]) > 2 else None)
        if kb:
            all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
            self.assertIn("menu:tasks_pending", all_data)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_tasks_pending_empty(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb2",
            "data": "menu:tasks_pending",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u65e0", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_tasks_overview(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb3",
            "data": "menu:tasks_overview",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u6982\u89c8", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_task_detail_not_found(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb4",
            "data": "task_detail:T9999",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u4e0d\u5b58\u5728", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_task_doc_not_found(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb5",
            "data": "task_doc:T9999",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u4e0d\u5b58\u5728", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_task_cancel_confirm_prompt(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb6",
            "data": "task_cancel:T0001",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u53d6\u6d88", text)
        self.assertIn("T0001", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_task_delete_confirm_prompt(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb7",
            "data": "task_delete:T0001",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u5220\u9664", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_archive_delete_confirm_prompt(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb8",
            "data": "archive_delete:arc-test-123",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u5220\u9664", text)
        self.assertIn("arc-test-123", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_archive_detail_not_found(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb9",
            "data": "archive_detail:arc-nonexistent",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u672a\u627e\u5230", text)


class TestTaskStatusListWithRealTasks(unittest.TestCase):
    """Integration test: create tasks and verify filtered list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create required dirs
        for d in ["pending", "processing", "results", "state", "state/task_state", "archive"]:
            (Path(self.tmp.name) / "codex-tasks" / d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _register_task(self, task_id, task_code, status, chat_id=100):
        from task_state import register_task_created, update_task_runtime
        task = {
            "task_id": task_id,
            "task_code": task_code,
            "chat_id": chat_id,
            "text": "Test task {}".format(task_code),
            "action": "codex",
            "status": "pending",
        }
        register_task_created(task)
        if status != "pending":
            task["status"] = status
            update_task_runtime(task, status=status, stage="results")

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_filter_pending_acceptance(self, mock_send, mock_answer):
        self._register_task("task-001", "T0001", "pending_acceptance")
        self._register_task("task-002", "T0002", "pending_acceptance")
        self._register_task("task-003", "T0003", "rejected")

        from bot_commands import handle_callback_query
        cb = {
            "id": "cb10",
            "data": "menu:tasks_pending_acceptance",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()

        # Verify keyboard has T0001, T0002 but not T0003
        call_args = mock_send.call_args
        kb = call_args[1].get("reply_markup") or {}
        all_data = [btn["callback_data"] for row in kb.get("inline_keyboard", []) for btn in row]
        detail_refs = [d for d in all_data if d.startswith("task_detail:")]
        self.assertEqual(len(detail_refs), 2)
        self.assertFalse(any("T0003" in d for d in detail_refs))

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_filter_no_tasks(self, mock_send, mock_answer):
        self._register_task("task-001", "T0001", "pending_acceptance")

        from bot_commands import handle_callback_query
        cb = {
            "id": "cb11",
            "data": "menu:tasks_processing",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("\u65e0", text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_overview_counts(self, mock_send, mock_answer):
        self._register_task("task-001", "T0001", "pending_acceptance")
        self._register_task("task-002", "T0002", "pending_acceptance")
        self._register_task("task-003", "T0003", "rejected")
        self._register_task("task-004", "T0004", "processing")

        from bot_commands import handle_callback_query
        cb = {
            "id": "cb12",
            "data": "menu:tasks_overview",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        kb = mock_send.call_args[1].get("reply_markup") or {}
        all_texts = [btn["text"] for row in kb.get("inline_keyboard", []) for btn in row]
        # Check counts are displayed
        self.assertTrue(any(": 2" in t for t in all_texts), "pending_acceptance count")
        self.assertTrue(any(": 1" in t and "\u62d2" in t for t in all_texts), "rejected count")

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_task_detail_with_real_task(self, mock_send, mock_answer):
        self._register_task("task-001", "T0001", "pending_acceptance")
        # Create a task file so find_task works
        task_data = {
            "task_id": "task-001",
            "task_code": "T0001",
            "chat_id": 100,
            "text": "Test task for acceptance",
            "status": "pending_acceptance",
            "action": "codex",
        }
        p = Path(self.tmp.name) / "codex-tasks" / "results" / "task-001"
        p.write_text(json.dumps(task_data), encoding="utf-8")

        from bot_commands import handle_callback_query
        cb = {
            "id": "cb13",
            "data": "task_detail:T0001",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        self.assertIn("T0001", text)
        self.assertIn("\u8be6\u60c5", text)
        # Check keyboard has accept/reject buttons
        kb = mock_send.call_args[1].get("reply_markup") or {}
        all_data = [btn["callback_data"] for row in kb.get("inline_keyboard", []) for btn in row]
        self.assertIn("accept:T0001", all_data)
        self.assertIn("reject:T0001", all_data)


class TestPaginationCallback(unittest.TestCase):
    """Test tasks_page callback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        for d in ["pending", "processing", "results", "state", "state/task_state", "archive"]:
            (Path(self.tmp.name) / "codex-tasks" / d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_pagination_page1(self, mock_send, mock_answer):
        # Create 8 tasks
        from task_state import register_task_created, update_task_runtime
        for i in range(8):
            tid = "task-{:03d}".format(i)
            task = {
                "task_id": tid,
                "chat_id": 100,
                "text": "Task {}".format(i),
                "action": "codex",
                "status": "pending",
            }
            code = register_task_created(task)
            update_task_runtime({"task_id": tid, "task_code": code}, status="rejected", stage="results")

        from bot_commands import handle_callback_query
        cb = {
            "id": "cb14",
            "data": "tasks_page:rejected:1",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called()
        kb = mock_send.call_args[1].get("reply_markup") or {}
        all_data = [btn["callback_data"] for row in kb.get("inline_keyboard", []) for btn in row]
        # Should have prev page button
        self.assertTrue(any("tasks_page:rejected:0" in d for d in all_data))


class TestRemoveArchiveEntry(unittest.TestCase):
    """Test _remove_archive_entry helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        archive_dir = Path(self.tmp.name) / "codex-tasks" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_remove_existing_entry(self):
        from task_state import _archive_index_file
        # Write test archive entry
        idx_file = _archive_index_file()
        entry = {"archive_id": "arc-test-001", "task_code": "T0001", "summary": "test"}
        idx_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        from bot_commands import _remove_archive_entry
        result = _remove_archive_entry("arc-test-001")
        self.assertTrue(result)
        # Verify entry is gone
        content = idx_file.read_text(encoding="utf-8").strip()
        self.assertEqual(content, "")

    def test_remove_nonexistent_entry(self):
        from bot_commands import _remove_archive_entry
        result = _remove_archive_entry("arc-nonexistent")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
