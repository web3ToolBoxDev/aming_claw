"""Acceptance tests: archive management is nested under task management submenu.

TC-1: Main menu has no archive entries
TC-2: Task management submenu has archive management entry
TC-3: Archive submenu return button points to task management
TC-4: Archive submenu has all 4 function buttons
TC-5: Callback handler shows archive menu on sub_archive click
TC-6: Full navigation loop: main -> task_mgmt -> archive -> back to task_mgmt
"""
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

from interactive_menu import (  # noqa: E402
    SUBMENU_TEXTS,
    archive_menu_keyboard,
    main_menu_keyboard,
    task_mgmt_menu_keyboard,
)


def _all_callback_data(kb):
    """Extract all callback_data values from a keyboard dict."""
    return [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]


def _all_buttons(kb):
    """Extract all (text, callback_data) tuples from a keyboard dict."""
    return [(btn["text"], btn["callback_data"]) for row in kb["inline_keyboard"] for btn in row]


class TC1_MainMenuNoArchive(unittest.TestCase):
    """TC-1: Main menu must NOT contain any archive-related buttons."""

    def test_no_sub_archive(self):
        kb = main_menu_keyboard()
        all_data = _all_callback_data(kb)
        self.assertNotIn("menu:sub_archive", all_data)

    def test_no_archive_prefix(self):
        kb = main_menu_keyboard()
        all_data = _all_callback_data(kb)
        archive_items = [d for d in all_data if "archive" in d]
        self.assertEqual(archive_items, [], "Main menu should have no archive-related buttons")


class TC2_TaskMgmtHasArchiveEntry(unittest.TestCase):
    """TC-2: Task management submenu must include archive management entry."""

    def test_has_sub_archive_button(self):
        kb = task_mgmt_menu_keyboard()
        all_data = _all_callback_data(kb)
        self.assertIn("menu:sub_archive", all_data)

    def test_archive_button_text_contains_label(self):
        kb = task_mgmt_menu_keyboard()
        buttons = _all_buttons(kb)
        archive_btn = [b for b in buttons if b[1] == "menu:sub_archive"]
        self.assertEqual(len(archive_btn), 1)
        self.assertIn("\u5f52\u6863\u7ba1\u7406", archive_btn[0][0])


class TC3_ArchiveReturnButton(unittest.TestCase):
    """TC-3: Archive submenu return button must point to task management."""

    def test_return_button_callback(self):
        kb = archive_menu_keyboard()
        last_row = kb["inline_keyboard"][-1]
        back_btn = last_row[0]
        self.assertEqual(back_btn["callback_data"], "menu:sub_task_mgmt")

    def test_return_button_text(self):
        kb = archive_menu_keyboard()
        last_row = kb["inline_keyboard"][-1]
        back_btn = last_row[0]
        self.assertIn("\u8fd4\u56de\u4efb\u52a1\u7ba1\u7406", back_btn["text"])


class TC4_ArchiveMenuFunctions(unittest.TestCase):
    """TC-4: Archive submenu must contain all 4 function buttons."""

    def test_has_archive_overview(self):
        all_data = _all_callback_data(archive_menu_keyboard())
        self.assertIn("menu:archive", all_data)

    def test_has_archive_search(self):
        all_data = _all_callback_data(archive_menu_keyboard())
        self.assertIn("menu:archive_search", all_data)

    def test_has_archive_log(self):
        all_data = _all_callback_data(archive_menu_keyboard())
        self.assertIn("menu:archive_log", all_data)

    def test_has_archive_show(self):
        all_data = _all_callback_data(archive_menu_keyboard())
        self.assertIn("menu:archive_show", all_data)

    def test_all_four_present(self):
        all_data = set(_all_callback_data(archive_menu_keyboard()))
        expected = {"menu:archive", "menu:archive_search", "menu:archive_log", "menu:archive_show"}
        self.assertTrue(expected.issubset(all_data))


