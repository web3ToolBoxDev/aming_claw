"""Tests for TaskOrchestrator auto-chain pipeline (Dev→Gatekeeper→Tester→QA→Merge).

Covers:
  - Isolated gatekeeper checkpoint (no coordinator eval)
  - Idempotency key dedup
  - Global retry budget (RETRY_BUDGET=6)
  - Failure memory dedup
  - Pipeline audit JSONL logging
  - Child task workspace/parent_task_id/auto_triggered inheritance
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Setup path
agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class _FakeEvidence:
    def __init__(self, files=None, diff_stat=""):
        self.changed_files = files or []
        self.new_files = []
        self.test_results = {"passed": True}
        self.diff_stat = diff_stat

    def to_dict(self):
        return {"changed_files": self.changed_files, "test_results": self.test_results}


class _FakeSession:
    def __init__(self):
        self.session_id = "sess-123"


class _FakeValidation:
    def __init__(self):
        self.needs_retry = False
        self.summary = ""
        self.approved_actions = []
        self.rejected_actions = []


def _make_orchestrator(tmp_dir):
    """Create a TaskOrchestrator with mocked dependencies and tmp shared volume."""
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir

    # Patch sys.modules so imports inside __init__ receive mocks.
    # (AILifecycleManager etc. are imported inside __init__, not at module level,
    #  so patch("task_orchestrator.X") would fail with AttributeError.)
    mock_sys_modules = {
        "ai_lifecycle": MagicMock(),
        "context_assembler": MagicMock(),
        "decision_validator": MagicMock(),
        "graph_validator": MagicMock(),
        "evidence_collector": MagicMock(),
        "ai_output_parser": MagicMock(),
    }
    with patch.dict(sys.modules, mock_sys_modules):
        from task_orchestrator import TaskOrchestrator
        orch = TaskOrchestrator()

    # Setup evidence collector mock
    orch.evidence_collector.collect_after_dev.return_value = _FakeEvidence(["agent/foo.py"])
    orch.evidence_collector.compare_with_ai_report.return_value = {
        "has_discrepancies": False,
        "discrepancies": [],
    }

    # Mock gateway reply
    orch._gateway_reply = MagicMock()

    return orch


class TestIsolatedGateCheck(unittest.TestCase):
    """Test _isolated_gate_check logic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_gate_pass_with_changes(self):
        evidence = _FakeEvidence(["a.py", "b.py"])
        result = self.orch._isolated_gate_check(
            evidence, [], [], {"discrepancies": []}
        )
        self.assertTrue(result["pass"])

    def test_gate_fail_no_changes(self):
        evidence = _FakeEvidence([])
        result = self.orch._isolated_gate_check(
            evidence, [], [], {"discrepancies": []}
        )
        self.assertFalse(result["pass"])
        self.assertIn("no file changes", result["reason"])

    def test_gate_fail_missing_target_files(self):
        evidence = _FakeEvidence(["a.py"])
        result = self.orch._isolated_gate_check(
            evidence, ["a.py", "b.py"], [], {"discrepancies": []}
        )
        self.assertFalse(result["pass"])
        self.assertIn("target files not modified", result["reason"])

    def test_gate_pass_all_targets_hit(self):
        evidence = _FakeEvidence(["a.py", "b.py"])
        result = self.orch._isolated_gate_check(
            evidence, ["a.py", "b.py"], [], {"discrepancies": []}
        )
        self.assertTrue(result["pass"])

    def test_gate_fail_critical_discrepancies(self):
        evidence = _FakeEvidence(["a.py"])
        result = self.orch._isolated_gate_check(
            evidence, [], [], {
                "discrepancies": [{"issue": "file_missing", "detail": "x"}]
            }
        )
        self.assertFalse(result["pass"])
        self.assertIn("critical discrepancies", result["reason"])

    def test_gate_pass_with_ai_discrepancies(self):
        """AI/evidence mismatches are audit-only, not blocking."""
        evidence = _FakeEvidence(["a.py"])
        result = self.orch._isolated_gate_check(
            evidence, [], [], {
                "discrepancies": [
                    {"issue": "ai_reported_but_not_changed", "detail": "b.py"},
                    {"issue": "changed_but_ai_not_reported", "detail": "a.py"},
                ]
            }
        )
        self.assertTrue(result["pass"])


