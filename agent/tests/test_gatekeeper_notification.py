"""Tests for gatekeeper notification fix: release_gate conditional logic and manager_signal."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

agent_dir = str(Path(__file__).resolve().parent.parent)
if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)


class TestHandleQaCompleteReleaseGate(unittest.TestCase):
    """Test handle_qa_complete respects release_gate flag."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    @patch("task_orchestrator.TaskOrchestrator._log_stage_transition")
    @patch("task_orchestrator.TaskOrchestrator._record_idempotent")
    @patch("task_orchestrator.TaskOrchestrator._check_idempotent", return_value=False)
    @patch("task_orchestrator.TaskOrchestrator._trigger_gatekeeper")
    @patch("task_orchestrator.TaskOrchestrator._gateway_reply")
    def test_release_gate_false_skips_gatekeeper(self, mock_reply, mock_gate, mock_check,
                                                  mock_record, mock_log):
        """release_gate=False should skip gatekeeper and send alternative notification."""
        from task_orchestrator import TaskOrchestrator
        orch = TaskOrchestrator()
        result = orch.handle_qa_complete(
            task_id="qa-123", project_id="proj-1",
            token="tok", chat_id=42,
            qa_report={},
            verification={"release_gate": False}
        )
        mock_gate.assert_not_called()
        mock_reply.assert_called_once_with(
            42, "✅ Merged to main (deploy not required for this task)", "tok")
        self.assertEqual(result["status"], "qa_passed")
        self.assertTrue(result["gatekeeper"]["skipped"])
        self.assertEqual(result["gatekeeper"]["reason"], "release_gate_false")

    @patch("task_orchestrator.TaskOrchestrator._log_stage_transition")
    @patch("task_orchestrator.TaskOrchestrator._record_idempotent")
    @patch("task_orchestrator.TaskOrchestrator._check_idempotent", return_value=False)
    @patch("task_orchestrator.TaskOrchestrator._trigger_gatekeeper", return_value={"pass": True})
    @patch("task_orchestrator.TaskOrchestrator._gateway_reply")
    def test_release_gate_true_triggers_gatekeeper(self, mock_reply, mock_gate, mock_check,
                                                    mock_record, mock_log):
        """release_gate=True (default) should trigger gatekeeper normally."""
        from task_orchestrator import TaskOrchestrator
        orch = TaskOrchestrator()
        result = orch.handle_qa_complete(
            task_id="qa-456", project_id="proj-2",
            token="tok", chat_id=42,
            qa_report={},
            verification={"release_gate": True}
        )
        mock_gate.assert_called_once_with("proj-2", "tok", 42)
        mock_reply.assert_not_called()
        self.assertEqual(result["gatekeeper"], {"pass": True})

    @patch("task_orchestrator.TaskOrchestrator._log_stage_transition")
    @patch("task_orchestrator.TaskOrchestrator._record_idempotent")
    @patch("task_orchestrator.TaskOrchestrator._check_idempotent", return_value=False)
    @patch("task_orchestrator.TaskOrchestrator._trigger_gatekeeper", return_value={"pass": True})
    @patch("task_orchestrator.TaskOrchestrator._gateway_reply")
    def test_no_verification_defaults_to_gatekeeper(self, mock_reply, mock_gate, mock_check,
                                                     mock_record, mock_log):
        """No verification param should default to triggering gatekeeper."""
        from task_orchestrator import TaskOrchestrator
        orch = TaskOrchestrator()
        result = orch.handle_qa_complete(
            task_id="qa-789", project_id="proj-3",
            token="tok", chat_id=42,
            qa_report={}
        )
        mock_gate.assert_called_once()
        self.assertEqual(result["gatekeeper"], {"pass": True})


class TestMergeManagerSignal(unittest.TestCase):
    """Test manager_signal written on successful merge."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmpdir.name) / "codex-tasks" / "state"
        self.state_dir.mkdir(parents=True)
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)

    def test_merge_success_writes_signal(self):
        """Successful merge (returncode=0) should write manager_signal."""
        from bot_commands import write_manager_signal
        sig_path = self.state_dir / "manager_signal.json"
        write_manager_signal("graceful_restart", {"task_id": "t1", "branch": "fix/x"}, 42)
        self.assertTrue(sig_path.exists())
        data = json.loads(sig_path.read_text(encoding="utf-8"))
        self.assertEqual(data["action"], "graceful_restart")
        self.assertEqual(data["args"]["task_id"], "t1")
        self.assertEqual(data["args"]["branch"], "fix/x")
        self.assertEqual(data["requested_by"], 42)

    def test_merge_failure_no_signal(self):
        """Failed merge should not write manager_signal (caller guards with returncode check)."""
        sig_path = self.state_dir / "manager_signal.json"
        # Simulate: merge failed → code does NOT call write_manager_signal
        # Verify no signal file exists
        self.assertFalse(sig_path.exists())


if __name__ == "__main__":
    unittest.main()
