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

        # Determine CLI binary and args
        claude_bin = os.getenv("CLAUDE_BIN", "claude")
        cwd = workspace or os.getenv("CODEX_WORKSPACE", os.getcwd())

        # v7: Write system prompt to file, use --system-prompt-file + -p
        # This gives Claude full tool access (Write/Edit/Bash) while still
        # capturing structured output via stdout.
        import tempfile
        prompt_file = os.path.join(tempfile.gettempdir(), f"ctx-{session_id}.md")
        try:
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            log.info("Prompt file written: %s (%d bytes)", prompt_file, len(system_prompt))
        except Exception as e:
            log.error("Failed to write prompt file: %s", e)

        cmd = [
            claude_bin,
            "-p",                              # Print mode (structured output)
            "--dangerously-skip-permissions",   # Skip permission prompts
            "--system-prompt-file", prompt_file, # Context via file (no stdin truncation)
            prompt,                             # User message as positional arg
        ]

        # Strip env vars that cause nested Claude issues
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        env.pop("ANTHROPIC_API_KEY", None)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
            )
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
        """Build the full prompt sent to Claude CLI."""
        from role_permissions import ROLE_PROMPTS

        role_prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS.get("coordinator", ""))

        context_str = json.dumps(context, ensure_ascii=False, indent=2) if context else "{}"

        return (
            f"{role_prompt}\n\n"
            f"项目: {project_id}\n\n"
            f"当前上下文:\n{context_str}\n\n"
            f"用户消息: {prompt}\n\n"
            "请按照指定 JSON 格式输出你的决策。"
        )
