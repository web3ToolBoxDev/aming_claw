"""AI Lifecycle Manager — v6 Executor-driven architecture.

All AI process management goes through this module.
AI cannot start AI. Only Executor code can create sessions.

Usage:
    manager = AILifecycleManager()
    session = manager.create_session(
        role="coordinator", prompt="...", context={...},
        project_id="amingClaw", timeout_sec=120
    )
    output = manager.wait_for_output(session.session_id)
    # output is structured JSON (parsed by ai_output_parser)
"""

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class AISession:
    """Represents a running AI CLI process."""
    session_id: str
    role: str               # coordinator / dev / tester / qa
    pid: int                # OS process ID
    project_id: str
    prompt: str
    context: dict
    started_at: float       # time.time()
    timeout_sec: int
    status: str = "running"  # running / completed / failed / killed / timeout
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None


class AILifecycleManager:
    """Manages all AI CLI processes. Code-controlled, AI cannot self-start."""

    def __init__(self):
        self._sessions: dict[str, AISession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        role: str,
        prompt: str,
        context: dict,
        project_id: str,
        timeout_sec: int = 120,
        workspace: str = "",
    ) -> AISession:
        """Start an AI CLI process.

        Args:
            role: coordinator / dev / tester / qa
            prompt: The user message or task prompt
            context: Assembled context dict (injected as system prompt)
            project_id: Project identifier
            timeout_sec: Max execution time
            workspace: Working directory for the CLI

        Returns:
            AISession with PID and session_id
        """
        session_id = f"ai-{role}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        # Build system prompt from context
        system_prompt = self._build_system_prompt(role, prompt, context, project_id)

        # Audit: write prompt to Redis Stream for full round-trip tracking
        self._audit_prompt(session_id, role, project_id, workspace or "", prompt, system_prompt)

        # Determine CLI binary and args
        claude_bin = os.getenv("CLAUDE_BIN", "claude")
        cwd = workspace or os.getenv("CODEX_WORKSPACE", os.getcwd())

        # v7.2: Write system prompt to file, use --system-prompt-file + -p
        # AI only gets read-only tools. All writes go through Executor /file/* API.
        # This prevents AI from writing to main workspace or arbitrary paths.
        prompt_file = os.path.join(tempfile.gettempdir(), f"ctx-{session_id}.md")
        try:
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            log.info("Prompt file written: %s (%d bytes)", prompt_file, len(system_prompt))
        except Exception as e:
            log.error("Failed to write prompt file: %s", e)

        # Tool access by role:
        # - dev: Read + Write + Edit + Bash (worktree isolation protects main)
        # Role-based tool permissions:
        # - dev: full access (worktree isolation protects main)
        # - tester/coordinator: read + bash (run tests, query APIs)
        # - pm/qa: read-only
        if role == "dev":
            allowed_tools = "Read,Grep,Glob,Write,Edit,Bash"
        elif role in ("tester", "coordinator"):
            allowed_tools = "Read,Grep,Glob,Bash"
        else:
            allowed_tools = "Read,Grep,Glob"

        cmd = [
            claude_bin,
            "-p",                              # Print mode (structured output via stdout)
            "--allowedTools", allowed_tools,    # Read-only tools only
            "--system-prompt-file", prompt_file, # Context via file (no stdin truncation)
        ]

        # Strip env vars that cause nested Claude issues
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        env.pop("ANTHROPIC_API_KEY", None)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,         # Prompt via stdin pipe
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
            )
            # Write prompt to stdin and close to signal EOF
            proc.stdin.write(prompt)
            proc.stdin.close()
        except FileNotFoundError:
            session = AISession(
                session_id=session_id, role=role, pid=0,
                project_id=project_id, prompt=prompt, context=context,
                started_at=time.time(), timeout_sec=timeout_sec,
                status="failed", stderr=f"CLI not found: {claude_bin}",
            )
            with self._lock:
                self._sessions[session_id] = session
            return session

        session = AISession(
            session_id=session_id,
            role=role,
            pid=proc.pid,
            project_id=project_id,
            prompt=prompt,
            context=context,
            started_at=time.time(),
            timeout_sec=timeout_sec,
        )

        with self._lock:
            self._sessions[session_id] = session

        log.info("AI session created: %s (role=%s, pid=%d, timeout=%ds)",
                 session_id, role, proc.pid, timeout_sec)

        # Register PID for safe orphan cleanup (executor only kills its own spawns)
        try:
            from executor import _EXECUTOR_SPAWNED_PIDS
            _EXECUTOR_SPAWNED_PIDS.add(proc.pid)
        except ImportError:
            pass  # Not running inside executor context

        # Wait for output in background thread (no stdin — prompt is via file + arg)
        def _run():
            try:
                stdout, stderr = proc.communicate(
                    timeout=timeout_sec
                )
                session.stdout = stdout or ""
                session.stderr = stderr or ""
                session.exit_code = proc.returncode
                session.status = "completed" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                proc.kill()
                session.stdout, session.stderr = proc.communicate()
                session.status = "timeout"
                session.exit_code = -1
                log.warning("AI session timeout: %s", session_id)
            except Exception as e:
                session.status = "failed"
                session.stderr = str(e)
                log.exception("AI session error: %s", session_id)
            finally:
                # Cleanup prompt file
                try:
                    if os.path.exists(prompt_file):
                        os.remove(prompt_file)
                except Exception:
                    pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        return session

    def wait_for_output(self, session_id: str, poll_interval: float = 0.5) -> dict:
        """Wait for AI session to complete and return output.

        Returns:
            {"status": "completed|failed|timeout", "stdout": "...", "stderr": "...",
             "exit_code": 0, "elapsed_sec": 12.3}
        """
        session = self._sessions.get(session_id)
        if not session:
            return {"status": "failed", "error": f"session {session_id} not found"}

        # Wait until session is no longer running
        while session.status == "running":
            elapsed = time.time() - session.started_at
            if elapsed > session.timeout_sec + 5:
                self.kill_session(session_id, "timeout exceeded in wait")
                break
            time.sleep(poll_interval)

        elapsed = time.time() - session.started_at

        return {
            "status": session.status,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "elapsed_sec": round(elapsed, 1),
            "session_id": session_id,
            "role": session.role,
        }

    def kill_session(self, session_id: str, reason: str = "") -> bool:
        """Force-terminate an AI process."""
        session = self._sessions.get(session_id)
        if not session or session.pid == 0:
            return False

        try:
            os.kill(session.pid, signal.SIGTERM)
            session.status = "killed"
            log.info("AI session killed: %s (reason=%s)", session_id, reason)
            return True
        except (ProcessLookupError, OSError):
            return False

    def cleanup_expired(self) -> int:
        """Kill all sessions that exceeded their timeout."""
        killed = 0
        now = time.time()
        with self._lock:
            for sid, session in list(self._sessions.items()):
                if session.status != "running":
                    continue
                if now - session.started_at > session.timeout_sec:
                    self.kill_session(sid, "timeout_cleanup")
                    killed += 1
        return killed

    def list_active(self) -> list[dict]:
        """List all active sessions."""
        result = []
        for session in self._sessions.values():
            if session.status == "running":
                result.append({
                    "session_id": session.session_id,
                    "role": session.role,
                    "pid": session.pid,
                    "project_id": session.project_id,
                    "elapsed_sec": round(time.time() - session.started_at, 1),
                })
        return result

    def get_session(self, session_id: str) -> Optional[AISession]:
        return self._sessions.get(session_id)

    def _build_system_prompt(self, role: str, prompt: str, context: dict, project_id: str) -> str:
        """Build the full prompt sent to Claude CLI.

        Structure:
          1. Role prompt (static, from ROLE_PROMPTS)
          2. API reference (shared across all roles)
          3. Base context snapshot (from /api/context-snapshot)
          4. Workspace info (dev only)
          5. Task prompt
        """
        from role_permissions import ROLE_PROMPTS, _API_REFERENCE

        role_prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS.get("coordinator", ""))

        # Fetch base context snapshot (single API call, consistent)
        snapshot_str = ""
        try:
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            task_id = context.get("task_id", "")
            url = f"{gov_url}/api/context-snapshot/{project_id}?role={role}&task_id={task_id}"
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                snapshot = json.loads(resp.read().decode())
            snapshot_str = f"\n--- Base Context Snapshot ---\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n"
        except Exception as e:
            log.warning("Context snapshot fetch failed: %s", e)

        # Dev role: inject workspace and target_files so AI knows where to work
        workspace_info = ""
        if role == "dev":
            ws = context.get("workspace", "")
            tf = context.get("target_files", [])
            if ws:
                workspace_info = (
                    f"IMPORTANT: Your working directory is: {ws}\n"
                    f"All file paths MUST use this directory as root. "
                    f"Use absolute paths starting with {ws}/ for all Read/Write/Edit operations.\n"
                )
            if tf:
                workspace_info += f"Target files: {', '.join(tf)}\n"

        context_str = ""
        try:
            import urllib.request
            snapshot_url = f"http://localhost:40000/api/context-snapshot/{project_id}"
            resp = urllib.request.urlopen(snapshot_url, timeout=5)
            snapshot = json.loads(resp.read().decode())
            context_str = json.dumps(snapshot, ensure_ascii=False, indent=2)
        except Exception:
            pass

        return (
            f"{role_prompt}\n\n"
            f"{_API_REFERENCE}\n\n"
            f"Project: {project_id}\n"
            f"{workspace_info}"
            f"{snapshot_str}\n"
            f"Task: {prompt}\n\n"
            "Respond with your decision in the specified JSON format."
        )

    @staticmethod
    def _audit_prompt(session_id: str, role: str, project_id: str,
                      workspace: str, prompt: str, system_prompt: str):
        """Write AI prompt to Redis Stream for audit trail."""
        try:
            from governance.redis_client import get_redis
            r = get_redis()
            if not r:
                return
            stream_key = f"ai:prompt:{session_id}"
            r.xadd(stream_key, {
                "type": "prompt",
                "session_id": session_id,
                "role": role,
                "project_id": project_id,
                "workspace": workspace,
                "prompt_length": str(len(prompt)),
                "system_prompt_length": str(len(system_prompt)),
                "user_prompt": prompt[:5000],  # Truncate for Redis memory
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, maxlen=5000)
        except Exception as e:
            log.debug("Redis audit write failed (non-fatal): %s", e)

    @staticmethod
    def audit_result(session_id: str, project_id: str, result: dict):
        """Write AI result to Redis Stream for full round-trip audit."""
        try:
            from governance.redis_client import get_redis
            r = get_redis()
            if not r:
                return
            stream_key = f"ai:prompt:{session_id}"
            r.xadd(stream_key, {
                "type": "result",
                "session_id": session_id,
                "project_id": project_id,
                "status": result.get("status", "unknown"),
                "exit_code": str(result.get("exit_code", -1)),
                "elapsed_sec": str(result.get("elapsed_sec", 0)),
                "stdout_length": str(len(result.get("stdout", ""))),
                "stdout": result.get("stdout", "")[:10000],
                "stderr": result.get("stderr", "")[:2000],
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, maxlen=5000)
        except Exception as e:
            log.debug("Redis audit result write failed (non-fatal): %s", e)
