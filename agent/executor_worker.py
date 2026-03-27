# v2: git-diff verified, artifact-filtered.
# Verified: executor self-fix bootstrap successful.
"""Executor Worker — polls Governance API for tasks and executes them via Claude CLI.

This is the missing link between:
  - Governance task queue (create/claim/complete)
  - AI execution (ai_lifecycle.py → Claude CLI)

Flow:
  1. Poll: GET /api/task/{project}/list?status=queued
  2. Claim: POST /api/task/{project}/claim
  3. Execute: AILifecycleManager.create_session(role, prompt)
  4. Report: POST /api/task/{project}/progress
  5. Complete: POST /api/task/{project}/complete (triggers auto-chain)

Usage:
  python -m agent.executor_worker --project aming-claw
  GOVERNANCE_URL=http://localhost:40006 python -m agent.executor_worker

Full chain verified: dev→test→qa→merge→deploy.
"""

import json
import logging
import os
import sys
import time
import argparse
import threading
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger("executor_worker")

# --- Configuration ---

GOVERNANCE_URL = os.getenv("GOVERNANCE_URL", "http://localhost:40006")
POLL_INTERVAL = int(os.getenv("EXECUTOR_POLL_INTERVAL", "10"))
WORKER_ID = os.getenv("EXECUTOR_WORKER_ID", f"executor-{os.getpid()}")
WORKSPACE = os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parents[1]))

# Task type → (role, timeout_sec)
TASK_ROLE_MAP = {
    "pm":    ("coordinator", 120),
    "dev":   ("dev", 300),
    "test":  ("tester", 180),
    "qa":    ("qa", 120),
    "merge": ("script", 30),  # handled by _execute_merge, no AI
    "task":  ("dev", 300),
}
# Merge is script-based, see _execute_merge()


