"""Tests for interactive_menu.py - pending actions and keyboard builders."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from interactive_menu import (  # noqa: E402
    HELP_TEXT,
    PENDING_PROMPTS,
    SUBMENU_TEXTS,
    WELCOME_TEXT,
    archive_menu_keyboard,
    back_to_menu_keyboard,
    backend_select_keyboard,
    cancel_keyboard,
    clear_pending_action,
    confirm_cancel_keyboard,
    fuzzy_workspace_add_keyboard,
    get_pending_action,
    main_menu_keyboard,
    ops_menu_keyboard,
    peek_pending_action,
    pipeline_preset_keyboard,
    search_roots_keyboard,
    security_menu_keyboard,
    set_pending_action,
    system_menu_keyboard,
    task_list_action_keyboard,
    workspace_menu_keyboard,
    workspace_select_keyboard,
)


class TestPendingActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_set_and_get(self):
        set_pending_action(100, 200, "new_task", {"extra": "data"})
        result = get_pending_action(100, 200)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "new_task")
        self.assertEqual(result["context"]["extra"], "data")

    def test_get_clears_action(self):
        set_pending_action(100, 200, "screenshot")
        self.assertIsNotNone(get_pending_action(100, 200))
        # Second get should return None (cleared)
        self.assertIsNone(get_pending_action(100, 200))

    def test_peek_does_not_clear(self):
        set_pending_action(100, 200, "archive_search")
        result = peek_pending_action(100, 200)
        self.assertIsNotNone(result)
        # Still available
        result2 = peek_pending_action(100, 200)
        self.assertIsNotNone(result2)

    def test_clear(self):
        set_pending_action(100, 200, "mgr_restart")
        clear_pending_action(100, 200)
        self.assertIsNone(get_pending_action(100, 200))

    def test_different_users(self):
        set_pending_action(100, 201, "new_task")
        set_pending_action(100, 202, "screenshot")
        r1 = get_pending_action(100, 201)
        r2 = get_pending_action(100, 202)
        self.assertEqual(r1["action"], "new_task")
        self.assertEqual(r2["action"], "screenshot")

    def test_get_nonexistent(self):
        self.assertIsNone(get_pending_action(999, 999))


class TestKeyboardBuilders(unittest.TestCase):
    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        rows = kb["inline_keyboard"]
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIsInstance(row, list)
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_main_menu(self):
        self._assert_valid_keyboard(main_menu_keyboard())

    def test_system_menu(self):
        self._assert_valid_keyboard(system_menu_keyboard())

    def test_archive_menu(self):
        self._assert_valid_keyboard(archive_menu_keyboard())

    def test_ops_menu(self):
        self._assert_valid_keyboard(ops_menu_keyboard())

    def test_security_menu(self):
        self._assert_valid_keyboard(security_menu_keyboard())

    def test_backend_select(self):
        kb = backend_select_keyboard()
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("codex" in d for d in all_data))
        self.assertTrue(any("claude" in d for d in all_data))

    def test_pipeline_preset(self):
        self._assert_valid_keyboard(pipeline_preset_keyboard())

    def test_cancel(self):
        self._assert_valid_keyboard(cancel_keyboard())

    def test_back_to_menu(self):
        self._assert_valid_keyboard(back_to_menu_keyboard())

    def test_task_list_action(self):
        self._assert_valid_keyboard(task_list_action_keyboard())

    def test_confirm_cancel(self):
        kb = confirm_cancel_keyboard("restart")
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("confirm:restart" in d for d in all_data))

    def test_workspace_menu(self):
        kb = workspace_menu_keyboard()
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("workspace_list" in d for d in all_data))
        self.assertTrue(any("workspace_add" in d for d in all_data))
        self.assertTrue(any("workspace_remove" in d for d in all_data))
        self.assertTrue(any("workspace_set_default" in d for d in all_data))
        self.assertTrue(any("workspace_queue_status" in d for d in all_data))
        self.assertTrue(any("workspace_search_roots" in d for d in all_data))

    def test_workspace_select(self):
        workspaces = [
            {"id": "ws-001", "label": "project-a", "is_default": True, "active": True},
            {"id": "ws-002", "label": "project-b", "is_default": False, "active": True},
        ]
        kb = workspace_select_keyboard(workspaces, "ws_test")
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("ws_test:ws-001" in d for d in all_data))
        self.assertTrue(any("ws_test:ws-002" in d for d in all_data))
        # Cancel button
        self.assertTrue(any("menu:cancel" in d for d in all_data))

    def test_workspace_select_empty(self):
        kb = workspace_select_keyboard([], "ws_test")
        self._assert_valid_keyboard(kb)
        # Should only have cancel button
        self.assertEqual(len(kb["inline_keyboard"]), 1)

    def test_main_menu_has_workspace(self):
        kb = main_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("sub_workspace" in d for d in all_data))


class TestTextConstants(unittest.TestCase):
    def test_welcome_has_placeholders(self):
        self.assertIn("{backend}", WELCOME_TEXT)
        self.assertIn("{model}", WELCOME_TEXT)

    def test_help_has_commands(self):
        self.assertIn("/menu", HELP_TEXT)
        self.assertIn("/task", HELP_TEXT)
        self.assertIn("/accept", HELP_TEXT)
        self.assertIn("/reject", HELP_TEXT)

    def test_submenu_texts(self):
        self.assertIn("system", SUBMENU_TEXTS)
        self.assertIn("archive", SUBMENU_TEXTS)
        self.assertIn("ops", SUBMENU_TEXTS)
        self.assertIn("security", SUBMENU_TEXTS)
        self.assertIn("workspace", SUBMENU_TEXTS)

    def test_pending_prompts(self):
        self.assertIn("new_task", PENDING_PROMPTS)
        self.assertIn("screenshot", PENDING_PROMPTS)
        self.assertIn("set_workspace", PENDING_PROMPTS)
        self.assertIn("workspace_remove", PENDING_PROMPTS)
        self.assertIn("workspace_set_default", PENDING_PROMPTS)
        self.assertIn("new_task_with_workspace", PENDING_PROMPTS)
        self.assertIn("search_root_add", PENDING_PROMPTS)

    def test_help_has_search_roots_command(self):
        self.assertIn("/workspace_search_roots", HELP_TEXT)

    def test_workspace_add_prompt_has_fuzzy_hint(self):
        prompt = PENDING_PROMPTS["workspace_add"]
        self.assertIn("关键词", prompt)
        self.assertIn("模糊", prompt)


class TestFuzzyWorkspaceAddKeyboard(unittest.TestCase):
    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_single_candidate(self):
        candidates = [Path("/tmp/my-toolbox")]
        kb = fuzzy_workspace_add_keyboard(candidates)
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("ws_fuzzy_add:1", all_data)
        # Cancel button
        self.assertTrue(any("menu:cancel" in d for d in all_data))

    def test_multiple_candidates(self):
        candidates = [Path("/tmp/toolbox-a"), Path("/tmp/toolbox-b"), Path("/tmp/toolbox-c")]
        kb = fuzzy_workspace_add_keyboard(candidates)
        self._assert_valid_keyboard(kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("ws_fuzzy_add:1", all_data)
        self.assertIn("ws_fuzzy_add:2", all_data)
        self.assertIn("ws_fuzzy_add:3", all_data)
        # Total: 3 candidates + 1 cancel
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    def test_empty_candidates(self):
        kb = fuzzy_workspace_add_keyboard([])
        self._assert_valid_keyboard(kb)
        # Only cancel button
        self.assertEqual(len(kb["inline_keyboard"]), 1)

    def test_custom_prefix(self):
        candidates = [Path("/tmp/proj")]
        kb = fuzzy_workspace_add_keyboard(candidates, callback_prefix="custom")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("custom:1", all_data)

    def test_long_path_truncated(self):
        long_name = "a" * 100
        candidates = [Path("/very/long/directory/path") / long_name]
        kb = fuzzy_workspace_add_keyboard(candidates)
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertLessEqual(len(btn_text), 63)  # 60 + "..."


class TestSearchRootsKeyboard(unittest.TestCase):
    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_empty_roots(self):
        kb = search_roots_keyboard([])
        self._assert_valid_keyboard(kb)
        # Should have add button + back button
        self.assertEqual(len(kb["inline_keyboard"]), 2)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("search_root_add" in d for d in all_data))
        self.assertTrue(any("sub_workspace" in d for d in all_data))

    def test_with_roots(self):
        roots = ["/tmp/projects", "/tmp/repos"]
        kb = search_roots_keyboard(roots)
        self._assert_valid_keyboard(kb)
        # 2 remove buttons + add button + back button = 4 rows
        self.assertEqual(len(kb["inline_keyboard"]), 4)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("sr_remove:1", all_data)
        self.assertIn("sr_remove:2", all_data)

    def test_long_path_truncated(self):
        long_path = "C:\\Users\\someone\\very\\deep\\nested\\directory\\structure\\that\\is\\really\\long"
        kb = search_roots_keyboard([long_path])
        btn_text = kb["inline_keyboard"][0][0]["text"]
        # Should be truncated (prefix + max 50 chars)
        self.assertLessEqual(len(btn_text), 56)  # "⛔ " prefix + 50 + "..."


if __name__ == "__main__":
    unittest.main()
