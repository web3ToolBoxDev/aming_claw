import json
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

import bot_commands  # noqa: E402
import coordinator  # noqa: E402
import executor  # noqa: E402
from utils import save_json, tasks_root, utc_iso  # noqa: E402


class AcceptanceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_to_pending_acceptance_writes_detailed_docs_and_cases(self) -> None:
        task = {
            "task_id": "task-1",
            "task_code": "T0001",
            "chat_id": 123,
            "action": "codex",
            "text": "新增一个可查询状态的接口",
            "created_at": utc_iso(),
        }
        result = {
            "task_id": "task-1",
            "task_code": "T0001",
            "status": "completed",
            "executor": {
                "elapsed_ms": 1234,
                "returncode": 0,
                "workspace": "C:/repo/demo",
                "last_message": "已完成接口与参数校验",
                "runlog_file": "shared-volume/codex-tasks/logs/task-1.run.json",
                "git_changed_files": ["api/status.py"],
            },
        }

        out = executor.to_pending_acceptance(task, result)
        self.assertEqual(out["status"], "pending_acceptance")
        self.assertEqual(out["acceptance"]["state"], "pending")
        self.assertTrue(out["acceptance"]["acceptance_required"])
        self.assertFalse(out["acceptance"]["archive_allowed"])

        doc_file = Path(out["acceptance"]["doc_file"])
        cases_file = Path(out["acceptance"]["cases_file"])
        self.assertTrue(doc_file.exists())
        self.assertTrue(cases_file.exists())

        doc_text = doc_file.read_text(encoding="utf-8")
        self.assertIn("验收门禁规则", doc_text)
        self.assertIn("/accept T0001", doc_text)
        self.assertIn("/status T0001", doc_text)

        cases = json.loads(cases_file.read_text(encoding="utf-8"))
        case_ids = {x.get("case_id") for x in cases.get("cases", [])}
        self.assertIn("UAT-001", case_ids)

    def test_acceptance_tag_mapping(self) -> None:
        self.assertEqual(coordinator.acceptance_tag({"status": "pending_acceptance"}), "待验收")
        self.assertEqual(coordinator.acceptance_tag({"status": "rejected"}), "验收拒绝")
        self.assertEqual(coordinator.acceptance_tag({"status": "accepted"}), "验收通过")
        self.assertEqual(
            coordinator.acceptance_tag({"_stage": "results", "status": "completed"}),
            "待验收(兼容旧任务)",
        )
        # succeeded is also a legacy completion status
        self.assertEqual(
            coordinator.acceptance_tag({"_stage": "results", "status": "succeeded"}),
            "待验收(兼容旧任务)",
        )
        # archived tasks should show as accepted
        self.assertEqual(
            coordinator.acceptance_tag({"_stage": "archive", "status": "unknown"}),
            "验收通过",
        )
        # acceptance dict takes precedence
        self.assertEqual(
            coordinator.acceptance_tag({"status": "unknown", "acceptance": {"state": "accepted"}}),
            "验收通过",
        )

    def test_status_list_shows_accepted_task_correctly(self) -> None:
        """Accepted task in active list should display '验收通过' via full task data merge."""
        from task_state import register_task_created, update_task_runtime

        task = {
            "task_id": "task-acc-1",
            "chat_id": 500,
            "requested_by": 500,
            "action": "codex",
            "text": "已接受的任务",
            "status": "accepted",
            "acceptance": {"state": "accepted"},
        }
        register_task_created(task)
        update_task_runtime(task, status="accepted", stage="results")
        # Save the task file in results/ with acceptance dict
        save_json(tasks_root() / "results" / "task-acc-1.json", task)

        with patch.object(bot_commands, "send_text") as send_text_mock:
            ok = coordinator.handle_command(chat_id=500, user_id=500, text="/status")
            self.assertTrue(ok)
            self.assertTrue(send_text_mock.called)
            msg = send_text_mock.call_args_list[0][0][1]

        self.assertIn("验收: 验收通过", msg)
        self.assertNotIn("验收: 待验收", msg)
        self.assertNotIn("验收: 未知", msg)

    def test_status_list_reads_acceptance_from_task_file(self) -> None:
        """List view should read acceptance dict from task file, not just active entry."""
        from task_state import register_task_created, update_task_runtime

        # Simulate: active entry has old status, but task file has acceptance dict
        task = {
            "task_id": "task-acc-2",
            "chat_id": 600,
            "requested_by": 600,
            "action": "codex",
            "text": "验收文件读取测试",
        }
        register_task_created(task)
        update_task_runtime(task, status="pending_acceptance", stage="results")

        # Write accepted task file with acceptance dict
        task_file_data = dict(task)
        task_file_data["status"] = "accepted"
        task_file_data["acceptance"] = {"state": "accepted", "accepted_at": utc_iso()}
        save_json(tasks_root() / "results" / "task-acc-2.json", task_file_data)

        with patch.object(bot_commands, "send_text") as send_text_mock:
            ok = coordinator.handle_command(chat_id=600, user_id=600, text="/status")
            self.assertTrue(ok)
            msg = send_text_mock.call_args_list[0][0][1]

        # The list view should read the task file and show accepted status
        self.assertIn("验收通过", msg)

    def test_status_command_contains_acceptance_marker(self) -> None:
        task = {
            "task_id": "task-2",
            "task_code": "T0002",
            "chat_id": 123,
            "requested_by": 321,
            "action": "codex",
            "text": "修复登录异常",
            "status": "pending_acceptance",
            "created_at": utc_iso(),
            "updated_at": utc_iso(),
            "executor": {
                "elapsed_ms": 888,
                "last_message": "修复完成",
            },
            "acceptance": {
                "state": "pending",
                "doc_file": "shared-volume/codex-tasks/acceptance/task-2.acceptance.md",
                "cases_file": "shared-volume/codex-tasks/acceptance/task-2.cases.json",
            },
        }
        save_json(tasks_root() / "results" / "task-2.json", task)

        with patch.object(bot_commands, "send_text") as send_text_mock:
            ok = coordinator.handle_command(chat_id=123, user_id=321, text="/status task-2")
            self.assertTrue(ok)
            self.assertTrue(send_text_mock.called)
            # First call contains the status summary; subsequent calls contain events
            msg = send_text_mock.call_args_list[0][0][1]

        self.assertIn("验收标识: 待验收", msg)
        self.assertIn("下一步: 通过 /accept T0002", msg)
        self.assertIn("验收文档:", msg)
        self.assertIn("验收用例:", msg)


if __name__ == "__main__":
    unittest.main()