class ExecutorWorker:
    """Polls governance API, claims tasks, executes via Claude CLI."""

    def __init__(self, project_id: str, governance_url: str = GOVERNANCE_URL,
                 worker_id: str = WORKER_ID, workspace: str = WORKSPACE):
        self.project_id = project_id
        self.base_url = governance_url.rstrip("/")
        self.worker_id = worker_id
        self.workspace = workspace
        self._running = False
        self._current_task = None
        self._lifecycle = None

    def _api(self, method: str, path: str, data: dict = None) -> dict:
        """Call governance API."""
        import requests
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                r = requests.get(url, timeout=10)
            else:
                r = requests.post(url, json=data or {}, timeout=30,
                                  headers={"Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("API call failed: %s %s → %s", method, path, e)
            return {"error": str(e)}

    def _claim_task(self) -> dict | None:
        """Try to claim next queued task."""
        result = self._api("POST", f"/api/task/{self.project_id}/claim",
                           {"worker_id": self.worker_id})
        if "error" in result or "task" not in result:
            return None
        task_pair = result["task"]
        if not task_pair or not isinstance(task_pair, list) or len(task_pair) < 2:
            return None
        task_data, fence_token = task_pair
        if not task_data or not isinstance(task_data, dict):
            return None
        task_data["_fence_token"] = fence_token
        return task_data

    def _report_progress(self, task_id: str, progress: dict):
        """Report execution progress."""
        self._api("POST", f"/api/task/{self.project_id}/progress",
                  {"task_id": task_id, "progress": progress})

    def _complete_task(self, task_id: str, status: str, result: dict) -> dict:
        """Mark task complete (triggers auto-chain)."""
        return self._api("POST", f"/api/task/{self.project_id}/complete",
                         {"task_id": task_id, "status": status, "result": result})

    def _execute_task(self, task: dict) -> dict:
        """Execute a single task via Claude CLI."""
        task_id = task["task_id"]
        task_type = task.get("type", "task")
        prompt = task.get("prompt", "")
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        role, timeout = TASK_ROLE_MAP.get(task_type, ("dev", 300))

        log.info("Executing %s (type=%s, role=%s, timeout=%ds)",
                 task_id, task_type, role, timeout)
        self._report_progress(task_id, {"step": "starting", "role": role})

        # Merge is a script operation, not AI
        if task_type == "merge":
            return self._execute_merge(task_id, metadata)

        # Build context for AI session
        context = {
            "task_id": task_id,
            "task_type": task_type,
            "project_id": self.project_id,
            "target_files": metadata.get("target_files", []),
            "changed_files": metadata.get("changed_files", []),
            "related_nodes": metadata.get("related_nodes", []),
            "attempt_num": task.get("attempt_num", 1),
        }

        # Enhance prompt with governance context
        enhanced_prompt = self._build_prompt(prompt, task_type, context)

        # Create AI session
        if self._lifecycle is None:
            from ai_lifecycle import AILifecycleManager
            self._lifecycle = AILifecycleManager()

        session = self._lifecycle.create_session(
            role=role,
            prompt=enhanced_prompt,
            context=context,
            project_id=self.project_id,
            timeout_sec=timeout,
            workspace=self.workspace,
        )

        if session.status == "failed":
            return {"status": "failed", "error": session.stderr}

        # Wait for completion with progress reporting
        self._report_progress(task_id, {"step": "running", "session_id": session.session_id})
        output = self._lifecycle.wait_for_output(session.session_id)

        if session.status == "timeout":
            return {"status": "failed", "error": f"Timeout after {timeout}s"}
        if session.status == "failed":
            return {"status": "failed", "error": session.stderr[:500]}

        # Detect actually changed files via git diff
        changed_files = self._get_git_changed_files()

        # Stage changed files if any
        if changed_files:
            try:
                import subprocess
                subprocess.run(
                    ["git", "add", "--"] + changed_files,
                    cwd=self.workspace,
                    capture_output=True,
                    timeout=30,
                )
                log.info("Staged %d changed file(s): %s", len(changed_files), changed_files)
            except Exception as e:
                log.warning("git add failed: %s", e)

        # Parse output and merge with real changed_files
        result = self._parse_output(session, task_type)

        # Always overwrite/set changed_files from git diff (ground truth)
        if changed_files:
            result["changed_files"] = changed_files
        elif "changed_files" not in result:
            result["changed_files"] = []

        return {"status": "succeeded", "result": result}

    def _execute_merge(self, task_id: str, metadata: dict) -> dict:
        """Merge is a script operation: git add → git commit. No AI needed."""
        import subprocess

        changed = metadata.get("changed_files", [])
        self._report_progress(task_id, {"step": "merging"})

        try:
            # Stage changed files (or all if none specified)
            if changed:
                subprocess.run(["git", "add", "--"] + changed,
                               cwd=self.workspace, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "add", "-A"],
                               cwd=self.workspace, capture_output=True, timeout=30)

            # Check if there's anything to commit
            status = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                    cwd=self.workspace, capture_output=True, text=True, timeout=10)
            staged = [f.strip() for f in status.stdout.splitlines() if f.strip()]

            if not staged:
                return {"status": "succeeded", "result": {
                    "merge_commit": "none", "branch": "main",
                    "files_changed": 0, "note": "nothing to commit"
                }}

            # Commit
            msg = f"Auto-merge: {task_id}\n\nChanged files: {', '.join(staged[:10])}"
            proc = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=self.workspace, capture_output=True, text=True, timeout=30)

            if proc.returncode != 0:
                return {"status": "failed", "error": f"git commit failed: {proc.stderr[:300]}"}

            # Get commit hash
            rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=self.workspace, capture_output=True, text=True, timeout=5)
            commit_hash = rev.stdout.strip()

            log.info("Merge complete: %s (%d files)", commit_hash, len(staged))
            return {"status": "succeeded", "result": {
                "merge_commit": commit_hash,
                "branch": "main",
                "files_changed": len(staged),
                "changed_files": staged,
            }}

        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _build_prompt(self, prompt: str, task_type: str, context: dict) -> str:
        """Enhance task prompt with governance context."""
        parts = [prompt]

        if task_type == "pm":
            parts.append(
                "\nAnalyze the request and output a PRD as JSON with the following fields: "
                "{\"target_files\": [\"...\"], \"verification\": {\"method\": \"...\", \"command\": \"...\"}, "
                "\"acceptance_criteria\": [\"...\"]}"
            )

        elif task_type == "test":
            changed = context.get("changed_files", [])
            parts.append(f"\nRun tests. Changed files: {json.dumps(changed)}")
            parts.append("Report result as JSON: {\"test_report\": {\"passed\": N, \"failed\": N, \"tool\": \"pytest\"}}")

        elif task_type == "qa":
            parts.append("\nYou are a QA reviewer. Review the test results and changed files above.")
            parts.append("If tests passed and changes look reasonable, respond ONLY with this exact JSON:")
            parts.append('{"recommendation": "qa_pass", "review_summary": "Tests pass, changes approved"}')
            parts.append("If there are critical issues, respond with:")
            parts.append('{"recommendation": "reject", "reason": "description of issue"}')

        elif task_type == "merge":
            parts.append("\nCommit all staged changes to git and respond with JSON: {\"merge_commit\": \"<hash>\", \"branch\": \"main\", \"files_changed\": N}")

        elif task_type in ("dev", "task"):
            target = context.get("target_files", [])
            if target:
                parts.append(f"\nTarget files: {json.dumps(target)}")
            parts.append("\nAfter completing, respond with JSON: {\"changed_files\": [...], \"summary\": \"...\"}")

        return "\n".join(parts)

    # Files/patterns to ignore in git diff (Claude CLI artifacts, not real changes).
    # executor_worker.py is intentionally excluded — it IS the executor itself, not a task deliverable.
    #
    # Node mapping (file → acceptance-graph node):
    #   executor_worker.py        → L3.2  ExecutorWorker (this file — executor process)
    #   governance/auto_chain.py  → L2.1  AutoChain      (stage-transition dispatcher)
    #   governance/graph.py       → L1.3  AcceptanceGraph (DAG rule layer)
    #   governance/task_registry.py → L2.2 TaskRegistry  (task CRUD / queue)
    _IGNORE_PATTERNS = {".claude/", "__pycache__/", ".pyc", ".lock", "executor_worker.py"}

    def _get_git_changed_files(self) -> list:
        """Run git diff --name-only to detect files changed since last commit."""
        try:
            import subprocess
            # Check both staged and unstaged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=15,
            )
            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]

            # Also include untracked files that are new (not yet in HEAD)
            result2 = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=A", "--cached"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=15,
            )
            new_files = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

            # Merge, preserving order, dedup
            seen = set(files)
            for f in new_files:
                if f not in seen:
                    files.append(f)
                    seen.add(f)

            # Filter out Claude artifacts and non-code files
            files = [f for f in files
                     if not any(p in f for p in self._IGNORE_PATTERNS)]

            return files
        except Exception as e:
            log.warning("git diff failed: %s", e)
            return []

    def _parse_output(self, session, task_type: str) -> dict:
        """Parse AI session output into structured result, handling non-JSON gracefully."""
        stdout = session.stdout or ""

        # Try to extract JSON from markdown code blocks first (```json ... ```)
        import re
        code_blocks = re.findall(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', stdout)
        for block in reversed(code_blocks):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

        # Fallback: find last JSON object from raw output
        parsed = None
        for candidate in reversed(list(re.finditer(r'\{', stdout))):
            start = candidate.start()
            depth = 0
            end = None
            for i, ch in enumerate(stdout[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is not None:
                try:
                    obj = json.loads(stdout[start:end])
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except json.JSONDecodeError:
                    continue

        if parsed is not None:
            return parsed

        # Fallback: return raw output as summary (non-JSON output is acceptable)
        summary = stdout.strip()
        if not summary:
            summary = "(no output)"
        return {
            "summary": summary[:1000],
            "exit_code": getattr(session, "exit_code", None),
        }

    def run_once(self) -> bool:
        """Try to claim and execute one task. Returns True if a task was processed."""
        task = self._claim_task()
        if not task:
            return False

        task_id = task["task_id"]
        self._current_task = task_id
        log.info("Claimed task: %s", task_id)

        try:
            outcome = self._execute_task(task)
            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})

            completion = self._complete_task(task_id, status, result)
            chain = completion.get("auto_chain", {})

            if chain.get("gate_blocked"):
                log.warning("Gate blocked after %s: %s", task_id, chain["reason"])
            elif chain.get("task_id"):
                log.info("Auto-chain: %s → %s (%s)", task_id, chain["task_id"], chain.get("type"))
            elif chain.get("deploy"):
                log.info("Deploy triggered from %s: %s", task_id, chain["deploy"])

            return True

        except Exception as e:
            log.error("Task %s execution failed: %s", task_id, e, exc_info=True)
            self._complete_task(task_id, "failed", {"error": str(e)})
            return True
        finally:
            self._current_task = None

    def run_loop(self):
        """Main polling loop."""
        self._running = True
        log.info("Executor worker started: project=%s, worker=%s, poll=%ds",
                 self.project_id, self.worker_id, POLL_INTERVAL)
        log.info("Governance: %s | Workspace: %s", self.base_url, self.workspace)

        # Verify governance is reachable
        health = self._api("GET", "/api/health")
        if "error" in health:
            log.error("Cannot reach governance at %s", self.base_url)
            return
        log.info("Governance: v%s (PID %s)", health.get("version", "?"), health.get("pid", "?"))

        while self._running:
            try:
                processed = self.run_once()
                if not processed:
                    time.sleep(POLL_INTERVAL)
                else:
                    time.sleep(1)  # Brief pause between tasks
            except KeyboardInterrupt:
                log.info("Shutting down...")
                self._running = False
            except Exception as e:
                log.error("Poll loop error: %s", e, exc_info=True)
                time.sleep(POLL_INTERVAL)

    def stop(self):
        """Stop the polling loop."""
        self._running = False


def main():
    parser = argparse.ArgumentParser(description="Executor Worker - polls governance for tasks")
    parser.add_argument("--project", "-p", default=os.getenv("PROJECT_ID", "aming-claw"),
                        help="Project ID to poll tasks from")
    parser.add_argument("--url", default=GOVERNANCE_URL,
                        help="Governance API URL")
    parser.add_argument("--worker-id", default=WORKER_ID,
                        help="Worker identifier")
    parser.add_argument("--workspace", default=WORKSPACE,
                        help="Working directory for task execution")
    parser.add_argument("--once", action="store_true",
                        help="Execute one task and exit (no loop)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    worker = ExecutorWorker(
        project_id=args.project,
        governance_url=args.url,
        worker_id=args.worker_id,
        workspace=args.workspace,
    )

    if args.once:
        worker.run_once()
    else:
        worker.run_loop()


if __name__ == "__main__":
    main()
