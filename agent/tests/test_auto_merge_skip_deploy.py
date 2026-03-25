"""Tests for auto-merge skip-deploy logic in process_qa_task_v6.

Covers:
  - release_gate=False → merge_args includes --skip-deploy
  - governance_nodes=False → merge_args includes --skip-deploy (unchanged behavior)
  - both True → no --skip-deploy
  - release_gate=False + merge success → manager signal written
  - release_gate=False + merge success → Telegram notification says "deploy not required"
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from executor import process_qa_task_v6


def _make_qa_task(task_id="test-qa-001", branch="dev/test-branch",
                  verification=None, chat_id=12345):
    """Build a minimal QA task dict for testing."""
    task = {
        "task_id": task_id,
        "project_id": "proj-1",
        "chat_id": chat_id,
        "_branch": branch,
        "_gov_token": "fake-token",
        "_verification": verification or {},
    }
    return task


class TestAutoMergeSkipDeploy(unittest.TestCase):
    """Verify skip-deploy flag is passed to merge-and-deploy.sh correctly."""

    def _run_with_verification(self, verification, merge_returncode=0):
        """Run process_qa_task_v6 with given verification config.

        Mocks subprocess.run so we can inspect the merge_args without
        executing real scripts.  Returns (captured_merge_args, result).
        """
        task = _make_qa_task(verification=verification)

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["SHARED_VOLUME_PATH"] = tmpdir
            processing = Path(tmpdir) / "codex-tasks" / "processing"
            processing.mkdir(parents=True)
            proc_file = processing / "test-qa-001.json"
            proc_file.write_text(json.dumps(task))

            captured_args = {}

            def fake_subprocess_run(args, **kwargs):
                mock_result = MagicMock()
                cmd_str = " ".join(str(a) for a in args)
                if "verify_loop" in cmd_str:
                    mock_result.stdout = "0 fail"
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "gatekeeper" in cmd_str:
                    mock_result.stdout = "status: completed"
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "merge-and-deploy" in cmd_str:
                    captured_args["merge"] = list(args)
                    mock_result.stdout = "merged ok"
                    mock_result.stderr = ""
                    mock_result.returncode = merge_returncode
                else:
                    mock_result.stdout = ""
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                return mock_result

            # Mock gatekeeper HTTP call to return pass
            mock_gate_response = MagicMock()
            mock_gate_response.json.return_value = {
                "release": True,
                "gatekeeper": {"pass": True},
            }

            with patch("executor.subprocess.run", side_effect=fake_subprocess_run), \
                 patch("executor._gateway_notify") as mock_notify, \
                 patch("bot_commands.write_manager_signal") as mock_signal, \
                 patch("executor.save_json"), \
                 patch("executor.requests.get", return_value=mock_gate_response):
                result = process_qa_task_v6(task, proc_file)

            return captured_args.get("merge", []), mock_notify, mock_signal, result

    def test_release_gate_false_adds_skip_deploy(self):
        """T1: release_gate=False → --skip-deploy present."""
        verification = {"release_gate": False, "governance_nodes": True}
        merge_args, _, _, _ = self._run_with_verification(verification)
        self.assertIn("--skip-deploy", merge_args,
                       "Expected --skip-deploy when release_gate=False")

    def test_governance_nodes_false_adds_skip_deploy(self):
        """Unchanged behavior: governance_nodes=False → --skip-deploy."""
        verification = {"release_gate": True, "governance_nodes": False}
        merge_args, _, _, _ = self._run_with_verification(verification)
        self.assertIn("--skip-deploy", merge_args)

    def test_both_true_no_skip_deploy(self):
        """Both True → no --skip-deploy."""
        verification = {"release_gate": True, "governance_nodes": True}
        merge_args, _, _, _ = self._run_with_verification(verification)
        self.assertNotIn("--skip-deploy", merge_args)

    def test_release_gate_false_signal_written(self):
        """T2: release_gate=False + merge OK → manager signal written."""
        verification = {"release_gate": False, "governance_nodes": True}
        _, _, mock_signal, _ = self._run_with_verification(verification, merge_returncode=0)
        mock_signal.assert_called_once()
        args, kwargs = mock_signal.call_args
        self.assertEqual(args[0], "graceful_restart")
        self.assertEqual(args[1]["task_id"], "test-qa-001")

    def test_release_gate_false_notification_text(self):
        """T3: release_gate=False + merge OK → correct Telegram message."""
        verification = {"release_gate": False, "governance_nodes": True}
        _, mock_notify, _, _ = self._run_with_verification(verification, merge_returncode=0)
        # Find the merge notification call (not the QA status call)
        merge_calls = [c for c in mock_notify.call_args_list
                       if "Merged to main" in str(c)]
        self.assertEqual(len(merge_calls), 1,
                         f"Expected one 'Merged to main' notification, got: {mock_notify.call_args_list}")
        msg = merge_calls[0][0][1]
        self.assertIn("deploy not required", msg)

    def test_merge_fail_no_signal(self):
        """Merge failure → no signal written regardless of skip_deploy."""
        verification = {"release_gate": False, "governance_nodes": True}
        _, _, mock_signal, _ = self._run_with_verification(verification, merge_returncode=1)
        mock_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
