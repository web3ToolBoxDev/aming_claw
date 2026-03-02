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
