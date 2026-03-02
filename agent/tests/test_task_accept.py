"""Tests for task_accept.py - acceptance documents and finalization."""
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

from task_accept import (  # noqa: E402
    acceptance_notice_text,
    acceptance_root,
    build_acceptance_cases,
    build_task_summary,
    json_sha256,
    task_inline_keyboard,
    to_pending_acceptance,
    write_acceptance_documents,
    write_run_log,
)
from utils import utc_iso  # noqa: E402


class TestJsonSha256(unittest.TestCase):
    def test_deterministic(self):
        data = {"key": "value", "num": 42}
        h1 = json_sha256(data)
        h2 = json_sha256(data)
        self.assertEqual(h1, h2)

    def test_different_data(self):
        h1 = json_sha256({"a": 1})
        h2 = json_sha256({"a": 2})
        self.assertNotEqual(h1, h2)

    def test_hash_length(self):
        h = json_sha256({"test": True})
        self.assertEqual(len(h), 64)  # SHA256 hex digest


class TestWriteRunLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_creates_log_file(self):
        path = write_run_log("task-log-1", {"cmd": "echo", "returncode": 0})
        self.assertTrue(path.exists())
        self.assertIn("task-log-1", str(path))


class TestBuildAcceptanceCases(unittest.TestCase):
    def test_all_cases_generated(self):
        task = {"task_id": "t1", "text": "修复bug", "action": "codex"}
        result = {
            "status": "completed",
            "executor": {
                "last_message": "done",
                "runlog_file": "/tmp/log.json",
                "git_changed_files": ["fix.py"],
            },
        }
        cases = build_acceptance_cases(task, result)
        self.assertEqual(len(cases), 5)
        ids = {c["case_id"] for c in cases}
        self.assertIn("AC-000", ids)
        self.assertIn("AC-001", ids)
        self.assertIn("AC-002", ids)
        self.assertIn("AC-003", ids)
        self.assertIn("UAT-001", ids)

    def test_empty_text_fails_ac000(self):
        task = {"task_id": "t2", "text": "", "action": "codex"}
        result = {"status": "completed", "executor": {}}
        cases = build_acceptance_cases(task, result)
        ac000 = next(c for c in cases if c["case_id"] == "AC-000")
        self.assertEqual(ac000["status"], "failed")

    def test_completed_passes_ac001(self):
        task = {"task_id": "t3", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {"last_message": "ok"}}
        cases = build_acceptance_cases(task, result)
        ac001 = next(c for c in cases if c["case_id"] == "AC-001")
        self.assertEqual(ac001["status"], "passed")

    def test_failed_fails_ac001(self):
        task = {"task_id": "t4", "text": "test", "action": "codex"}
        result = {"status": "failed", "executor": {"last_message": "error"}}
        cases = build_acceptance_cases(task, result)
        ac001 = next(c for c in cases if c["case_id"] == "AC-001")
        self.assertEqual(ac001["status"], "failed")

    def test_uat001_always_pending(self):
        task = {"task_id": "t5", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {}}
        cases = build_acceptance_cases(task, result)
        uat001 = next(c for c in cases if c["case_id"] == "UAT-001")
        self.assertEqual(uat001["status"], "pending")


class TestWriteAcceptanceDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_generates_doc_and_cases(self):
        task = {"task_id": "task-doc-1", "text": "测试任务", "action": "codex"}
        result = {
            "task_code": "T0001",
            "status": "completed",
            "executor": {
                "elapsed_ms": 500,
                "returncode": 0,
                "workspace": "/ws",
                "last_message": "完成",
                "runlog_file": "",
                "git_changed_files": [],
            },
        }
        docs = write_acceptance_documents(task, result)
        self.assertTrue(Path(docs["doc_file"]).exists())
        self.assertTrue(Path(docs["cases_file"]).exists())

        # Check doc content
        content = Path(docs["doc_file"]).read_text(encoding="utf-8")
        self.assertIn("验收门禁规则", content)
        self.assertIn("task-doc-1", content)

        # Check cases content
        cases_data = json.loads(Path(docs["cases_file"]).read_text(encoding="utf-8"))
        self.assertIn("cases", cases_data)


class TestToPendingAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_sets_pending_acceptance(self):
        task = {"task_id": "task-pa-1", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {"last_message": "ok"}}
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["status"], "pending_acceptance")
        self.assertEqual(out["execution_status"], "completed")
        self.assertEqual(out["acceptance"]["state"], "pending")
        self.assertTrue(out["acceptance"]["acceptance_required"])
        self.assertFalse(out["acceptance"]["archive_allowed"])

    def test_preserves_execution_status(self):
        task = {"task_id": "task-pa-2", "text": "test", "action": "codex"}
        result = {"status": "failed", "executor": {"last_message": "err"}}
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["execution_status"], "failed")
        self.assertEqual(out["status"], "pending_acceptance")


class TestBuildTaskSummary(unittest.TestCase):
    def test_with_message(self):
        result = {"executor": {"last_message": "任务完成", "noop_reason": ""}}
        self.assertEqual(build_task_summary(result), "任务完成")

    def test_with_noop(self):
        result = {"executor": {"last_message": "", "noop_reason": "无执行"}}
        self.assertIn("失败原因", build_task_summary(result))

    def test_with_error(self):
        result = {"error": "timeout", "executor": {}}
        self.assertIn("错误", build_task_summary(result))

    def test_fallback(self):
        result = {"executor": {}}
        self.assertIn("日志文件", build_task_summary(result))


class TestAcceptanceNoticeText(unittest.TestCase):
    def test_completed_notice(self):
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 1000, "last_message": "ok"},
        }
        text = acceptance_notice_text(result, "task-1", "T0001", detailed=True)
        self.assertIn("T0001", text)
        self.assertIn("pending_acceptance", text)
        self.assertIn("/accept T0001", text)

    def test_failed_notice(self):
        result = {
            "execution_status": "failed",
            "executor": {"elapsed_ms": 500, "noop_reason": "无执行"},
        }
        text = acceptance_notice_text(result, "task-2", "T0002", detailed=False)
        self.assertIn("执行失败", text)
        self.assertIn("/reject T0002", text)

    def test_brief_vs_detailed(self):
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 1000, "last_message": "ok"},
        }
        brief = acceptance_notice_text(result, "t1", "T1", detailed=False)
        detailed = acceptance_notice_text(result, "t1", "T1", detailed=True)
        self.assertGreater(len(detailed), len(brief))


class TestTaskInlineKeyboard(unittest.TestCase):
    def test_keyboard_structure(self):
        kb = task_inline_keyboard("T0001", "task-1")
        self.assertIn("inline_keyboard", kb)
        rows = kb["inline_keyboard"]
        self.assertGreater(len(rows), 0)
        # Check first row has buttons
        self.assertGreater(len(rows[0]), 0)
        # Check callback data contains task code
        all_data = [btn["callback_data"] for row in rows for btn in row]
        self.assertTrue(any("T0001" in d for d in all_data))


if __name__ == "__main__":
    unittest.main()
