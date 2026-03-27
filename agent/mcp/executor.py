"""Worker Pool — manages Claude CLI workers for task execution.

Each WorkerSlot runs in a thread, polling the governance API for tasks,
spawning Claude CLI to execute, and reporting results back.

The pool is managed by the MCP server.  On shutdown (EOF on stdin), all
workers are stopped and running Claude CLI subprocesses are terminated.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger(__name__)

# Task type → (role, timeout_sec)
TASK_ROLE_MAP = {
    "pm":    ("coordinator", 120),
    "dev":   ("dev", 300),
    "test":  ("tester", 180),
    "qa":    ("qa", 120),
    "merge": ("script", 30),
    "task":  ("dev", 300),
}

POLL_INTERVAL = int(os.getenv("EXECUTOR_POLL_INTERVAL", "10"))


class WorkerSlot:
    """One worker that claims and executes tasks."""

    def __init__(self, slot_id: str, pool: "WorkerPool"):
        self.slot_id = slot_id
        self.pool = pool
        self._running = False
        self._thread: threading.Thread | None = None
        self._current_task: str | None = None
        self._cli_proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"worker-{self.slot_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def kill_cli(self) -> None:
        """Terminate running Claude CLI process if any."""
        proc = self._cli_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @property
    def status(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "running": self._running,
            "current_task": self._current_task,
            "thread_alive": self._thread.is_alive() if self._thread else False,
        }

    def _run_loop(self) -> None:
        """Poll → claim → execute → complete → repeat."""
        while self._running:
            try:
                task = self._claim()
                if task:
                    self._execute(task)
                else:
                    time.sleep(POLL_INTERVAL)
            except Exception:
                log.exception("Worker %s: unhandled error", self.slot_id)
                time.sleep(POLL_INTERVAL)

    def _claim(self) -> dict | None:
        result = self.pool.api("POST", f"/api/task/{self.pool.project_id}/claim",
                               {"worker_id": f"mcp-{self.slot_id}"})
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

    def _execute(self, task: dict) -> None:
        task_id = task["task_id"]
        task_type = task.get("type", "task")
        prompt = task.get("prompt", "")
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        self._current_task = task_id
        role, timeout = TASK_ROLE_MAP.get(task_type, ("dev", 300))
        log.info("[%s] Executing %s (type=%s, role=%s)", self.slot_id, task_id, task_type, role)

        try:
            # Merge is script-based
            if task_type == "merge":
                outcome = self._execute_merge(task_id, metadata)
            else:
                outcome = self._execute_ai(task_id, task_type, prompt, metadata, role, timeout)

            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})

            completion = self.pool.api(
                "POST", f"/api/task/{self.pool.project_id}/complete",
                {"task_id": task_id, "status": status, "result": result},
            )

            chain = completion.get("auto_chain", {})
            if chain.get("gate_blocked"):
                log.warning("[%s] Gate blocked: %s", self.slot_id, chain.get("reason", ""))
                self.pool.on_event("gate.blocked", {
                    "task_id": task_id, "stage": task_type,
                    "reason": chain.get("reason", ""),
                })
            elif chain.get("task_id"):
                log.info("[%s] Auto-chain: %s → %s (%s)",
                         self.slot_id, task_id, chain["task_id"], chain.get("type", ""))
                self.pool.on_event("task.created", {
                    "task_id": chain["task_id"], "type": chain.get("type", ""),
                    "parent": task_id,
                })

        except Exception:
            log.exception("[%s] Execute failed: %s", self.slot_id, task_id)
            self.pool.api(
                "POST", f"/api/task/{self.pool.project_id}/complete",
                {"task_id": task_id, "status": "failed", "result": {"error": "executor exception"}},
            )
        finally:
            self._current_task = None
            self._cli_proc = None

    def _execute_ai(self, task_id, task_type, prompt, metadata, role, timeout) -> dict:
        """Execute via Claude CLI."""
        from ai_lifecycle import AILifecycleManager

        context = {
            "task_id": task_id,
            "task_type": task_type,
            "project_id": self.pool.project_id,
            "target_files": metadata.get("target_files", []),
            "changed_files": metadata.get("changed_files", []),
            "related_nodes": metadata.get("related_nodes", []),
        }

        enhanced_prompt = self._build_prompt(prompt, task_type, context)
        lifecycle = AILifecycleManager()
        session = lifecycle.create_session(
            role=role,
            prompt=enhanced_prompt,
            context=context,
            project_id=self.pool.project_id,
            timeout_sec=timeout,
            workspace=self.pool.workspace,
        )

        if session.status == "failed":
            return {"status": "failed", "error": session.stderr}

        # Track the subprocess for clean shutdown
        self._cli_proc = getattr(session, '_proc', None)

        lifecycle.wait_for_output(session.session_id)

        if session.status in ("timeout", "failed"):
            return {"status": "failed", "error": session.stderr[:500] if session.stderr else f"{session.status}"}

        # Git diff for ground truth
        changed_files = self._get_git_changed()
        if changed_files:
            try:
                subprocess.run(
                    ["git", "add", "--"] + changed_files,
                    cwd=self.pool.workspace, capture_output=True, timeout=30,
                )
            except Exception:
                pass

        result = self._parse_output(session, task_type)
        if changed_files:
            result["changed_files"] = changed_files
        elif "changed_files" not in result:
            result["changed_files"] = []

        return {"status": "succeeded", "result": result}

    def _execute_merge(self, task_id: str, metadata: dict) -> dict:
        """Git add + commit. No AI."""
        changed = metadata.get("changed_files", [])
        try:
            ws = self.pool.workspace
            if changed:
                subprocess.run(["git", "add", "--"] + changed,
                               cwd=ws, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "add", "-A"],
                               cwd=ws, capture_output=True, timeout=30)

            status = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                    cwd=ws, capture_output=True, text=True, timeout=10)
            staged = [f.strip() for f in status.stdout.splitlines() if f.strip()]

            if not staged:
                return {"status": "succeeded", "result": {
                    "merge_commit": "none", "branch": "main",
                    "files_changed": 0, "note": "nothing to commit",
                }}

            msg = f"Auto-merge: {task_id}\n\nChanged: {', '.join(staged[:10])}"
            proc = subprocess.run(["git", "commit", "-m", msg],
                                  cwd=ws, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return {"status": "failed", "error": f"git commit failed: {proc.stderr[:300]}"}

            rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=ws, capture_output=True, text=True, timeout=5)
            return {"status": "succeeded", "result": {
                "merge_commit": rev.stdout.strip(),
                "branch": "main",
                "files_changed": len(staged),
                "changed_files": staged,
            }}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _get_git_changed(self) -> list[str]:
        """Detect changed files via git diff."""
        ignore = {".claude/", "__pycache__/"}
        files = set()
        try:
            ws = self.pool.workspace
            for cmd in [
                ["git", "diff", "--name-only", "HEAD"],
                ["git", "diff", "--name-only", "--diff-filter=A", "--cached"],
            ]:
                proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=10)
                for f in proc.stdout.splitlines():
                    f = f.strip()
                    if f and not any(f.startswith(p) for p in ignore):
                        files.add(f)
        except Exception:
            pass
        return sorted(files)

    def _parse_output(self, session, task_type: str) -> dict:
        """Extract structured result from Claude CLI output."""
        import re
        stdout = session.stdout or ""

        # Try markdown code blocks first
        for block in reversed(re.findall(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', stdout)):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

        # Fallback: last JSON object in raw output
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
                        return obj
                except json.JSONDecodeError:
                    continue

        return {"summary": (stdout.strip() or "(no output)")[:1000]}

    def _build_prompt(self, prompt: str, task_type: str, context: dict) -> str:
        parts = [prompt]
        if task_type == "pm":
            parts.append(
                '\nOutput a PRD as JSON: {"target_files": [...], '
                '"verification": {"method": "...", "command": "..."}, '
                '"acceptance_criteria": [...]}'
            )
        elif task_type == "test":
            changed = context.get("changed_files", [])
            parts.append(f"\nRun tests. Changed files: {json.dumps(changed)}")
            parts.append('Report as JSON: {"test_report": {"passed": N, "failed": N, "tool": "pytest"}}')
        elif task_type == "qa":
            parts.append("\nReview changes. Respond with JSON: "
                         '{"recommendation": "qa_pass"} or {"recommendation": "reject", "reason": "..."}')
        return "\n".join(parts)


class WorkerPool:
    """Manages a pool of WorkerSlot threads."""

    def __init__(self, governance_url: str, project_id: str,
                 workspace: str, max_workers: int = 1,
                 on_event: Any = None):
        self.gov_url = governance_url.rstrip("/")
        self.project_id = project_id
        self.workspace = workspace
        self._max_workers = max_workers
        self._slots: list[WorkerSlot] = []
        self._on_event = on_event  # Callback for events

    def api(self, method: str, path: str, data: dict = None) -> dict:
        """Call governance HTTP API."""
        import urllib.request
        import urllib.error
        url = f"{self.gov_url}{path}"
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(url, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
            else:
                req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() if exc.fp else ""
            try:
                return json.loads(raw)
            except Exception:
                return {"error": str(exc), "body": raw}
        except Exception as exc:
            return {"error": str(exc)}

    def on_event(self, event_name: str, payload: dict) -> None:
        """Notify MCP server of an event from within a worker."""
        if self._on_event:
            try:
                self._on_event(event_name, payload)
            except Exception:
                pass

    def start(self) -> None:
        """Start the worker pool."""
        # PID lockfile
        lockfile = f"/tmp/aming-claw-executor-{self.project_id}.pid"
        self._kill_old_pid(lockfile)
        try:
            with open(lockfile, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        for i in range(self._max_workers):
            slot = WorkerSlot(f"w{i}", self)
            slot.start()
            self._slots.append(slot)
        log.info("WorkerPool started: %d workers for %s", len(self._slots), self.project_id)

    def stop(self, timeout: int = 30) -> None:
        """Clean shutdown: stop all workers, kill running CLI procs."""
        for slot in self._slots:
            slot.stop()
        # Give workers time to finish current task
        deadline = time.time() + timeout
        for slot in self._slots:
            remaining = max(0, deadline - time.time())
            if slot._thread and slot._thread.is_alive():
                slot._thread.join(timeout=remaining)
            slot.kill_cli()
        self._slots.clear()
        log.info("WorkerPool stopped")

    def scale(self, n: int) -> dict:
        """Set worker count to n."""
        current = len(self._slots)
        if n > current:
            for i in range(current, n):
                slot = WorkerSlot(f"w{i}", self)
                slot.start()
                self._slots.append(slot)
        elif n < current:
            for slot in self._slots[n:]:
                slot.stop()
            # Don't remove from list yet — they'll finish current task
        return {"previous": current, "target": n, "active": len(self._slots)}

    def status(self) -> dict:
        return {
            "project_id": self.project_id,
            "workers": len(self._slots),
            "slots": [s.status for s in self._slots],
            "governance_url": self.gov_url,
        }

    @staticmethod
    def _kill_old_pid(lockfile: str) -> None:
        """Kill process from previous lockfile if still alive."""
        try:
            with open(lockfile) as f:
                old_pid = int(f.read().strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(old_pid)],
                               capture_output=True, timeout=10)
            else:
                os.kill(old_pid, signal.SIGTERM)
            log.info("Killed old executor PID %d", old_pid)
            time.sleep(1)
        except (FileNotFoundError, ValueError, ProcessLookupError,
                PermissionError, OSError, subprocess.SubprocessError):
            pass
