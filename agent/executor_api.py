"""Executor HTTP API — Session intervention interface.

Runs alongside the Executor task loop on port 40100.
Provides monitoring, intervention, direct chat, and debugging.

Used by Claude Code sessions (developer terminal) to:
  - Monitor AI sessions and task flow
  - Pause/cancel/retry tasks
  - Directly chat with Coordinator (bypass Telegram)
  - Debug validator decisions and context assembly
"""

import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

PORT = int(os.getenv("EXECUTOR_API_PORT", "40100"))

# References to shared state (set by executor.py on startup)
_ai_manager = None
_orchestrator = None
_validator_last_result = None
_context_cache = {}


def set_shared_state(ai_manager=None, orchestrator=None):
    """Called by executor.py to share v6 components."""
    global _ai_manager, _orchestrator
    _ai_manager = ai_manager
    _orchestrator = orchestrator


def set_validator_result(result):
    """Called by decision_validator to cache last result."""
    global _validator_last_result
    _validator_last_result = result


class ExecutorAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for executor monitoring and intervention."""

    def log_message(self, format, *args):
        log.info("API %s", format % args)

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        # ── Monitoring (L18.2) ──

        if path == "/status":
            self._handle_status()
        elif path == "/sessions":
            self._handle_sessions()
        elif path == "/tasks":
            self._handle_tasks(qs)
        elif path.startswith("/task/") and not path.endswith(("/pause", "/cancel", "/retry")):
            task_id = path.split("/task/")[1]
            self._handle_task_detail(task_id)
        elif path.startswith("/trace/"):
            trace_id = path.split("/trace/")[1]
            self._handle_trace(trace_id)

        # ── Debugging (L18.5) ──

        elif path == "/validator/last-result":
            self._handle_validator_last()
        elif path.startswith("/context/"):
            project_id = path.split("/context/")[1]
            self._handle_context(project_id)
        elif path.startswith("/ai-session/") and path.endswith("/output"):
            session_id = path.split("/ai-session/")[1].replace("/output", "")
            self._handle_ai_session_output(session_id)

        # ── Health ──
        elif path == "/health":
            self._json_response(200, {
                "status": "ok",
                "service": "executor_api",
                "port": PORT,
                "ai_manager": _ai_manager is not None,
                "orchestrator": _orchestrator is not None,
            })

        else:
            self._json_response(404, {"error": f"not found: {path}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        # ── Intervention (L18.3) ──

        if path.endswith("/pause"):
            task_id = path.split("/task/")[1].replace("/pause", "")
            self._handle_task_pause(task_id)
        elif path.endswith("/cancel"):
            task_id = path.split("/task/")[1].replace("/cancel", "")
            self._handle_task_cancel(task_id)
        elif path.endswith("/retry"):
            task_id = path.split("/task/")[1].replace("/retry", "")
            self._handle_task_retry(task_id)
        elif path == "/cleanup-orphans":
            self._handle_cleanup_orphans()

        # ── Direct Chat (L18.4) ──

        elif path == "/coordinator/chat":
            self._handle_coordinator_chat(body)

        else:
            self._json_response(404, {"error": f"not found: {path}"})

    # ── L18.2 Monitoring Handlers ──

    def _handle_status(self):
        """GET /status — Overall executor status."""
        import socket
        from pathlib import Path

        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        pending = len(list((tasks_root / "pending").glob("*.json"))) if (tasks_root / "pending").exists() else 0
        processing = len(list((tasks_root / "processing").glob("*.json"))) if (tasks_root / "processing").exists() else 0

        ai_sessions = _ai_manager.list_active() if _ai_manager else []

        self._json_response(200, {
            "status": "running",
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "pending_tasks": pending,
            "processing_tasks": processing,
            "active_ai_sessions": len(ai_sessions),
            "uptime_info": "executor running",
        })

    def _handle_sessions(self):
        """GET /sessions — List active AI sessions."""
        if not _ai_manager:
            self._json_response(200, {"sessions": [], "note": "ai_manager not initialized"})
            return
        self._json_response(200, {"sessions": _ai_manager.list_active()})

    def _handle_tasks(self, qs):
        """GET /tasks — List tasks from governance."""
        project_id = qs.get("project_id", ["amingClaw"])[0]
        status = qs.get("status", [""])[0]
        limit = int(qs.get("limit", ["20"])[0])

        token = os.getenv("GOV_COORDINATOR_TOKEN", "")
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")

        try:
            import requests
            url = f"{gov_url}/api/task/{project_id}/list?limit={limit}"
            if status:
                url += f"&status={status}"
            resp = requests.get(url, headers={"X-Gov-Token": token}, timeout=5)
            self._json_response(200, resp.json())
        except Exception as e:
            self._json_response(500, {"error": str(e)[:200]})

    def _handle_task_detail(self, task_id):
        """GET /task/{id} — Single task detail."""
        from pathlib import Path
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        # Search in all stages
        for stage in ["pending", "processing", "results"]:
            filepath = tasks_root / stage / f"{task_id}.json"
            if filepath.exists():
                with open(filepath) as f:
                    task = json.load(f)
                task["_stage"] = stage
                task["_file"] = str(filepath)
                self._json_response(200, task)
                return

        self._json_response(404, {"error": f"task {task_id} not found in filesystem"})

    def _handle_trace(self, trace_id):
        """GET /trace/{id} — Trace detail."""
        try:
            from observability import load_trace
            trace = load_trace(trace_id)
            if trace:
                self._json_response(200, trace)
            else:
                self._json_response(404, {"error": f"trace {trace_id} not found"})
        except ImportError:
            self._json_response(500, {"error": "observability module not available"})

    # ── L18.3 Intervention Handlers ──

    def _handle_task_pause(self, task_id):
        """POST /task/{id}/pause — Pause a running task."""
        if _ai_manager:
            # Find session for this task and kill it
            for session in _ai_manager._sessions.values():
                if session.prompt and task_id in session.prompt:
                    _ai_manager.kill_session(session.session_id, f"paused by operator")
                    self._json_response(200, {"paused": True, "session": session.session_id})
                    return
        self._json_response(404, {"error": f"no active session for task {task_id}"})

    def _handle_task_cancel(self, task_id):
        """POST /task/{id}/cancel — Cancel a task."""
        from pathlib import Path
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        # Remove from pending
        pending = tasks_root / "pending" / f"{task_id}.json"
        if pending.exists():
            pending.unlink()
            self._json_response(200, {"cancelled": True, "was_in": "pending"})
            return

        # Kill if processing
        processing = tasks_root / "processing" / f"{task_id}.json"
        if processing.exists():
            if _ai_manager:
                for session in _ai_manager._sessions.values():
                    if session.status == "running":
                        _ai_manager.kill_session(session.session_id, "cancelled by operator")
            processing.unlink()
            self._json_response(200, {"cancelled": True, "was_in": "processing"})
            return

        self._json_response(404, {"error": f"task {task_id} not found"})

    def _handle_task_retry(self, task_id):
        """POST /task/{id}/retry — Retry a failed task."""
        from pathlib import Path
        import shutil
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        # Move from results back to pending
        for stage in ["results", "processing"]:
            filepath = tasks_root / stage / f"{task_id}.json"
            if filepath.exists():
                dst = tasks_root / "pending" / f"{task_id}.json"
                shutil.move(str(filepath), str(dst))
                self._json_response(200, {"retried": True, "moved_from": stage})
                return

        self._json_response(404, {"error": f"task {task_id} not found for retry"})

    def _handle_cleanup_orphans(self):
        """POST /cleanup-orphans — Kill orphan processes and clean stale tasks."""
        cleaned = 0
        try:
            from executor import _cleanup_orphans, _recover_stale_tasks
            cleaned += _cleanup_orphans()
            cleaned += _recover_stale_tasks()
        except Exception as e:
            self._json_response(500, {"error": str(e)[:200]})
            return
        self._json_response(200, {"cleaned": cleaned})

    # ── L18.4 Direct Chat Handler ──

    def _handle_coordinator_chat(self, body):
        """POST /coordinator/chat — Direct Coordinator session (bypass Telegram)."""
        message = body.get("message", "")
        project_id = body.get("project_id", "amingClaw")
        chat_id = body.get("chat_id", 0)

        if not message:
            self._json_response(400, {"error": "message required"})
            return

        if _orchestrator:
            try:
                result = _orchestrator.handle_user_message(
                    chat_id=chat_id,
                    text=message,
                    project_id=project_id,
                    token=os.getenv("GOV_COORDINATOR_TOKEN", ""),
                )
                # L19.4: Structured response for terminal translation
                structured = {
                    "reply": result.get("reply", ""),
                    "actions_summary": [],
                    "status": "success" if result.get("actions_rejected", 0) == 0 else "partial",
                    "actions_executed": result.get("actions_executed", 0),
                    "actions_rejected": result.get("actions_rejected", 0),
                    "next_step": "",
                }
                # Determine next step
                if result.get("actions_executed", 0) > 0:
                    structured["next_step"] = "任务已创建，等待 Executor 执行"
                elif result.get("actions_rejected", 0) > 0:
                    structured["next_step"] = "部分操作被拦截，请检查权限"
                else:
                    structured["next_step"] = "已回复，无需额外操作"

                self._json_response(200, structured)
            except Exception as e:
                self._json_response(500, {
                    "reply": f"处理失败: {str(e)[:200]}",
                    "status": "error",
                    "error": str(e)[:500],
                    "next_step": "请重试或检查日志",
                })
        else:
            self._json_response(503, {
                "reply": "Orchestrator 未初始化",
                "status": "error",
                "next_step": "重启 Executor",
            })

    # ── L18.5 Debug Handlers ──

    def _handle_validator_last(self):
        """GET /validator/last-result — Last validation result."""
        if _validator_last_result:
            self._json_response(200, {
                "approved": len(_validator_last_result.approved_actions),
                "rejected": len(_validator_last_result.rejected_actions),
                "layers": [{"layer": lr.layer, "passed": lr.passed, "errors": lr.errors}
                           for lr in _validator_last_result.layer_results],
                "needs_retry": _validator_last_result.needs_retry,
                "needs_human": _validator_last_result.needs_human,
            })
        else:
            self._json_response(200, {"note": "no validation has run yet"})

    def _handle_context(self, project_id):
        """GET /context/{pid} — Current assembled context."""
        if not _orchestrator:
            self._json_response(503, {"error": "orchestrator not initialized"})
            return
        try:
            ctx = _orchestrator.context_assembler.assemble(
                project_id=project_id, chat_id=0, role="coordinator", prompt="debug"
            )
            self._json_response(200, {"project_id": project_id, "context": ctx})
        except Exception as e:
            self._json_response(500, {"error": str(e)[:200]})

    def _handle_ai_session_output(self, session_id):
        """GET /ai-session/{id}/output — Raw AI session output."""
        if not _ai_manager:
            self._json_response(503, {"error": "ai_manager not initialized"})
            return
        session = _ai_manager.get_session(session_id)
        if not session:
            self._json_response(404, {"error": f"session {session_id} not found"})
            return
        self._json_response(200, {
            "session_id": session.session_id,
            "role": session.role,
            "status": session.status,
            "pid": session.pid,
            "stdout": session.stdout[:5000] if session.stdout else "",
            "stderr": session.stderr[:2000] if session.stderr else "",
            "exit_code": session.exit_code,
            "elapsed_sec": round(time.time() - session.started_at, 1),
        })


def start_api_server():
    """Start the Executor API server in a background thread."""
    server = HTTPServer(("0.0.0.0", PORT), ExecutorAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Executor API server started on port %d", PORT)
    return server