class TestIdempotency(unittest.TestCase):
    """Test idempotency key checking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_first_check_returns_false(self):
        self.assertFalse(self.orch._check_idempotency("task-1", "tester"))

    def test_after_mark_returns_true(self):
        self.orch._mark_idempotency("task-1", "tester")
        self.assertTrue(self.orch._check_idempotency("task-1", "tester"))

    def test_different_stages_independent(self):
        self.orch._mark_idempotency("task-1", "tester")
        self.assertFalse(self.orch._check_idempotency("task-1", "qa"))

    def test_persisted_to_file(self):
        self.orch._mark_idempotency("task-1", "tester")
        path = os.path.join(self.orch._state_dir(), "pipeline_idempotency.json")
        self.assertTrue(os.path.exists(path))
        data = json.loads(open(path).read())
        self.assertIn("task-1:tester", data)


class TestRetryBudget(unittest.TestCase):
    """Test global retry budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_initial_count_zero(self):
        self.assertEqual(self.orch._get_retry_count("task-1"), 0)

    def test_increment(self):
        self.assertEqual(self.orch._increment_retry("task-1"), 1)
        self.assertEqual(self.orch._increment_retry("task-1"), 2)
        self.assertEqual(self.orch._get_retry_count("task-1"), 2)

    def test_budget_not_exceeded(self):
        self.assertFalse(self.orch._budget_exceeded("task-1"))

    def test_budget_exceeded_at_limit(self):
        from task_orchestrator import RETRY_BUDGET
        for _ in range(RETRY_BUDGET):
            self.orch._increment_retry("task-1")
        self.assertTrue(self.orch._budget_exceeded("task-1"))

    def test_budget_is_six(self):
        from task_orchestrator import RETRY_BUDGET
        self.assertEqual(RETRY_BUDGET, 6)


class TestAuditLog(unittest.TestCase):
    """Test pipeline audit JSONL logging."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_audit_log_creates_file(self):
        self.orch._audit_log("task-1", "dev", "gatekeeper", "test detail")
        path = os.path.join(self.orch._logs_dir(), "pipeline_audit.jsonl")
        self.assertTrue(os.path.exists(path))

    def test_audit_log_jsonl_format(self):
        self.orch._audit_log("task-1", "dev", "gatekeeper", "detail1")
        self.orch._audit_log("task-1", "gatekeeper", "tester", "detail2")
        path = os.path.join(self.orch._logs_dir(), "pipeline_audit.jsonl")
        with open(path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        entry1 = json.loads(lines[0])
        self.assertEqual(entry1["parent_task_id"], "task-1")
        self.assertEqual(entry1["from"], "dev")
        self.assertEqual(entry1["to"], "gatekeeper")
        entry2 = json.loads(lines[1])
        self.assertEqual(entry2["from"], "gatekeeper")
        self.assertEqual(entry2["to"], "tester")


class TestHandleDevComplete(unittest.TestCase):
    """Test handle_dev_complete with isolated gatekeeper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)
        # Mock _auto_archive
        self.orch._auto_archive = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_gate_pass_triggers_tester(self):
        """Gate pass should trigger tester, not coordinator eval."""
        ai_report = {
            "changed_files": ["agent/foo.py"],
            "target_files": [],
            "_chain_depth": 0,
        }
        # Mock _write_task_file to avoid filesystem
        self.orch._write_task_file = MagicMock()

        result = self.orch.handle_dev_complete(
            "task-1", "proj-1", "tok", 123, ai_report
        )
        self.assertIn("Gatekeeper PASS", result["reply"])
        # Should have written a test task file
        self.orch._write_task_file.assert_called()
        call_args = self.orch._write_task_file.call_args
        self.assertEqual(call_args[1].get("auto_triggered"), True)

    def test_gate_fail_retries_dev(self):
        """Gate fail should retry dev task when budget allows."""
        # Make evidence return no changes → gate fail
        self.orch.evidence_collector.collect_after_dev.return_value = _FakeEvidence([])
        ai_report = {
            "changed_files": [],
            "target_files": ["agent/foo.py"],
            "_chain_depth": 0,
        }
        self.orch._write_task_file = MagicMock()

        result = self.orch.handle_dev_complete(
            "task-1", "proj-1", "tok", 123, ai_report
        )
        self.assertIn("Gatekeeper FAIL", result["reply"])
        self.assertIn("retry", result["reply"].lower())

    def test_gate_fail_budget_exceeded(self):
        """Gate fail with exhausted budget should mark needs_review."""
        self.orch.evidence_collector.collect_after_dev.return_value = _FakeEvidence([])
        ai_report = {"changed_files": [], "_chain_depth": 0}
        self.orch._write_task_file = MagicMock()

        # Exhaust budget
        from task_orchestrator import RETRY_BUDGET
        for _ in range(RETRY_BUDGET):
            self.orch._increment_retry("task-1")

        result = self.orch.handle_dev_complete(
            "task-1", "proj-1", "tok", 123, ai_report
        )
        self.assertIn("budget exhausted", result["reply"].lower())

    def test_no_coordinator_eval_session(self):
        """Ensure no coordinator AI session is created (self-review removed)."""
        ai_report = {"changed_files": ["a.py"], "_chain_depth": 0}
        self.orch._write_task_file = MagicMock()

        self.orch.handle_dev_complete("task-1", "proj-1", "tok", 123, ai_report)
        # ai_manager.create_session should NOT be called
        self.orch.ai_manager.create_session.assert_not_called()


