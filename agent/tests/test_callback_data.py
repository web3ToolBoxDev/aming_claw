"""Tests for callback_data truncation and archive prefix matching."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from interactive_menu import safe_callback_data, confirm_cancel_keyboard, archive_detail_keyboard  # noqa: E402
from task_state import _build_archive_id, find_archive_entry, register_task_created, archive_task_result  # noqa: E402


class TestSafeCallbackData(unittest.TestCase):
    """Verify safe_callback_data always produces ≤ 64 byte results."""

    def test_short_data_unchanged(self):
        result = safe_callback_data("task_doc", "T0001")
        self.assertEqual(result, "task_doc:T0001")

    def test_long_ascii_truncated(self):
        long_id = "arc-" + "a" * 80
        result = safe_callback_data("archive_delete", long_id)
        self.assertLessEqual(len(result.encode("utf-8")), 64)
        self.assertTrue(result.startswith("archive_delete:arc-"))

    def test_long_chinese_truncated(self):
        long_id = "arc-" + "修复" * 20
        result = safe_callback_data("archive_delete", long_id)
        self.assertLessEqual(len(result.encode("utf-8")), 64)

    def test_confirm_prefix_truncated(self):
        long_id = "arc-" + "x" * 60
        result = safe_callback_data("confirm:archive_delete", long_id)
        self.assertLessEqual(len(result.encode("utf-8")), 64)
        self.assertTrue(result.startswith("confirm:archive_delete:arc-"))

    def test_all_archive_button_patterns(self):
        """Test that all archive button callback_data patterns fit in 64 bytes."""
        # Worst case: 48-byte archive ID (max from _build_archive_id)
        aid = "arc-" + "a" * 24 + "-20260302-abcdef"  # 48 bytes
        self.assertEqual(len(aid.encode("utf-8")), 48)

        patterns = [
            safe_callback_data("task_doc", aid),
            safe_callback_data("archive_delete", aid),
            safe_callback_data("archive_detail", aid),
            safe_callback_data("confirm:archive_delete", aid),
        ]
        for p in patterns:
            self.assertLessEqual(
                len(p.encode("utf-8")), 64,
                "Pattern too long: {} ({} bytes)".format(p, len(p.encode("utf-8"))),
            )

    def test_no_trailing_dash(self):
        # When truncation happens at a dash boundary
        long_id = "arc-aaa-bbb-ccc-ddd-eee-fff-ggg-hhh"
        result = safe_callback_data("confirm:archive_delete", long_id)
        identifier = result.split(":", 2)[-1]  # get the archive_id part
        self.assertFalse(identifier.endswith("-"))


class TestConfirmCancelKeyboard(unittest.TestCase):
    """Verify confirm_cancel_keyboard produces valid callback_data."""

    def test_short_context(self):
        kbd = confirm_cancel_keyboard("archive_delete", "T0001")
        cb = kbd["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(cb, "confirm:archive_delete:T0001")

    def test_long_context_truncated(self):
        long_id = "arc-" + "x" * 60
        kbd = confirm_cancel_keyboard("archive_delete", long_id)
        cb = kbd["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(cb.encode("utf-8")), 64)


class TestArchiveDetailKeyboard(unittest.TestCase):
    """Verify archive_detail_keyboard produces valid callback_data."""

    def test_long_archive_id(self):
        long_id = "arc-" + "修复" * 15 + "-20260302-abcdef"
        kbd = archive_detail_keyboard(long_id)
        for row in kbd["inline_keyboard"]:
            for btn in row:
                cb = btn.get("callback_data", "")
                self.assertLessEqual(
                    len(cb.encode("utf-8")), 64,
                    "Button '{}' callback too long: {} bytes".format(
                        btn.get("text", ""), len(cb.encode("utf-8"))
                    ),
                )


class TestArchivePrefixMatchIntegration(unittest.TestCase):
    """Integration test: truncated callback_data can find the archive entry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_truncated_id_finds_entry(self):
        # Create an archive entry
        task = {
            "task_id": "task-cb-001",
            "task_code": "T0050",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "修复点击归档任务报错按钮操作失败系统崩溃错误",
            "status": "accepted",
            "executor": {"last_message": "done"},
        }
        register_task_created(task)
        from utils import save_json, tasks_root
        result_path = tasks_root() / "results" / "task-cb-001.json"
        save_json(result_path, task)
        entry = archive_task_result(task, result_path, None)
        aid = entry["archive_id"]

        # Simulate what happens: button is built with safe_callback_data
        cb = safe_callback_data("archive_detail", aid)
        self.assertLessEqual(len(cb.encode("utf-8")), 64)

        # Extract the (possibly truncated) ID from callback_data
        ref = cb.split(":", 1)[1]

        # find_archive_entry should still find it via prefix match
        found = find_archive_entry(ref)
        self.assertIsNotNone(found, "Should find archive entry with ref: {}".format(ref))
        self.assertEqual(found["archive_id"], aid)


if __name__ == "__main__":
    unittest.main()
