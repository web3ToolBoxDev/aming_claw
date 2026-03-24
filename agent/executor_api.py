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
import uuid as _uuid
from datetime import datetime, timezone as _tz
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

try:
    from context_store import query_audit_by_session as _query_audit_by_session
except ImportError:  # pragma: no cover — only absent in unit-test stubs
    _query_audit_by_session = None  # type: ignore[assignment]


class ObserverManager:
    """In-process Observer session manager.

    All state is stored in class-level dicts so it survives across requests
    within the same process without any external dependency.
    """

    _sessions: dict = {}           # session_id -> {task_id, session_type, attached_at}
    _active_session_id: "str|None" = None
    _reports: dict = {}            # task_id -> report_dict

    # ── Core session management ──

    @classmethod
    def attach(cls, task_id: str, session_type: str) -> str:
        """Create a new observer session and mark it as active."""
        session_id = _uuid.uuid4().hex
        cls._sessions[session_id] = {
            "task_id": task_id,
            "session_type": session_type,
            "attached_at": datetime.now(_tz.utc).isoformat(),
        }
        cls._active_session_id = session_id
        return session_id

    @classmethod
    def detach(cls) -> bool:
        """Clear the active session pointer (session record is kept for history)."""
        if cls._active_session_id is None:
            return False
        cls._active_session_id = None
        return True

    @classmethod
    def status(cls) -> dict:
        """Return current active-session status including linked task metadata."""
        if cls._active_session_id is None:
            return {"active": False, "session_id": None, "session": None}
        session = cls._sessions.get(cls._active_session_id)
        return {
            "active": True,
            "session_id": cls._active_session_id,
            "session": session,
        }

    @classmethod
    def list_sessions(cls) -> list:
        """Return all sessions (active and historic) as a list."""
        return [
            {"session_id": sid, **data}
            for sid, data in cls._sessions.items()
        ]

    # ── Report helpers ──

    @classmethod
    def get_report(cls, task_id: str) -> "dict|None":
        """Retrieve a previously generated report for *task_id*."""
        return cls._reports.get(task_id)

    @classmethod
    def generate_report(cls, task_id: str, task_data: dict) -> dict:
        """Build and cache an execution report from *task_data* snapshot."""
        report = {
            "task_id": task_id,
            "generated_at": datetime.now(_tz.utc).isoformat(),
            "start_time": task_data.get("created_at") or task_data.get("started_at", ""),
            "end_time": task_data.get("completed_at") or task_data.get("finished_at", ""),
            "status": task_data.get("status", "unknown"),
            "result_summary": task_data.get("result") or task_data.get("summary", ""),
            "source": task_data.get("source", ""),
            "project_id": task_data.get("project_id", ""),
        }
        cls._reports[task_id] = report
        return report

    # ── Automatic registration ──

    @classmethod
    def auto_register(cls, task_id: str) -> str:
        """Auto-attach an observer session when a task is created via API."""
        session_id = cls.attach(task_id, session_type="auto")
        cls.generate_report(task_id, {"task_id": task_id, "status": "accepted"})
        return session_id

PORT = int(os.getenv("EXECUTOR_API_PORT", "40100"))

# References to shared state (set by executor.py on startup)
_ai_manager = None
_orchestrator = None
_validator_last_result = None
_context_cache = {}