class TC5_CallbackHandler(unittest.TestCase):
    """TC-5: sub_archive callback must show archive menu correctly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        state_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_sub_archive_shows_archive_menu(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb_test",
            "data": "menu:sub_archive",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        # reply_markup should be archive_menu_keyboard()
        kb = call_args[1].get("reply_markup") or (call_args[0][2] if len(call_args[0]) > 2 else None)
        expected_kb = archive_menu_keyboard()
        self.assertEqual(kb, expected_kb)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_sub_archive_answers_callback(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb_test2",
            "data": "menu:sub_archive",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_answer.assert_called()
        answer_text = mock_answer.call_args[0][1]
        self.assertEqual(answer_text, "\u5f52\u6863\u7ba1\u7406")


class TC6_FullNavLoop(unittest.TestCase):
    """TC-6: Full navigation: main -> task_mgmt -> archive -> back to task_mgmt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        state_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_navigation_chain(self):
        # Step 1: Main menu has task management entry
        main_kb = main_menu_keyboard()
        main_data = _all_callback_data(main_kb)
        self.assertIn("menu:sub_task_mgmt", main_data)

        # Step 2: Task management has archive entry
        task_kb = task_mgmt_menu_keyboard()
        task_data = _all_callback_data(task_kb)
        self.assertIn("menu:sub_archive", task_data)

        # Step 3: Archive menu has return to task management
        archive_kb = archive_menu_keyboard()
        archive_data = _all_callback_data(archive_kb)
        self.assertIn("menu:sub_task_mgmt", archive_data)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_navigation_callbacks(self, mock_send, mock_answer):
        from bot_commands import handle_callback_query

        # Step 1: Click task management from main menu
        cb1 = {
            "id": "nav1",
            "data": "menu:sub_task_mgmt",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb1)
        kb1 = mock_send.call_args[1].get("reply_markup") or {}
        data1 = _all_callback_data(kb1)
        self.assertIn("menu:sub_archive", data1, "Task mgmt menu must have archive entry")

        mock_send.reset_mock()

        # Step 2: Click archive management from task management menu
        cb2 = {
            "id": "nav2",
            "data": "menu:sub_archive",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb2)
        kb2 = mock_send.call_args[1].get("reply_markup") or {}
        data2 = _all_callback_data(kb2)
        self.assertIn("menu:archive", data2, "Archive menu must have overview")
        self.assertIn("menu:sub_task_mgmt", data2, "Archive menu must have back to task mgmt")

        mock_send.reset_mock()

        # Step 3: Click back from archive -> should go to task management
        cb3 = {
            "id": "nav3",
            "data": "menu:sub_task_mgmt",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb3)
        kb3 = mock_send.call_args[1].get("reply_markup") or {}
        data3 = _all_callback_data(kb3)
        self.assertIn("menu:tasks_pending", data3, "Back should show task mgmt menu")
        self.assertIn("menu:sub_archive", data3, "Task mgmt should still have archive")


class TC_Extra_SubmenuTexts(unittest.TestCase):
    """Extra: SUBMENU_TEXTS['archive'] must exist."""

    def test_archive_text_exists(self):
        self.assertIn("archive", SUBMENU_TEXTS)

    def test_archive_text_meaningful(self):
        text = SUBMENU_TEXTS["archive"]
        self.assertIn("\u5f52\u6863", text)

    def test_callback_data_byte_limit(self):
        """All callback_data must fit within 64 UTF-8 bytes."""
        keyboards = [main_menu_keyboard(), task_mgmt_menu_keyboard(), archive_menu_keyboard()]
        for kb in keyboards:
            for row in kb["inline_keyboard"]:
                for btn in row:
                    cb = btn["callback_data"]
                    self.assertLessEqual(
                        len(cb.encode("utf-8")), 64,
                        "callback_data too long: {} ({} bytes)".format(cb, len(cb.encode("utf-8"))),
                    )


if __name__ == "__main__":
    unittest.main()
