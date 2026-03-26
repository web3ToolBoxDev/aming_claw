"""Evidence Collector — Executor independently collects facts.

AI reports decisions. Executor collects evidence.
Evidence is trusted because it comes from code, not AI.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DevEvidence:
    """Evidence collected after a dev task."""
    changed_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    test_results: dict = field(default_factory=dict)
    diff_stat: str = ""
    collected_by: str = "executor_code"
    collected_at: str = ""

    def to_dict(self) -> dict:
        return {
            "changed_files": self.changed_files,
            "new_files": self.new_files,
            "deleted_files": self.deleted_files,
            "test_results": self.test_results,
            "diff_stat": self.diff_stat,
            "collected_by": self.collected_by,
            "collected_at": self.collected_at,
        }


class EvidenceCollector:
    """Independently collects execution evidence. Does not trust AI self-reports."""

    def __init__(self, workspace: str = "", project_id: str = ""):
        self.workspace = workspace or os.getenv("CODEX_WORKSPACE", os.getcwd())
        self.project_id = project_id

    def collect_before_snapshot(self) -> dict:
        """Take a snapshot before dev execution for later comparison."""
        return {
            "commit": self._git_head_commit(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def collect_after_dev(self, before_snapshot: dict) -> DevEvidence:
        """Collect evidence after dev AI completes. Independent of AI output."""
        evidence = DevEvidence(collected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        before_commit = before_snapshot.get("commit", "HEAD~1")

        # 1. Real changed files from git
        evidence.changed_files = self._git_changed_files(before_commit)

        # 2. Real new files from git
        evidence.new_files = self._git_new_files()

        # 3. Real deleted files
        evidence.deleted_files = self._git_deleted_files(before_commit)

        # 4. Diff stat
        evidence.diff_stat = self._git_diff_stat(before_commit)

        # 5. Test results (run actual tests)
        evidence.test_results = self._run_tests()

        return evidence

    def compare_with_ai_report(self, evidence: DevEvidence, ai_report: dict) -> dict:
        """Compare executor-collected evidence with AI self-report.

        Returns discrepancies for audit logging.
        """
        discrepancies = []

        # Changed files mismatch
        ai_files = set(ai_report.get("changed_files", []))
        real_files = set(evidence.changed_files)
        if ai_files != real_files:
            extra_in_ai = ai_files - real_files
            missing_in_ai = real_files - ai_files
            if extra_in_ai:
                discrepancies.append({
                    "field": "changed_files",
                    "issue": "ai_reported_but_not_changed",
                    "files": list(extra_in_ai),
                })
            if missing_in_ai:
                discrepancies.append({
                    "field": "changed_files",
                    "issue": "changed_but_ai_not_reported",
                    "files": list(missing_in_ai),
                })

        # Test results mismatch
        ai_test_passed = ai_report.get("test_results", {}).get("passed", -1)
        real_test_passed = evidence.test_results.get("passed", -1)
        if ai_test_passed != -1 and real_test_passed != -1:
            if ai_test_passed != real_test_passed:
                discrepancies.append({
                    "field": "test_results",
                    "issue": "test_count_mismatch",
                    "ai_reported": ai_test_passed,
                    "actual": real_test_passed,
                })

        return {
            "has_discrepancies": len(discrepancies) > 0,
            "discrepancies": discrepancies,
            "trust_source": "executor_code",
        }

    # ── Git operations ──

    def _run_git(self, *args) -> str:
        try:
            result = subprocess.run(
                ["git"] + list(args),
                capture_output=True, text=True, cwd=self.workspace, timeout=30,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _git_head_commit(self) -> str:
        return self._run_git("rev-parse", "HEAD")

    def _git_changed_files(self, since_commit: str) -> list[str]:
        output = self._run_git("diff", "--name-only", since_commit)
        return [f for f in output.splitlines() if f.strip()]

    def _git_new_files(self) -> list[str]:
        output = self._run_git("ls-files", "--others", "--exclude-standard")
        return [f for f in output.splitlines() if f.strip()]

    def _git_deleted_files(self, since_commit: str) -> list[str]:
        output = self._run_git("diff", "--diff-filter=D", "--name-only", since_commit)
        return [f for f in output.splitlines() if f.strip()]

    def _git_diff_stat(self, since_commit: str) -> str:
        return self._run_git("diff", "--stat", since_commit)

    # ── Test runner ──

    def _run_tests(self) -> dict:
        """Run project tests and capture results."""
        # Resolve test command from project config, fallback to env/default
        default_cmd = "python -m pytest -q"
        if self.project_id:
            try:
                from project_config import get_test_command
                default_cmd = get_test_command(self.project_id) or default_cmd
            except ImportError:
                pass
        test_cmd = os.getenv("SAFE_TEST_COMMAND", default_cmd)
        try:
            result = subprocess.run(
                test_cmd.split(),
                capture_output=True, text=True, cwd=self.workspace, timeout=300,
            )
            return {
                "exit_code": result.returncode,
                "passed": result.returncode == 0,
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else "",
                "command": test_cmd,
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "passed": False, "error": "timeout"}
        except Exception as e:
            return {"exit_code": -1, "passed": False, "error": str(e)[:200]}
