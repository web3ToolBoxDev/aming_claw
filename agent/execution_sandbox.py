"""Execution Sandbox — Isolate AI code execution.

Provides workspace isolation, command whitelist, and approval flow.
"""

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Command whitelist (auto-allowed without approval)
COMMAND_WHITELIST = {
    "git diff", "git status", "git log", "git show", "git branch",
    "python -m pytest", "python -m unittest", "pytest", "npm test", "npm run test",
    "ls", "cat", "head", "tail", "wc", "find", "grep",
    "python -c", "node -e",
}

# Commands needing human approval
APPROVAL_REQUIRED = {
    "git push", "git reset", "git checkout --",
    "docker compose down", "docker rm", "docker rmi",
    "npm publish", "pip install", "npm install",
    "rm -rf", "del /s",
}

# Always denied
ALWAYS_DENY = {
    "rm -rf /", "rm -rf ~", "format", "mkfs",
    "shutdown", "reboot", "halt",
    "curl.*api_key", "wget.*token",
}


class ExecutionSandbox:
    """Provides isolated execution environment for AI tasks."""

    def __init__(self, base_workspace: str = ""):
        self.base_workspace = base_workspace or os.getenv("CODEX_WORKSPACE", os.getcwd())

    def create_workspace(self, task_id: str) -> str:
        """Create isolated workspace for a task.

        Currently uses the shared workspace (same git repo).
        Future: overlay filesystem or separate clone.
        """
        # For now, use the same workspace but track task association
        workspace = self.base_workspace
        log.info("Sandbox: task %s using workspace %s", task_id, workspace)
        return workspace

    def check_command(self, command: str) -> tuple[str, str]:
        """Check command against security policy.

        Returns:
            (decision: "allow"|"approval"|"deny", reason: str)
        """
        cmd_lower = command.lower().strip()

        # Always deny
        for pattern in ALWAYS_DENY:
            if re.search(pattern, cmd_lower):
                return "deny", f"命令被永久禁止: {pattern}"

        # Needs approval
        for pattern in APPROVAL_REQUIRED:
            if pattern in cmd_lower:
                return "approval", f"命令需要人工确认: {pattern}"

        # Whitelist
        for pattern in COMMAND_WHITELIST:
            if cmd_lower.startswith(pattern):
                return "allow", "whitelisted"

        # Default: allow but log
        log.info("Sandbox: command not in whitelist, allowing: %s", cmd_lower[:50])
        return "allow", "not_in_whitelist_but_allowed"

    def check_file_access(self, filepath: str, operation: str = "read") -> tuple[bool, str]:
        """Check if AI can access a file.

        Args:
            filepath: Path to check
            operation: "read" or "write"
        """
        # Normalize path
        fp = Path(filepath).resolve()
        ws = Path(self.base_workspace).resolve()

        # Must be within workspace
        try:
            fp.relative_to(ws)
        except ValueError:
            return False, f"文件不在工作区内: {filepath}"

        # Sensitive paths
        sensitive = {".ssh", ".aws", ".gnupg", ".env", "credentials", "secrets"}
        for part in fp.parts:
            if part.lower() in sensitive:
                return False, f"敏感路径: {part}"

        return True, "ok"

    def validate_changed_files(self, changed_files: list[str]) -> list[str]:
        """Validate AI didn't modify files outside workspace.

        Returns list of violations.
        """
        violations = []
        ws = Path(self.base_workspace).resolve()

        for f in changed_files:
            fp = Path(f)
            if fp.is_absolute():
                try:
                    fp.relative_to(ws)
                except ValueError:
                    violations.append(f"文件在工作区外: {f}")

            # Check for sensitive file modifications
            ok, reason = self.check_file_access(str(fp), "write")
            if not ok:
                violations.append(reason)

        return violations