# L22.3: Observer sessions registry — maps task_id → {token, status, created_at, ...}
_observer_sessions = {}


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
        elif path == "/traces":
            self._handle_traces(qs)
        elif path.startswith("/trace/"):
            trace_id = path.split("/trace/")[1]
            self._handle_trace(trace_id)
        elif path == "/ctx/list":
            self._handle_ctx_list()

        # ── Debugging (L18.5) ──

        elif path == "/validator/last-result":
            self._handle_validator_last()
        elif path.startswith("/context/"):
            project_id = path.split("/context/")[1]
            self._handle_context(project_id)
        elif path.startswith("/ai-session/") and path.endswith("/output"):
            session_id = path.split("/ai-session/")[1].replace("/output", "")
            self._handle_ai_session_output(session_id)

        # ── L22.3: Task Observe ──
        elif path.startswith("/executor/task/") and path.endswith("/observe"):
            task_id = path[len("/executor/task/"):-len("/observe")]
            self._handle_observe_task(task_id)

        # ── Observer System ──
        elif path == "/observer/status":
            self._handle_observer_status()
        elif path == "/observer/list":
            self._handle_observer_list()
        elif path.startswith("/observer/report/"):
            task_id = path[len("/observer/report/"):]
            self._handle_observer_report(task_id)

        # ── Context Audit Trace ──
        elif path.startswith("/ctx/") and path.endswith("/trace"):
            session_id = path[len("/ctx/"):-len("/trace")]
            self._handle_ctx_trace(session_id)

        # ── KPI ──
        elif path == "/kpi":
            self._handle_kpi()

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

        # ── L22.3: Unified Task Submit ──

        elif path == "/executor/task":
            self._handle_submit_task(body)

        # ── Observer System ──
        elif path == "/observer/attach":
            self._handle_observer_attach(body)
        elif path == "/observer/detach":
            self._handle_observer_detach()
        elif path == "/observer/downgrade":
            self._handle_observer_downgrade()

        # ── Context Audit Replay ──
        elif path.startswith("/ctx/") and path.endswith("/replay"):
            session_id = path[len("/ctx/"):-len("/replay")]
            self._handle_ctx_replay(session_id)

        # ── L24.2: File Write API (path-validated) ──
        elif path == "/file/write":
            self._handle_file_write(body)
        elif path == "/file/patch":
            self._handle_file_patch(body)
        elif path == "/file/mkdir":
            self._handle_file_mkdir(body)

        # ── L24.3: Controlled Command API ──
        elif path == "/test/run":
            self._handle_test_run(body)
        elif path == "/lint/run":
            self._handle_lint_run(body)

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
        """GET /trace/{id} — Full trace chain from filesystem (processing + results)."""
        from pathlib import Path
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        for stage in ["processing", "results"]:
            stage_dir = tasks_root / stage
            if not stage_dir.exists():
                continue
            for filepath in stage_dir.glob("*.json"):
                try:
                    with open(filepath) as f:
                        data = json.load(f)
                except Exception:
                    continue
                # Match by filename (task_id) or explicit trace_id field
                if filepath.stem == trace_id or data.get("trace_id") == trace_id:
                    data["_stage"] = stage
                    # Sort events by timestamp if present
                    events = data.get("events") or data.get("trace_events") or []
                    if events:
                        events.sort(key=lambda e: e.get("ts") or e.get("timestamp") or "")
                        data["events"] = events
                    self._json_response(200, data)
                    return

        self._json_response(404, {"error": f"trace {trace_id} not found"})

    def _handle_traces(self, qs):
        """GET /traces — List trace summaries from filesystem (processing + results)."""
        from pathlib import Path
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        project_id_filter = qs.get("project_id", [None])[0]
        limit = min(int(qs.get("limit", ["20"])[0]), 100)

        summaries = []
        for stage in ["processing", "results"]:
            stage_dir = tasks_root / stage
            if not stage_dir.exists():
                continue
            for filepath in stage_dir.glob("*.json"):
                try:
                    with open(filepath) as f:
                        data = json.load(f)
                except Exception:
                    continue
                pid = data.get("project_id", "")
                if project_id_filter and pid != project_id_filter:
                    continue
                summaries.append({
                    "trace_id": data.get("trace_id") or filepath.stem,
                    "task_id": data.get("task_id") or filepath.stem,
                    "status": data.get("status", "unknown"),
                    "created_at": data.get("created_at", ""),
                    "project_id": pid,
                })

        summaries.sort(key=lambda s: s["created_at"] or "", reverse=True)
        self._json_response(200, {"traces": summaries[:limit], "total": len(summaries)})

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


    # ── L22.3: Task Submit / Observe Handlers ──

    def _handle_submit_task(self, body):
        """POST /executor/task — Unified API entry point for task submission.

        Generates a task_id and observer_token, registers an observer session,
        submits the task to task_orchestrator asynchronously, and returns
        immediately without blocking on execution.
        """
        import uuid
        from datetime import datetime, timezone

        message = body.get("message", "")
        if not message:
            self._json_response(400, {"error": "message is required"})
            return

        source = body.get("source", "api")
        session_type = body.get("session_type", "task")
        project_id = body.get("project_id", "amingClaw")
        chat_id = body.get("chat_id", 0)

        # Generate unique IDs
        task_id = f"task-api-{uuid.uuid4().hex[:12]}"
        observer_token = uuid.uuid4().hex
        observer_url = f"/executor/task/{task_id}/observe"

        # Register ObserverManager auto session for the new task
        ObserverManager.auto_register(task_id)

        # Register observer session (before dispatching so observe can see it immediately)
        _observer_sessions[task_id] = {
            "token": observer_token,
            "status": "accepted",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "session_type": session_type,
            "project_id": project_id,
        }

        # Build payload for orchestrator
        payload = {
            "source": source,
            "session_type": session_type,
            "message": message,
            "project_id": project_id,
            "chat_id": chat_id,
        }

        # Dispatch asynchronously — do not block the HTTP response
        if _orchestrator:
            import threading as _threading
            t = _threading.Thread(
                target=_orchestrator.handle_task_from_api,
                args=(task_id, payload),
                daemon=True,
                name=f"api-task-{task_id[:16]}",
            )
            t.start()
            _observer_sessions[task_id]["status"] = "pending"
        else:
            log.warning("POST /executor/task: orchestrator not initialized, task accepted but not dispatched")

        self._json_response(200, {
            "task_id": task_id,
            "observer_token": observer_token,
            "observer_url": observer_url,
            "status": "accepted",
        })

    def _handle_observe_task(self, task_id):
        """GET /executor/task/{task_id}/observe — Query task execution status.

        Combines observer session metadata with filesystem stage information
        to return the current status of a submitted task.
        """
        from pathlib import Path

        session = _observer_sessions.get(task_id)
        if session is None:
            self._json_response(404, {"error": f"observer session not found for task {task_id}"})
            return

        # Probe filesystem stage
        tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks"

        fs_stage = None
        for stage in ("pending", "processing", "results"):
            if (tasks_root / stage / f"{task_id}.json").exists():
                fs_stage = stage
                break

        # Map filesystem stage to observable status
        stage_to_status = {
            "pending": "queued",
            "processing": "running",
            "results": "completed",
        }
        status = stage_to_status.get(fs_stage, session.get("status", "accepted"))

        self._json_response(200, {
            "task_id": task_id,
            "status": status,
            "fs_stage": fs_stage,
            "observer_token": session.get("token"),
            "observer_url": f"/executor/task/{task_id}/observe",
            "created_at": session.get("created_at"),
            "source": session.get("source"),
            "session_type": session.get("session_type"),
            "project_id": session.get("project_id"),
        })


    # ── Observer System Handlers ──

    def _handle_observer_attach(self, body):
        """POST /observer/attach — Create and activate an observer session."""
        task_id = body.get("task_id", "")
        session_type = body.get("session_type", "manual")
        if not task_id:
            self._json_response(400, {"error": "task_id is required"})
            return
        session_id = ObserverManager.attach(task_id, session_type)
        self._json_response(200, {
            "session_id": session_id,
            "task_id": task_id,
            "session_type": session_type,
            "active": True,
        })

    def _handle_observer_detach(self):
        """POST /observer/detach — Deactivate the current observer session."""
        ok = ObserverManager.detach()
        if ok:
            self._json_response(200, {"detached": True})
        else:
            self._json_response(200, {"detached": False, "note": "no active session"})

    def _handle_observer_status(self):
        """GET /observer/status — Active session status + linked task state."""
        from pathlib import Path

        status = ObserverManager.status()
        task_stage = None
        task_data = None

        # If there is an active session, try to pull task state from filesystem
        if status.get("active") and status.get("session"):
            task_id = status["session"].get("task_id", "")
            if task_id:
                tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
                    os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
                ) / "codex-tasks"
                for stage in ("pending", "processing", "results"):
                    fp = tasks_root / stage / f"{task_id}.json"
                    if fp.exists():
                        task_stage = stage
                        try:
                            with open(fp) as f:
                                task_data = json.load(f)
                        except Exception:
                            task_data = {}
                        break

        self._json_response(200, {
            **status,
            "task_fs_stage": task_stage,
            "task_snapshot": task_data,
        })

    def _handle_observer_report(self, task_id):
        """GET /observer/report/{task_id} — Execution report for a task."""
        from pathlib import Path

        report = ObserverManager.get_report(task_id)
        if report is None:
            # Try to generate from filesystem data on demand
            tasks_root = Path(os.getenv("SHARED_VOLUME_PATH",
                os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
            ) / "codex-tasks"
            task_data = None
            for stage in ("pending", "processing", "results", "archive"):
                fp = tasks_root / stage / f"{task_id}.json"
                if fp.exists():
                    try:
                        with open(fp) as f:
                            task_data = json.load(f)
                        task_data["_stage"] = stage
                    except Exception:
                        pass
                    break
            if task_data is None:
                self._json_response(404, {"error": f"no report and no task data found for {task_id}"})
                return
            report = ObserverManager.generate_report(task_id, task_data)

        self._json_response(200, report)

    def _handle_observer_list(self):
        """GET /observer/list — All observer sessions."""
        sessions = ObserverManager.list_sessions()
        self._json_response(200, {
            "sessions": sessions,
            "total": len(sessions),
            "active_session_id": ObserverManager._active_session_id,
        })

    def _handle_observer_downgrade(self):
        """POST /observer/downgrade — Set active session type to 'manual'."""
        sid = ObserverManager._active_session_id
        if sid is None:
            self._json_response(400, {"error": "no active observer session"})
            return
        session = ObserverManager._sessions.get(sid)
        if session is None:
            self._json_response(500, {"error": "active session id points to missing session"})
            return
        prev_type = session.get("session_type")
        session["session_type"] = "manual"
        self._json_response(200, {
            "downgraded": True,
            "session_id": sid,
            "previous_session_type": prev_type,
            "session_type": "manual",
        })


    # ── Context List Handler ──

    def _handle_ctx_list(self):
        """GET /ctx/list — List all context sessions from audit."""
        try:
            if _query_audit_by_session:
                # Get recent sessions from context_store audit
                sessions = _query_audit_by_session(limit=50)
                self._json_response(200, {"sessions": sessions})
            else:
                # Fallback: list from observer sessions
                sessions = [
                    {"task_id": tid, **info}
                    for tid, info in _observer_sessions.items()
                ]
                self._json_response(200, {"sessions": sessions})
        except Exception as e:
            self._json_response(200, {"sessions": [], "note": str(e)[:200]})

    # ── Context Audit Trace Handler ──

    def _handle_ctx_trace(self, session_id: str):
        """GET /ctx/{session_id}/trace — Return audit trace for a session.

        Calls ``context_store.query_audit_by_session`` to fetch all audit
        records associated with *session_id*.  Always returns HTTP 200; an
        absent session yields an empty ``trace`` list.
        """
        try:
            if _query_audit_by_session is None:
                raise RuntimeError("context_store.query_audit_by_session is not available")
            records = _query_audit_by_session(session_id)
            # Normalise each record to the documented schema; fill missing keys
            # with safe defaults so callers can rely on a stable shape.
            trace = [
                {
                    "role":        r.get("role") or "",
                    "prompt":      r.get("prompt") or "",
                    "ai_stdout":   r.get("ai_stdout") or "",
                    "status":      r.get("status") or "",
                    "duration_ms": r.get("duration_ms") or 0,
                    "created_at":  r.get("created_at") or "",
                }
                for r in records
            ]
            self._json_response(200, {"session_id": session_id, "trace": trace})
        except Exception as exc:
            log.exception("GET /ctx/%s/trace failed", session_id)
            self._json_response(500, {
                "error": f"failed to retrieve trace: {str(exc)[:300]}"
            })


    # ── Context Audit Replay Handler ──

    def _handle_ctx_replay(self, session_id: str):
        """POST /ctx/{session_id}/replay — Replay a session with its original input.

        Looks up the audit record(s) for *session_id*, extracts the original
        prompt and project_id from the first record, then creates a new task
        through the normal submission flow.

        Returns::

            {"new_session_id": "...", "status": "pending", "replayed_from": "<session_id>"}

        Error responses:
            404 {"error": "session not found"}        — session_id unknown
            404 {"error": "audit record not found"}   — no audit records for session
            500 {"error": "..."}                      — unexpected failure
        """
        import uuid as _uuid_mod
        from datetime import datetime, timezone

        try:
            # ── 1. Verify session existence ──────────────────────────────────
            # A session is known if it appears in ObserverManager or the
            # task-observer registry (keyed by task_id / session_id).
            session_known = (
                session_id in ObserverManager._sessions
                or session_id in _observer_sessions
            )
            if not session_known:
                self._json_response(404, {"error": "session not found"})
                return

            # ── 2. Fetch audit records ───────────────────────────────────────
            if _query_audit_by_session is None:
                raise RuntimeError("context_store.query_audit_by_session is not available")

            records = _query_audit_by_session(session_id)
            if not records:
                self._json_response(404, {"error": "audit record not found"})
                return

            # ── 3. Extract original input ────────────────────────────────────
            first = records[0]
            original_prompt = first.get("prompt") or ""
            project_id = first.get("project_id") or "amingClaw"

            # ── 4. Create new task via normal flow ───────────────────────────
            new_task_id = f"task-replay-{_uuid_mod.uuid4().hex[:12]}"
            observer_token = _uuid_mod.uuid4().hex

            ObserverManager.auto_register(new_task_id)

            _observer_sessions[new_task_id] = {
                "token": observer_token,
                "status": "accepted",
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "replay",
                "session_type": "task",
                "project_id": project_id,
                "replayed_from": session_id,
            }

            payload = {
                "source": "replay",
                "session_type": "task",
                "message": original_prompt,
                "project_id": project_id,
                "chat_id": 0,
                "replayed_from": session_id,
            }

            if _orchestrator:
                import threading as _threading
                t = _threading.Thread(
                    target=_orchestrator.handle_task_from_api,
                    args=(new_task_id, payload),
                    daemon=True,
                    name=f"replay-{new_task_id[:20]}",
                )
                t.start()
                _observer_sessions[new_task_id]["status"] = "pending"
            else:
                log.warning(
                    "POST /ctx/%s/replay: orchestrator not initialized, "
                    "task %s accepted but not dispatched",
                    session_id, new_task_id,
                )

            self._json_response(200, {
                "new_session_id": new_task_id,
                "status": _observer_sessions[new_task_id]["status"],
                "replayed_from": session_id,
            })

        except Exception as exc:
            log.exception("POST /ctx/%s/replay failed", session_id)
            self._json_response(500, {
                "error": f"replay failed: {str(exc)[:300]}"
            })


    # ── KPI Handler ──

    def _handle_kpi(self):
        """GET /kpi — Aggregate KPI metrics from archive/ and results/ task files.

        Iterates over every task status JSON in ``codex-tasks/archive/`` and
        ``codex-tasks/results/``, reads the following optional fields from each
        file, and computes per-metric ratios (4 decimal places):

        * ``retry_count``        — int, defaults to 0
        * ``validator_result``   — str, "rejected" counts as validator reject
        * ``wrong_file``         — bool/truthy, task modified a non-target file
        * ``manual_downgrade``   — bool/truthy, task was manually downgraded
        * ``observer_report``    — dict; may contain any of the above keys as
                                   fallback when top-level keys are absent

        Returns ``total_tasks: 0`` with all metrics ``null`` when no task files
        are found.
        """
        from pathlib import Path

        tasks_root = Path(os.getenv(
            "SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"),
        )) / "codex-tasks"

        total = 0
        first_pass_count = 0          # retry_count == 0
        retry_rounds_sum = 0          # sum of retry_count values
        validator_reject_count = 0    # validator_result == "rejected"
        wrong_file_count = 0          # wrong_file truthy
        manual_downgrade_count = 0    # manual_downgrade truthy

        for stage in ("archive", "results"):
            stage_dir = tasks_root / stage
            if not stage_dir.exists():
                continue
            for filepath in stage_dir.glob("*.json"):
                try:
                    with open(filepath, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue

                # observer_report acts as a fallback source for any missing top-level field
                obs = data.get("observer_report") or {}

                def _field(key, default=None):
                    """Return top-level field, falling back to observer_report."""
                    val = data.get(key)
                    if val is None:
                        val = obs.get(key)
                    return val if val is not None else default

                total += 1

                retry = int(_field("retry_count", 0))
                retry_rounds_sum += retry
                if retry == 0:
                    first_pass_count += 1

                v_result = _field("validator_result", "")
                if str(v_result).lower() == "rejected":
                    validator_reject_count += 1

                if _field("wrong_file", False):
                    wrong_file_count += 1

                if _field("manual_downgrade", False):
                    manual_downgrade_count += 1

        if total == 0:
            self._json_response(200, {
                "total_tasks": 0,
                "first_pass_rate": None,
                "avg_retry_rounds": None,
                "validator_reject_rate": None,
                "wrong_file_rate": None,
                "manual_downgrade_rate": None,
            })
            return

        self._json_response(200, {
            "total_tasks": total,
            "first_pass_rate": round(first_pass_count / total, 4),
            "avg_retry_rounds": round(retry_rounds_sum / total, 4),
            "validator_reject_rate": round(validator_reject_count / total, 4),
            "wrong_file_rate": round(wrong_file_count / total, 4),
            "manual_downgrade_rate": round(manual_downgrade_count / total, 4),
        })


    # ── L24.2: File Write API Handlers ──

    def _validate_file_path(self, path: str, task_id: str = "") -> tuple[bool, str, str]:
        """Validate file path is within allowed worktree.

        Returns (ok, resolved_path, error_message).
        Checks:
          1. Path is absolute after resolution
          2. realpath is within worktree_root (not main workspace)
          3. Not in .git, system dirs, or sensitive paths
        """
        import pathlib

        if not path:
            return False, "", "empty path"

        # Get worktree root for this task
        worktree_root = _active_worktree_roots.get(task_id, "")
        if not worktree_root:
            # Fallback: use main workspace (for coordinator tasks without worktree)
            worktree_root = os.getenv("CODEX_WORKSPACE",
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        # Resolve to absolute path within worktree
        if not os.path.isabs(path):
            path = os.path.join(worktree_root, path)

        # Resolve symlinks and normalize
        try:
            resolved = str(pathlib.Path(path).resolve())
        except Exception as e:
            return False, "", f"path resolution failed: {e}"

        # Must be within worktree root
        worktree_resolved = str(pathlib.Path(worktree_root).resolve())
        if not resolved.startswith(worktree_resolved):
            return False, "", f"path {resolved} is outside worktree {worktree_resolved}"

        # Block sensitive paths
        rel = os.path.relpath(resolved, worktree_resolved)
        blocked = [".git", ".env", "node_modules", "__pycache__"]
        for b in blocked:
            if rel == b or rel.startswith(b + os.sep):
                return False, "", f"blocked path: {rel}"

        return True, resolved, ""

    def _handle_file_write(self, body: dict):
        """POST /file/write — Write entire file (new or overwrite).

        Body: {path, content, task_id, expected_hash (optional)}
        Validates path is in worktree, records audit.
        """
        path = body.get("path", "")
        content = body.get("content", "")
        task_id = body.get("task_id", "")
        expected_hash = body.get("expected_hash", "")

        ok, resolved, err = self._validate_file_path(path, task_id)
        if not ok:
            self._json_response(403, {"error": f"path rejected: {err}"})
            return

        # Check expected hash (optimistic concurrency)
        if expected_hash and os.path.exists(resolved):
            import hashlib
            actual = hashlib.sha256(open(resolved, "rb").read()).hexdigest()[:16]
            if actual != expected_hash:
                self._json_response(409, {
                    "error": "hash mismatch",
                    "expected": expected_hash,
                    "actual": actual,
                })
                return

        # Write file
        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            old_hash = ""
            if os.path.exists(resolved):
                import hashlib
                old_hash = hashlib.sha256(open(resolved, "rb").read()).hexdigest()[:16]

            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)

            import hashlib
            new_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

            log.info("[file/write] %s (task=%s old=%s new=%s)", resolved, task_id, old_hash, new_hash)
            self._json_response(200, {
                "ok": True, "path": resolved,
                "old_hash": old_hash, "new_hash": new_hash,
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_file_patch(self, body: dict):
        """POST /file/patch — Apply old→new text replacement.

        Body: {path, old_text, new_text, task_id, expected_hash (optional)}
        """
        path = body.get("path", "")
        old_text = body.get("old_text", "")
        new_text = body.get("new_text", "")
        task_id = body.get("task_id", "")
        expected_hash = body.get("expected_hash", "")

        ok, resolved, err = self._validate_file_path(path, task_id)
        if not ok:
            self._json_response(403, {"error": f"path rejected: {err}"})
            return

        if not os.path.exists(resolved):
            self._json_response(404, {"error": f"file not found: {resolved}"})
            return

        try:
            import hashlib
            current = open(resolved, "r", encoding="utf-8").read()
            old_hash = hashlib.sha256(current.encode()).hexdigest()[:16]

            if expected_hash and old_hash != expected_hash:
                self._json_response(409, {"error": "hash mismatch", "expected": expected_hash, "actual": old_hash})
                return

            if old_text not in current:
                self._json_response(400, {"error": "old_text not found in file"})
                return

            updated = current.replace(old_text, new_text, 1)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(updated)

            new_hash = hashlib.sha256(updated.encode()).hexdigest()[:16]
            log.info("[file/patch] %s (task=%s old=%s new=%s)", resolved, task_id, old_hash, new_hash)
            self._json_response(200, {"ok": True, "path": resolved, "old_hash": old_hash, "new_hash": new_hash})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_file_mkdir(self, body: dict):
        """POST /file/mkdir — Create directory."""
        path = body.get("path", "")
        task_id = body.get("task_id", "")

        ok, resolved, err = self._validate_file_path(path, task_id)
        if not ok:
            self._json_response(403, {"error": f"path rejected: {err}"})
            return

        try:
            os.makedirs(resolved, exist_ok=True)
            log.info("[file/mkdir] %s (task=%s)", resolved, task_id)
            self._json_response(200, {"ok": True, "path": resolved})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── L24.3: Controlled Command API ──

    def _handle_test_run(self, body: dict):
        """POST /test/run — Run tests in controlled environment."""
        import subprocess
        task_id = body.get("task_id", "")
        test_cmd = body.get("command", "python -m pytest -q")
        worktree = _active_worktree_roots.get(task_id, "")
        cwd = worktree or os.getenv("CODEX_WORKSPACE", os.getcwd())

        # Whitelist test commands
        allowed_prefixes = ["python -m pytest", "python -m unittest", "npm test", "npm run test"]
        if not any(test_cmd.startswith(p) for p in allowed_prefixes):
            self._json_response(403, {"error": f"test command not allowed: {test_cmd}"})
            return

        try:
            result = subprocess.run(
                test_cmd.split(), cwd=cwd, capture_output=True, text=True, timeout=120)
            self._json_response(200, {
                "exit_code": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-1000:],
                "passed": result.returncode == 0,
            })
        except subprocess.TimeoutExpired:
            self._json_response(408, {"error": "test timeout (120s)"})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_lint_run(self, body: dict):
        """POST /lint/run — Run linter in controlled environment."""
        import subprocess
        task_id = body.get("task_id", "")
        lint_cmd = body.get("command", "python -m py_compile")
        target = body.get("target", "")
        worktree = _active_worktree_roots.get(task_id, "")
        cwd = worktree or os.getenv("CODEX_WORKSPACE", os.getcwd())

        allowed_prefixes = ["python -m py_compile", "python -m flake8", "npx eslint"]
        if not any(lint_cmd.startswith(p) for p in allowed_prefixes):
            self._json_response(403, {"error": f"lint command not allowed: {lint_cmd}"})
            return

        cmd_parts = lint_cmd.split()
        if target:
            cmd_parts.append(target)

        try:
            result = subprocess.run(
                cmd_parts, cwd=cwd, capture_output=True, text=True, timeout=30)
            self._json_response(200, {
                "exit_code": result.returncode,
                "stdout": result.stdout[-1000:],
                "stderr": result.stderr[-500:],
                "passed": result.returncode == 0,
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})


# Active worktree roots per task (set by Executor when creating dev sessions)
_active_worktree_roots: dict[str, str] = {}


def register_worktree(task_id: str, worktree_root: str) -> None:
    """Register a worktree root for a task (called by Executor)."""
    _active_worktree_roots[task_id] = worktree_root


def unregister_worktree(task_id: str) -> None:
    """Unregister worktree when task completes."""
    _active_worktree_roots.pop(task_id, None)


def start_api_server():
    """Start the Executor API server in a background thread."""
    server = HTTPServer(("0.0.0.0", PORT), ExecutorAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Executor API server started on port %d", PORT)
    return server