class TestTriggerTesterIdempotency(unittest.TestCase):
    """Test _trigger_tester idempotency."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)
        self.orch._write_task_file = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_first_call_creates_task(self):
        evidence = _FakeEvidence(["a.py"])
        self.orch._trigger_tester("task-1", "proj", "tok", 123, evidence)
        self.orch._write_task_file.assert_called_once()

    def test_second_call_skipped(self):
        evidence = _FakeEvidence(["a.py"])
        self.orch._trigger_tester("task-1", "proj", "tok", 123, evidence)
        self.orch._write_task_file.reset_mock()
        self.orch._trigger_tester("task-1", "proj", "tok", 123, evidence)
        self.orch._write_task_file.assert_not_called()


class TestTriggerQAIdempotency(unittest.TestCase):
    """Test _trigger_qa idempotency."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)
        self.orch._write_task_file = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_first_call_creates_task(self):
        self.orch._trigger_qa("task-1", "proj", "tok", 123, {"parent_task_id": "root-1"})
        self.orch._write_task_file.assert_called_once()

    def test_second_call_skipped(self):
        self.orch._trigger_qa("task-1", "proj", "tok", 123, {"parent_task_id": "root-1"})
        self.orch._write_task_file.reset_mock()
        self.orch._trigger_qa("task-1", "proj", "tok", 123, {"parent_task_id": "root-1"})
        self.orch._write_task_file.assert_not_called()


class TestChildTaskInheritance(unittest.TestCase):
    """Test that child tasks inherit workspace, project_id, parent_task_id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = _make_orchestrator(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    @patch("task_orchestrator.requests", create=True)
    def test_write_task_file_includes_metadata(self, mock_req):
        mock_req.post = MagicMock()
        # Patch workspace_registry
        with patch("task_orchestrator.resolve_workspace_for_task",
                    side_effect=ImportError, create=True):
            self.orch._write_task_file(
                "child-1", {"prompt": "test", "target_files": []},
                "proj-1", "tok", "test_task", 123,
                workspace="ws-main", parent_task_id="root-1",
                auto_triggered=True,
            )

        # Read the written file
        pending_dir = os.path.join(self.tmp.name, "codex-tasks", "pending")
        path = os.path.join(pending_dir, "child-1.json")
        self.assertTrue(os.path.exists(path))
        data = json.loads(open(path).read())
        self.assertEqual(data["workspace"], "ws-main")
        self.assertEqual(data["parent_task_id"], "root-1")
        self.assertTrue(data["metadata"]["auto_triggered"])
        self.assertEqual(data["metadata"]["parent_task_id"], "root-1")


if __name__ == "__main__":
    unittest.main()
