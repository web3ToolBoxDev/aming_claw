"""
executor.py - Task dispatcher and main polling loop.

Slim orchestrator: picks pending tasks, dispatches to the right backend
(screenshot, claude, pipeline, or codex), and handles error/timeout flows.

Heavy lifting is delegated to:
  backends.py   - AI runners, noop detection, pipeline
  task_accept.py - Acceptance docs, finalization, notifications
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import http.server
import json as _json
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone

import requests

from utils import (
    load_json,
    save_json,
    send_document,
    send_text,
    task_file,
    tasks_root,
    utc_iso,
)
from git_rollback import (
    pre_task_checkpoint,
    get_workspace_git_status,
)

# Track PIDs spawned by THIS executor instance.
# Only these PIDs are eligible for orphan cleanup.
# NEVER kill arbitrary claude.exe processes — they may be user Claude Code sessions.
_EXECUTOR_SPAWNED_PIDS: set = set()

# ── Graceful shutdown ──────────────────────────────────────────────────────
_shutdown_requested: threading.Event = threading.Event()
_GRACEFUL_SHUTDOWN_TIMEOUT_SEC: int = int(os.getenv("EXECUTOR_SHUTDOWN_TIMEOUT_SEC", "120"))
from task_state import (
    append_task_event,
    load_task_status,
    mark_task_finished,
    mark_task_started,
    mark_task_completion_notified,
    update_task_heartbeat,
    update_task_runtime,
)
from config import get_agent_backend
from i18n import t
from backends import (
    process_codex,
    process_claude,
    process_pipeline,
    resolve_workspace as _backends_resolve_workspace,
    # Re-exports for backward compatibility (tests import from executor)
    build_codex_prompt,
    build_claude_prompt,
    run_codex,
    run_claude,
    run_codex_with_retry,
    run_claude_with_retry,
    is_ack_only_message,
    has_execution_evidence,
    detect_noop_execution,
)
from task_accept import (
    finalize_codex_task,
    finalize_pipeline_task,
    to_pending_acceptance,
    task_inline_keyboard,
    build_task_summary,
    acceptance_notice_text,
    write_run_log,
    acceptance_root,
    json_sha256,
    generate_stage_summary,
    format_elapsed,
)


class TaskLogger:
    """Per-task structured logging for observer monitoring."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.log_dir = tasks_root() / "logs" / task_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.log_dir / "timeline.jsonl"

    def log_event(self, event: str, data: dict = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
            **(data or {}),
        }
        with open(self.timeline_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")

    def write_file(self, name: str, content: str) -> None:
        (self.log_dir / name).write_text(content, encoding="utf-8")

    def write_json(self, name: str, data) -> None:
        with open(self.log_dir / name, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)


def pick_pending_task() -> Optional[Path]:
    pending_dir = tasks_root() / "pending"
    # Only scan real .json files (not .tmp)
    items = sorted(
        (f for f in pending_dir.glob("*.json") if not f.name.endswith(".tmp.json")),
        key=lambda p: p.stat().st_mtime,
    )
    return items[0] if items else None


def move_task(src: Path, stage: str) -> Path:
    dst = task_file(stage, src.stem)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def call_executor_gateway(task: Dict) -> Dict:
    base_url = os.getenv("EXECUTOR_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    token = os.getenv("EXECUTOR_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing EXECUTOR_API_TOKEN")
    payload = {
        "task_id": task["task_id"],
        "action": "take_screenshot",
        "command_text": task["text"],
    }
    t0 = time.perf_counter()
    resp = requests.post(
        base_url + "/execute",
        json=payload,
        headers={"X-Executor-Token": token},
        timeout=int(os.getenv("SCREENSHOT_TIMEOUT_SEC", "90")),
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    data = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError("executor-gateway http {}: {}".format(resp.status_code, data))
    data["_elapsed_ms"] = elapsed_ms
    return data


def wants_timing(task: Dict) -> bool:
    text = (task.get("text") or "").lower()
    return ("耗时" in text) or ("timing" in text) or ("time cost" in text)


def process_screenshot(task: Dict, processing: Path) -> Dict:
    gateway = call_executor_gateway(task)
    if not gateway.get("ok"):
        raise RuntimeError(gateway.get("error") or "screenshot failed")

    details = gateway.get("details") or {}
    files = details.get("files") or []
    chat_id = int(task.get("chat_id") or 0)

    sent = []
    for path in files:
        p = Path(path)
        if p.exists():
            send_document(chat_id, p, caption="screenshot: {}".format(p.name))
            sent.append(str(p))

    send_text(chat_id, t("msg.screenshot_done", count=len(sent)))

    timings = details.get("timings_ms") or {}
    if wants_timing(task):
        send_text(
            chat_id,
            t("msg.screenshot_timing_gw",
              total=timings.get("total_ms", 0),
              capture=timings.get("capture_ms", 0),
              copy=timings.get("copy_ms", 0),
              gateway=gateway.get("_elapsed_ms", 0)),
        )

    result = {
        **task,
        "status": "completed",
        "completed_at": utc_iso(),
        "updated_at": utc_iso(),
        "executor": {
            "action": "screenshot",
            "gateway_elapsed_ms": gateway.get("_elapsed_ms", 0),
            "files_sent": sent,
            "timings_ms": timings,
        },
    }
    save_json(processing, result)
    return result


def _heartbeat_loop(task_id: str, stop_event: threading.Event, interval_sec: float = 30.0) -> None:
    """Background thread: updates heartbeat_at in status.json every interval_sec."""
    while not stop_event.wait(interval_sec):
        try:
            update_task_heartbeat(task_id)
        except Exception:
            pass


def _worker_id() -> str:
    # chain v4
    return "executor-{}".format(socket.gethostname())


def is_idle() -> bool:
    """Return True when no tasks are actively being processed (active_count == 0)."""
    processing_dir = tasks_root() / "processing"
    if not processing_dir.exists():
        return True
    active = sum(1 for _ in processing_dir.glob("*.json"))
    return active == 0


def _request_shutdown(signum: int, frame) -> None:
    """Signal handler: request graceful shutdown on SIGTERM or SIGINT."""
    sig_name = "SIGTERM" if signum == getattr(signal, "SIGTERM", 15) else "SIGINT"
    print(f"[executor] received {sig_name}, requesting graceful shutdown...")
    _shutdown_requested.set()


def _wait_for_idle_or_timeout(
    timeout_sec: int = _GRACEFUL_SHUTDOWN_TIMEOUT_SEC,
) -> None:
    """Block until all active tasks finish or timeout_sec elapses.

    Also flushes workspace_queue state to disk (best-effort) before returning.
    The workspace_queue module persists every mutation, so this is a no-op
    in practice — it just ensures the in-memory references are released cleanly.
    """
    if not is_idle():
        print(
            f"[executor] waiting up to {timeout_sec}s for active tasks to complete..."
        )
        deadline = time.monotonic() + timeout_sec
        while not is_idle():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[executor] shutdown timeout reached, exiting with active tasks")
                break
            time.sleep(1.0)
        if is_idle():
            print("[executor] all tasks completed, shutting down cleanly")

    # Best-effort: ensure workspace_queue state is flushed to disk.
    try:
        from workspace_queue import list_all_queues  # noqa: F401 — triggers module-level _save_queue via import
    except Exception:
        pass


# ── Tool Policy ────────────────────────────────────────────────────────────

TOOL_POLICY = {
    "auto_allow": [
        "git diff", "git status", "git log", "git show",
        "pytest", "python -m pytest", "npm test", "npm run test",
        "python -m unittest", "ls", "cat", "head", "tail", "wc",
    ],
    "needs_approval": [
        "git push", "git reset", "docker compose down",
        "npm publish", "pip install", "npm install",
    ],
    "always_deny": [
        "rm -rf /", "rm -rf ~", "format", "mkfs",
        "shutdown", "reboot",
    ],
}


def check_tool_policy(command: str) -> str:
    """Check command against tool policy. Returns 'allow', 'approval', or 'deny'."""
    cmd_lower = command.lower().strip()
    for denied in TOOL_POLICY["always_deny"]:
        if denied in cmd_lower:
            return "deny"
    for needs in TOOL_POLICY["needs_approval"]:
        if needs in cmd_lower:
            return "approval"
    return "allow"


# ---------------------------------------------------------------------------
# Gate session — isolated subprocess for acceptance review
# ---------------------------------------------------------------------------

#: Only these payload keys are forwarded to the gate subprocess.
_GATE_PAYLOAD_ALLOWED_KEYS: frozenset = frozenset({
    "acceptance_screenshot",
    "logs",
    "original_instruction",
})

#: Explicit env vars allowed to pass into the gate child process.
#: Anything not in this set is blocked (no parent secrets leak).
_GATE_ENV_PASSTHROUGH: frozenset = frozenset({
    "PATH",
    "PYTHONPATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "COMSPEC",
})


def spawn_gate_session(task_id: str, payload: dict) -> dict:
    """Spawn an isolated gate-review subprocess.

    Each call generates a fresh UUID4 session_id.  The payload is sanitised
    (only ``_GATE_PAYLOAD_ALLOWED_KEYS`` are forwarded) and written to a
    temporary file whose path is passed via ``GATE_PAYLOAD_FILE``.

    The child process receives a minimal env containing only
    ``_GATE_ENV_PASSTHROUGH`` keys plus the gate-specific variables
    ``GATE_SESSION_ID``, ``GATE_TASK_ID``, and ``GATE_PAYLOAD_FILE``.
    ``shell=False`` is enforced for Windows security.

    Returns:
        dict with keys: session_id, task_id, status, elapsed_ms.
        ``status`` is one of: "completed", "failed", "timeout", "error".
    """
    import uuid
    import json as _json_gs
    import tempfile
    import time as _time_gs

    session_id = str(uuid.uuid4())
    timeout_sec = int(os.getenv("GATE_SESSION_TIMEOUT_SEC", "120"))
    start = _time_gs.monotonic()
    payload_path: Optional[str] = None

    try:
        # Sanitise: strip forbidden keys
        clean_payload = {k: v for k, v in payload.items() if k in _GATE_PAYLOAD_ALLOWED_KEYS}

        # Write payload to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tf:
            payload_path = tf.name
            _json_gs.dump(clean_payload, tf, ensure_ascii=False)

        # Build isolated child env (no arbitrary parent vars)
        child_env: dict = {}
        for key in _GATE_ENV_PASSTHROUGH:
            val = os.environ.get(key)
            if val is not None:
                child_env[key] = val
        child_env["GATE_SESSION_ID"] = session_id
        child_env["GATE_TASK_ID"] = task_id
        child_env["GATE_PAYLOAD_FILE"] = payload_path

        proc = subprocess.run(
            [sys.executable, "-m", "gate_runner"],
            capture_output=True,
            text=True,
            env=child_env,
            timeout=timeout_sec,
            shell=False,
        )

        elapsed_ms = int((_time_gs.monotonic() - start) * 1000)

        if proc.returncode != 0:
            return {
                "session_id": session_id,
                "task_id": task_id,
                "status": "failed",
                "elapsed_ms": elapsed_ms,
            }

        try:
            output = _json_gs.loads(proc.stdout.strip())
        except Exception:
            output = {}

        return {
            "session_id": session_id,
            "task_id": task_id,
            "status": output.get("status", "completed"),
            "elapsed_ms": elapsed_ms,
        }

    except subprocess.TimeoutExpired:
        return {
            "session_id": session_id,
            "task_id": task_id,
            "status": "timeout",
            "elapsed_ms": timeout_sec * 1000,
        }
    except Exception:
        elapsed_ms = int((_time_gs.monotonic() - start) * 1000)
        return {
            "session_id": session_id,
            "task_id": task_id,
            "status": "error",
            "elapsed_ms": elapsed_ms,
        }
    finally:
        if payload_path:
            try:
                os.unlink(payload_path)
            except Exception:
                pass


def _handle_task_failure(task: Dict, processing: Path, chat_id: int, exc: Exception) -> str:
    """Shared error handler for task timeout and general exceptions.

    Finalizes the task as failed, moves to pending_acceptance, and notifies user.
    Returns the error message string.
    """
    _action = task.get("action", "codex")
    _timeout_env = "CLAUDE_TIMEOUT_RETRIES" if _action == "claude" else "CODEX_TIMEOUT_RETRIES"
    is_timeout = isinstance(exc, subprocess.TimeoutExpired)
    error_str = "{} timeout".format(_action) if is_timeout else str(exc)

    run_data = {
        "returncode": None,
        "elapsed_ms": None,
        "cmd": None,
        "timeout_retries": int(os.getenv(_timeout_env, "1")),
        "workspace": str(_backends_resolve_workspace()) if is_timeout else None,
        "git_changed_files": None,
        "noop_reason": None,
        "stdout": "",
        "stderr": "",
        "last_message": "",
    }
    result = finalize_codex_task(task, processing, run_data, "failed", error=error_str)
    log_path = tasks_root() / "logs" / (task["task_id"] + ".run.json")
    result = to_pending_acceptance(task, result)
    save_json(processing, result)
    failed_path = move_task(processing, "results")
    update_task_runtime(result, status="pending_acceptance", stage="results")
    mark_task_finished(
        result,
        status="pending_acceptance",
        stage="results",
        result_file=str(failed_path),
        runlog_file=str(log_path) if log_path and log_path.exists() else "",
        summary=build_task_summary(result),
        error=str(result.get("error") or error_str),
    )
    task_code = result.get("task_code", "-")
    # Truncate error to 5 lines max
    err_lines = error_str[:500].strip().splitlines()
    if len(err_lines) > 5:
        err_lines = err_lines[:5] + ["..."]
    err_display = "\n".join(err_lines)
    send_text(
        chat_id,
        t("msg.task_failed", code=task_code, err=err_display),
        reply_markup=task_inline_keyboard(task_code, task["task_id"]),
    )
    mark_task_completion_notified(task["task_id"])
    return error_str


def _gov_api(method: str, path: str, data: dict = None, token: str = None) -> dict:
    """Call governance API. Non-blocking, returns {} on failure."""
    import json as _json
    gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
    url = f"{gov_url}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gov-Token"] = token
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=5)
        else:
            resp = requests.post(url, headers=headers, data=_json.dumps(data or {}), timeout=5)
        return resp.json()
    except Exception:
        return {}


def _redis_notify(event: str, payload: dict) -> None:
    """Publish task event to Redis. Fire-and-forget."""
    try:
        import redis
        import json as _json
        redis_url = os.getenv("REDIS_URL", "redis://localhost:40079/0")
        r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        r.publish("task:events", _json.dumps({"event": event, "payload": payload}))
    except Exception:
        pass


def _registry_claim(task: dict) -> bool:
    """Claim task in Task Registry. Returns True if successful."""
    project_id = task.get("project_id", "")
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    if not project_id or not token:
        return True  # No registry configured, proceed anyway
    result = _gov_api("POST", f"/api/task/{project_id}/claim",
        data={"task_id": task["task_id"], "worker_id": _worker_id()},
        token=token)
    return result.get("error") is None


def _registry_complete(task: dict, execution_status: str, error: str = "") -> None:
    """Mark task complete in Task Registry."""
    project_id = task.get("project_id", "")
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    if not project_id or not token:
        return
    _gov_api("POST", f"/api/task/{project_id}/complete",
        data={
            "task_id": task["task_id"],
            "execution_status": execution_status,
            "error_message": error[:500] if error else "",
        },
        token=token)
    # Also notify via Redis
    _redis_notify("task:completed", {
        "task_id": task["task_id"],
        "project_id": project_id,
        "execution_status": execution_status,
        "chat_id": task.get("chat_id"),
    })


def process_task(path: Path) -> None:
    if not path.exists():
        return  # Already claimed by another worker
    try:
        task = load_json(path)
    except (FileNotFoundError, PermissionError):
        return  # Race: another worker grabbed it
    task_id = str(task.get("task_id") or "")

    # Idempotency: if already in a terminal state, skip silently
    _TERMINAL = {"succeeded", "failed", "timeout", "canceled", "completed", "pending_acceptance", "accepted", "rejected"}
    existing = load_task_status(task_id)
    if existing and existing.get("status") in _TERMINAL:
        try:
            path.unlink()
        except Exception:
            pass
        return

    # If already in processing/ (parallel mode moves before dispatch),
    # don't re-move; otherwise move from pending/ to processing/
    if "processing" in str(path.parent):
        processing = path
    else:
        processing = move_task(path, "processing")
    task = load_json(processing)
    task["status"] = "processing"
    task["started_at"] = utc_iso()
    task["updated_at"] = utc_iso()
    task["worker_id"] = _worker_id()
    task["attempt"] = int(task.get("attempt") or 0) + 1
    max_attempts = int(task.get("max_attempts") or 3)

    # Dead letter: too many retries
    if task["attempt"] > max_attempts:
        dead_letter_dir = tasks_root() / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        dead_path = dead_letter_dir / processing.name
        task["status"] = "failed_terminal"
        task["failure_reason"] = f"exceeded max_attempts ({max_attempts})"
        save_json(processing, task)
        processing.rename(dead_path)
        print(f"[executor] task {task_id} moved to dead_letter after {max_attempts} attempts")
        return

    task.setdefault("action", get_agent_backend())
    save_json(processing, task)

    # Claim in Task Registry (non-blocking)
    _registry_claim(task)

    update_task_runtime(task, status="processing", stage="processing")
    mark_task_started(task, stage="processing")

    # Start heartbeat thread
    _hb_stop = threading.Event()
    _hb_interval = float(os.getenv("EXECUTOR_HEARTBEAT_SEC", "30"))
    _hb_thread = threading.Thread(target=_heartbeat_loop, args=(task_id, _hb_stop, _hb_interval), daemon=True)
    _hb_thread.start()

    chat_id = int(task.get("chat_id") or 0)
    silent_mode = os.getenv("TASK_SILENT_MODE", "1").strip().lower() not in {"0", "false", "no"}
    log_path: Optional[Path] = None

    # ── Git checkpoint: auto-commit uncommitted changes before task execution ──
    checkpoint_info: Dict = {}
    try:
        checkpoint_info = pre_task_checkpoint(
            workspace=Path(resolve_workspace(task)), task_id=task_id)
        if checkpoint_info.get("auto_committed"):
            append_task_event(task_id, "git_checkpoint_created", {
                "checkpoint_commit": checkpoint_info.get("checkpoint_commit", ""),
                "auto_committed_files": checkpoint_info.get("committed_files", []),
            })
        elif checkpoint_info.get("checkpoint_commit"):
            append_task_event(task_id, "git_checkpoint_recorded", {
                "checkpoint_commit": checkpoint_info.get("checkpoint_commit", ""),
            })
        if checkpoint_info.get("error"):
            append_task_event(task_id, "git_checkpoint_warning", {
                "error": checkpoint_info["error"],
            })
    except Exception as exc:
        append_task_event(task_id, "git_checkpoint_error", {"error": str(exc)})
        checkpoint_info = {"checkpoint_commit": "", "error": str(exc)}

    # Store checkpoint info in task for later use by accept/reject
    task["_git_checkpoint"] = checkpoint_info.get("checkpoint_commit", "")
    save_json(processing, task)

    try:
        append_task_event(task["task_id"], "executor_run_begin", {"action": task.get("action", "codex")})
        if task.get("action") == "coordinator_chat":
            result = process_coordinator_chat(task, processing)
        elif task.get("action") == "screenshot":
            result = process_screenshot(task, processing)
        elif task.get("type") == "dev_task" and task.get("project_id"):
            result = process_dev_task_v6(task, processing)
        elif task.get("type") == "test_task" and task.get("project_id"):
            result = process_test_task_v6(task, processing)
        elif task.get("type") == "qa_task" and task.get("project_id"):
            result = process_qa_task_v6(task, processing)
        elif task.get("action") == "claude":
            result = process_claude(task, processing)
        elif task.get("action") == "pipeline":
            result = process_pipeline(task, processing)
        else:
            result = process_codex(task, processing)
        append_task_event(
            task["task_id"],
            "executor_run_end",
            {
                "execution_status": result.get("status", "unknown"),
                "action": task.get("action", "codex"),
            },
        )
        # v7 path: dev_task/coordinator_chat with project_id → skip old finalize
        is_v6 = task.get("project_id") and task.get("type") in ("dev_task", "test_task", "qa_task", "coordinator_chat")

        if is_v6:
            # v6: Save result, move to results, trigger next step
            result_path = move_task(processing, "results")
            save_json(result_path, result)
            exec_status = "succeeded" if result.get("status") in ("completed", "succeeded") else "failed"
            _registry_complete(task, exec_status)

            # Auto-trigger chain per task type
            task_type = task.get("type", "")
            try:
                if task_type == "dev_task":
                    _trigger_coordinator_eval(task, result)
                elif task_type == "test_task":
                    if exec_status == "succeeded":
                        from task_orchestrator import TaskOrchestrator
                        TaskOrchestrator().handle_test_complete(
                            task_id=task["task_id"], project_id=task.get("project_id", ""),
                            token=task.get("_gov_token", ""), chat_id=chat_id,
                            test_report=result.get("executor", {}))
                    else:
                        # Log test failure to audit for observability
                        from task_orchestrator import TaskOrchestrator
                        TaskOrchestrator()._log_stage_transition(
                            task["task_id"], "test", "failed", "test_failed")
                        if chat_id:
                            failed_count = result.get("executor", {}).get("failed", "?")
                            _gateway_notify(chat_id,
                                f"❌ Tests failed ({failed_count} failures). Chain stopped.")
                elif task_type == "qa_task" and exec_status == "succeeded":
                    from task_orchestrator import TaskOrchestrator
                    TaskOrchestrator().handle_qa_complete(
                        task_id=task["task_id"], project_id=task.get("project_id", ""),
                        token=task.get("_gov_token", ""), chat_id=chat_id,
                        qa_report=result.get("executor", {}),
                        verification=task.get("_verification", {}))
            except Exception as chain_err:
                print(f"[executor] task {task_id} chain trigger ({task_type}) failed: {chain_err}")
                if chat_id:
                    _gateway_notify(chat_id, f"链路触发失败: {str(chain_err)[:200]}")
        else:
            # Legacy path: old finalize → pending_acceptance → send_text
            if task.get("action") in ("codex", "claude", "pipeline"):
                log_path = tasks_root() / "logs" / (task["task_id"] + ".run.json")
            result = to_pending_acceptance(task, result)
            save_json(processing, result)
            result_path = move_task(processing, "results")
            update_task_runtime(result, status="pending_acceptance", stage="results")
            acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else {}
            mark_task_finished(
                result,
                status="pending_acceptance",
                stage="results",
                result_file=str(result_path),
                runlog_file=str(log_path) if log_path and log_path.exists() else "",
                summary=build_task_summary(result),
                error=str(result.get("error") or ""),
            )
            result["_git_checkpoint"] = task.get("_git_checkpoint", "")
            save_json(result_path, result)

            task_code = result.get("task_code", "-")
            send_text(
                chat_id,
                acceptance_notice_text(result, task["task_id"], task_code, detailed=not silent_mode),
                reply_markup=task_inline_keyboard(task_code, task["task_id"]),
            )
            mark_task_completion_notified(task["task_id"])
            _registry_complete(task, "succeeded")

    except (subprocess.TimeoutExpired, Exception) as exc:
        error_msg = _handle_task_failure(task, processing, chat_id, exc)
        # v6: Classify error for retry strategy
        try:
            from task_state_machine import classify_error, get_retry_strategy
            category = classify_error(str(exc))
            strategy = get_retry_strategy(category)
            attempt = int(task.get("attempt", 1))
            max_attempts = int(task.get("max_attempts", strategy.get("max_retries", 3)))
            if category.value in ("retryable_model", "retryable_env") and attempt < max_attempts:
                _registry_complete(task, "failed_retryable", error=str(exc)[:500])
            else:
                _registry_complete(task, "failed", error=str(exc)[:500])
        except ImportError:
            _registry_complete(task, "failed", error=str(exc)[:500])
    finally:
        _hb_stop.set()
        # ── Auto-generate execution report on task completion ──
        try:
            from executor_api import ObserverManager
            report = ObserverManager.generate_report(task_id, task)
            if report:
                # Persist report to dbservice (best-effort, following project pattern)
                try:
                    dbservice_url = os.getenv("DBSERVICE_URL", "http://localhost:40002")
                    requests.post(
                        f"{dbservice_url}/knowledge/upsert",
                        json={
                            "project_id": task.get("project_id", ""),
                            "type": "task_report",
                            "task_id": task_id,
                            "content": report if isinstance(report, str) else _json.dumps(
                                report, ensure_ascii=False),
                        },
                        timeout=5,
                    )
                except Exception:
                    # dbservice 不可用时写入 task state 的 report 字段
                    from task_state import save_task_status
                    save_task_status(task_id, {"report": report})
        except ImportError:
            pass  # executor_api 未启动时跳过


def resolve_workspace(task: dict) -> str:
    """Resolve the main workspace path for a task.

    Priority:
      1. task['_workspace'] if present
      2. Workspace registry (by project_id, label, or default)
      3. Active workspace from environment
    """
    if task.get("_workspace"):
        return task["_workspace"]
    from workspace_registry import resolve_workspace_for_task
    ws = resolve_workspace_for_task(task)
    if ws:
        return ws["path"]
    from workspace import resolve_active_workspace
    return str(resolve_active_workspace())


def _preflight_check(task: dict, workspace: str) -> list:
    """Validate task context matches resolved workspace before AI session.

    Returns list of error strings. Empty list = all checks pass.
    """
    errors = []

    # 1. target_files exist in workspace
    for tf in task.get("target_files", []):
        full = os.path.join(workspace, tf)
        if not os.path.exists(full):
            errors.append(f"target_file not found: {tf} (in {workspace})")

    # 2. project_id matches workspace registry entry
    try:
        from workspace_registry import find_workspace_by_project_id
        ws = find_workspace_by_project_id(task.get("project_id", ""))
        if ws and os.path.normpath(ws["path"]) != os.path.normpath(workspace):
            errors.append(
                f"workspace mismatch: registry={ws['path']}, resolved={workspace}"
            )
    except ImportError:
        pass

    # 3. Workspace has .git (is a valid repo)
    if not os.path.isdir(os.path.join(workspace, ".git")):
        errors.append(f"no .git in workspace: {workspace}")

    # 4. Main workspace should be on main branch (prevent branch pollution)
    try:
        r = subprocess.run(["git", "branch", "--show-current"],
            cwd=workspace, capture_output=True, text=True, timeout=5)
        current_branch = r.stdout.strip()
        if current_branch and current_branch != "main":
            # Auto-fix: checkout main before task starts
            subprocess.run(["git", "checkout", "main"],
                cwd=workspace, capture_output=True, timeout=10)
            print(f"[preflight] auto-fixed branch: {current_branch} → main")
    except Exception:
        pass

    return errors


def process_dev_task_v6(task: Dict, processing: Path) -> Dict:
    """Process dev_task through v6 pipeline.

    Flow:
      1. Git branch: checkout -b dev/task-xxx
      2. Evidence snapshot (before)
      3. AILifecycleManager: start dev session
      4. Evidence collect (after): git diff, test results
      5. DecisionValidator: check dev output
      6. Return result (eval triggered separately by _trigger_coordinator_eval)
    """
    task_id = task.get("task_id", "")
    project_id = task.get("project_id", "")
    prompt = task.get("prompt", "")
    chat_id = int(task.get("chat_id", 0))
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    branch = task.get("_branch", "")

    # Task logger
    tlog = TaskLogger(task_id)
    tlog.log_event("dev_task_start", {"project_id": project_id, "prompt": prompt[:200]})
    tlog.write_file("prompt.txt", prompt)

    try:
        from ai_lifecycle import AILifecycleManager
        from evidence_collector import EvidenceCollector
        from context_assembler import ContextAssembler
        from ai_output_parser import parse_ai_output

        ai_mgr = AILifecycleManager()
        evidence = EvidenceCollector()
        ctx_asm = ContextAssembler()
        tlog.log_event("v6_modules_loaded")

        # 0. Resolve workspace and pre-flight validation
        main_workspace = resolve_workspace(task)
        preflight_errors = _preflight_check(task, main_workspace)
        if preflight_errors:
            tlog.log_event("preflight_failed", {"errors": preflight_errors})
            for err in preflight_errors:
                print(f"[executor-v6] preflight: {err}")
            # Non-fatal: log warnings but continue (target_files may be created by AI)
        try:
            status_r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=main_workspace, capture_output=True, text=True, timeout=10)
            dirty_files = [l for l in status_r.stdout.strip().split("\n") if l.strip()]
            if dirty_files:
                tlog.log_event("workspace_dirty", {"files": dirty_files[:5]})
                print(f"[executor-v6] WARNING: workspace has {len(dirty_files)} uncommitted files")
                # Auto-stash to prevent pollution
                subprocess.run(
                    ["git", "stash", "push", "-m", f"auto-clean-{task_id}"],
                    cwd=main_workspace, capture_output=True, text=True, timeout=10)
                print(f"[executor-v6] auto-stashed dirty files")
        except Exception as e:
            print(f"[executor-v6] clean check failed: {e}")

        # 1. Git worktree: create isolated directory for dev work
        worktree_dir = ""
        if not branch:
            branch = f"dev/{task_id}"
            worktree_dir = os.path.join(main_workspace, ".worktrees", branch.replace("/", "-"))
            try:
                os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch, worktree_dir],
                    cwd=main_workspace, capture_output=True, text=True, timeout=30,
                    check=True)
                print(f"[executor-v6] created worktree: {worktree_dir} (branch: {branch})")
                tlog.log_event("git_worktree_created", {"worktree": worktree_dir, "branch": branch})
            except subprocess.CalledProcessError as e:
                print(f"[executor-v6] worktree creation failed: {e.stderr}")
                # Fallback: try checkout -b in main workspace
                worktree_dir = ""
                try:
                    subprocess.run(["git", "checkout", "-b", branch],
                        cwd=main_workspace, capture_output=True, timeout=10)
                    print(f"[executor-v6] fallback to checkout -b: {branch}")
                except Exception:
                    branch = ""
            except Exception as e:
                print(f"[executor-v6] worktree error: {e}")
                worktree_dir = ""
                branch = ""

        # Use worktree dir if available, otherwise main workspace
        workspace = worktree_dir if worktree_dir else main_workspace

        # L24.4: Register worktree root for file API path validation
        try:
            from executor_api import register_worktree, unregister_worktree
            register_worktree(task_id, workspace)
        except ImportError:
            pass

        # 2. Before snapshot — evidence must use the worktree, not main workspace
        evidence.workspace = workspace
        before = evidence.collect_before_snapshot()

        # 3. Assemble dev context (with workspace + target_files for prompt injection)
        dev_context = ctx_asm.assemble(
            project_id=project_id, chat_id=chat_id,
            role="dev", prompt=prompt,
            workspace=workspace,
            target_files=task.get("target_files", []),
        )

        # 4. Start dev AI session
        session = ai_mgr.create_session(
            role="dev",
            prompt=prompt,
            context=dev_context,
            project_id=project_id,
            timeout_sec=int(os.getenv("AI_SESSION_TIMEOUT", "600")),
            workspace=workspace,
        )

        raw = ai_mgr.wait_for_output(session.session_id)
        tlog.log_event("ai_session_complete", {"status": raw.get("status")})
        tlog.write_file("stdout.txt", raw.get("stdout", ""))
        if raw.get("stderr"):
            tlog.write_file("stderr.txt", raw.get("stderr", ""))

        # Audit: write result to Redis Stream for full round-trip tracking
        ai_mgr.audit_result(session.session_id, project_id, raw)

        if raw.get("status") != "completed":
            tlog.log_event("ai_session_failed", {"status": raw.get("status")})
            raise RuntimeError(f"Dev AI failed: {raw.get('status')} {raw.get('stderr','')[:200]}")

        # 5. Parse dev output
        dev_output = parse_ai_output(raw.get("stdout", ""), role="dev")
        tlog.log_event("output_parsed", {"keys": list(dev_output.keys())})

        # 6. Collect real evidence (independent, don't trust AI)
        real_evidence = evidence.collect_after_dev(before)
        tlog.write_json("evidence.json", real_evidence.to_dict() if hasattr(real_evidence, 'to_dict') else {})
        tlog.log_event("evidence_collected", {
            "changed_files": real_evidence.changed_files if hasattr(real_evidence, 'changed_files') else [],
            "test_passed": real_evidence.test_results.get("passed") if hasattr(real_evidence, 'test_results') else None,
        })

        # 7. Compare AI report vs real evidence
        comparison = evidence.compare_with_ai_report(real_evidence, dev_output)
        tlog.write_json("validator.json", comparison)
        if comparison.get("has_discrepancies"):
            print(f"[executor-v6] evidence discrepancy: {comparison['discrepancies']}")

        # 8. Build result (use real evidence, not AI self-report)
        result = {
            **task,
            "status": "completed",
            "completed_at": utc_iso(),
            "executor": {
                "action": "dev_task_v6",
                "branch": branch,
                "changed_files": real_evidence.changed_files,
                "new_files": real_evidence.new_files,
                "test_passed": real_evidence.test_results.get("passed", False),
                "diff_stat": real_evidence.diff_stat,
                "ai_summary": dev_output.get("summary", ""),
                "discrepancies": comparison.get("discrepancies", []),
                "evidence": real_evidence.to_dict() if hasattr(real_evidence, 'to_dict') else {},
            },
            "_git_checkpoint": before.get("commit", ""),
        }
        save_json(processing, result)

        # 9. Notify user
        if chat_id:
            summary = dev_output.get("summary", "Dev 完成")
            files = ", ".join(real_evidence.changed_files[:5])
            test_ok = "pass" if real_evidence.test_results.get("passed") else "fail"
            _gateway_notify(chat_id,
                f"Dev 完成 ({branch})\n"
                f"改动: {files}\n"
                f"测试: {test_ok}\n"
                f"摘要: {summary[:200]}")

        # 10. Cleanup worktree or checkout main
        # L24.4: Unregister worktree from file API
        try:
            from executor_api import unregister_worktree
            unregister_worktree(task_id)
        except ImportError:
            pass

        try:
            if worktree_dir and os.path.exists(worktree_dir):
                # Worktree mode: remove worktree (branch stays for review)
                subprocess.run(["git", "worktree", "remove", worktree_dir, "--force"],
                    cwd=main_workspace, capture_output=True, timeout=30)
                tlog.log_event("git_worktree_removed", {"worktree": worktree_dir})
                print(f"[executor-v6] worktree removed: {worktree_dir} (branch {branch} kept)")
            # Always ensure main workspace returns to main branch
            # Prevents branch pollution for observer/manual edits between tasks
            cur_branch = subprocess.run(["git", "branch", "--show-current"],
                cwd=main_workspace, capture_output=True, text=True, timeout=5)
            if cur_branch.stdout.strip() != "main":
                subprocess.run(["git", "checkout", "main"],
                    cwd=main_workspace, capture_output=True, timeout=10)
                print(f"[executor-v6] restored main workspace to main branch")
        except Exception as e:
            tlog.log_event("git_restore_error", {"error": str(e)[:200]})

        return result

    except ImportError as e:
        error_msg = f"v6 module import failed: {e}"
        print(f"[executor-v6] {error_msg}")
        tlog.log_event("v6_import_error", {"error": str(e)})
        _cleanup_dev_branch(branch, workspace)
        result = {**task, "status": "failed", "error": error_msg, "completed_at": utc_iso()}
        save_json(processing, result)
        return result
    except Exception as e:
        error_msg = str(e)[:500]
        if chat_id:
            _gateway_notify(chat_id, f"Dev v6 执行失败: {error_msg[:200]}")
        _cleanup_dev_branch(branch, workspace)
        result = {**task, "status": "failed", "error": error_msg, "completed_at": utc_iso()}
        save_json(processing, result)
        return result


def _cleanup_dev_branch(branch: str, workspace: str) -> None:
    """Cleanup dev branch on failure: checkout main + delete branch."""
    if not branch or not branch.startswith("dev/"):
        return
    try:
        subprocess.run(["git", "checkout", "main"], cwd=workspace, capture_output=True, timeout=10)
        subprocess.run(["git", "branch", "-D", branch], cwd=workspace, capture_output=True, timeout=10)
        print(f"[executor-v6] cleaned up branch: {branch}")
    except Exception:
        pass


def process_test_task_v6(task: Dict, processing: Path) -> Dict:
    """Run real tests via subprocess, not AI review."""
    task_id = task.get("task_id", "")
    project_id = task.get("project_id", "")
    chat_id = int(task.get("chat_id", 0))
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    tlog = TaskLogger(task_id)
    tlog.log_event("test_task_start", {"project_id": project_id})
    # Use project root, not thread-local workspace (which may be search-workspace)
    workspace = os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parent.parent))

    try:
        import re as _re
        tlog.log_event("running_unittest")
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", os.path.join(workspace, "agent", "tests"), "-t", workspace, "-p", "test_*.py"],
            cwd=workspace, capture_output=True, text=True, timeout=300)
        test_output = r.stdout + r.stderr
        tlog.write_file("test_output.txt", test_output)
        m = _re.search(r"Ran (\d+) tests?", test_output)
        ran = int(m.group(1)) if m else 0
        test_passed = r.returncode == 0 or "OK" in test_output
        fm = _re.search(r"failures?=(\d+)", test_output)
        em = _re.search(r"errors?=(\d+)", test_output)
        failed = int(fm.group(1) if fm else 0) + int(em.group(1) if em else 0)
        tlog.log_event("unittest_complete", {"ran": ran, "failed": failed, "passed": test_passed})

        result = {**task, "status": "completed" if test_passed else "failed", "completed_at": utc_iso(),
                  "executor": {"action": "test_task_v6", "ran": ran, "failed": failed, "passed": test_passed}}
        save_json(processing, result)
        if chat_id:
            _gateway_notify(chat_id, f"Test {'PASS' if test_passed else 'FAIL'}: {ran} tests, {failed} failures")
        return result
    except Exception as e:
        result = {**task, "status": "failed", "error": str(e)[:500], "completed_at": utc_iso()}
        save_json(processing, result)
        return result


def process_qa_task_v6(task: Dict, processing: Path) -> Dict:
    """QA: run verify_loop + gatekeeper. Code-driven."""
    task_id = task.get("task_id", "")
    project_id = task.get("project_id", "")
    chat_id = int(task.get("chat_id", 0))
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    tlog = TaskLogger(task_id)
    tlog.log_event("qa_task_start", {"project_id": project_id})
    workspace = os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parent.parent))

    try:
        # Read task-level verification config (set by PM); missing = run all checks
        verification = task.get("_verification", {})
        skipped_checks = []

        # 1. Try governance verify_loop
        verify_pass = False
        verify_unavailable = False
        if verification.get("verify_loop") is False:
            verify_pass = True
            skipped_checks.append("verify_loop")
            tlog.log_event("verify_loop_skipped", {"reason": "verification.verify_loop=false"})
        else:
            tlog.log_event("running_verify_loop")
            try:
                vl = subprocess.run(["bash", "scripts/verify_loop.sh", token, project_id],
                    cwd=workspace, capture_output=True, text=True, timeout=60)
                tlog.write_file("verify_loop.txt", vl.stdout + vl.stderr)
                verify_pass = "0 fail" in vl.stdout
                tlog.log_event("verify_loop_complete", {"passed": verify_pass})
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                verify_unavailable = True
                tlog.log_event("verify_loop_unavailable", {"error": str(e)[:200]})

        # 2. Try governance gatekeeper
        gate_pass = False
        gate_unavailable = False
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        if verification.get("release_gate") is False:
            gate_pass = True
            skipped_checks.append("release_gate")
            tlog.log_event("release_gate_skipped", {"reason": "verification.release_gate=false"})
        else:
            tlog.log_event("running_gatekeeper")
            try:
                gate = requests.get(f"{gov_url}/api/wf/{project_id}/release-gate",
                    headers={"X-Gov-Token": token}, timeout=15).json()
                gate_pass = gate.get("release", False) and gate.get("gatekeeper", {}).get("pass", False)
                tlog.log_event("gatekeeper_complete", {"passed": gate_pass})
            except Exception as e:
                gate_unavailable = True
                tlog.log_event("gatekeeper_unavailable", {"error": str(e)[:200]})

        # Governance node checks (skipped if explicitly disabled)
        if verification.get("governance_nodes") is False:
            skipped_checks.append("governance_nodes")
            tlog.log_event("governance_nodes_skipped", {"reason": "verification.governance_nodes=false"})

        if skipped_checks:
            tlog.log_event("checks_skipped", {"skipped": skipped_checks})

        # 3. Determine QA result with fallback
        # If governance is unavailable, fallback to parent test results (Codex advice: mark explicitly)
        qa_status = "failed"
        fallback_used = False
        if verify_pass and gate_pass:
            qa_status = "completed"
        elif (verify_unavailable or gate_unavailable):
            # Check if parent test task passed (fallback evidence)
            parent_test_passed = task.get("executor", {}).get("test_passed", False)
            if parent_test_passed or task.get("_parent_test_passed", False):
                qa_status = "passed_with_fallback"
                fallback_used = True
                tlog.log_event("qa_fallback", {
                    "reason": "governance unavailable, using test results as evidence",
                    "verify_unavailable": verify_unavailable,
                    "gate_unavailable": gate_unavailable,
                })
            # else: governance unavailable AND no test evidence → fail

        result = {**task,
                  "status": qa_status,
                  "completed_at": utc_iso(),
                  "executor": {
                      "action": "qa_task_v6",
                      "verify_pass": verify_pass,
                      "gate_pass": gate_pass,
                      "verify_unavailable": verify_unavailable,
                      "gate_unavailable": gate_unavailable,
                      "fallback_used": fallback_used,
                      "skipped_checks": skipped_checks,
                  }}
        save_json(processing, result)

        if qa_status in ("completed", "passed_with_fallback"):
            tlog.log_event("triggering_auto_merge", {"qa_status": qa_status})
            branch = task.get("_branch", "")
            if not branch:
                br = subprocess.run(["git", "branch", "--list", "dev/*", "--sort=-committerdate"],
                    cwd=workspace, capture_output=True, text=True, timeout=5)
                branches = [b.strip().lstrip("* ") for b in br.stdout.strip().split("\n") if b.strip()]
                branch = branches[0] if branches else ""
            if branch:
                # When verification says no governance nodes or release_gate is False, skip deploy checks
                verification = task.get("_verification", {})
                skip_deploy = (
                    not verification.get("governance_nodes", True)
                    or verification.get("release_gate") is False
                )
                merge_args = ["bash", "scripts/merge-and-deploy.sh", branch]
                if skip_deploy:
                    merge_args.append("--skip-deploy")
                mr = subprocess.run(merge_args,
                    cwd=workspace, capture_output=True, text=True, timeout=180,
                    env={**os.environ, "GOV_COORDINATOR_TOKEN": token})
                tlog.write_file("merge_output.txt", mr.stdout + mr.stderr)
                tlog.log_event("auto_merge", {"passed": mr.returncode == 0, "branch": branch})
                if mr.returncode == 0:
                    try:
                        from bot_commands import write_manager_signal
                        write_manager_signal("graceful_restart", {"task_id": task_id, "branch": branch}, chat_id or 0)
                    except Exception as sig_err:
                        print(f"[executor] Failed to write manager_signal: {sig_err}")

                    # --- Deploy chain (non-blocking) ---
                    try:
                        from deploy_chain import run_deploy  # noqa: PLC0415
                        # Prefer changed_files from executor result, fall back to git diff vs main
                        changed_files: list = (
                            task.get("executor", {}).get("changed_files")
                            or task.get("_changed_files")
                            or []
                        )
                        if not changed_files:
                            try:
                                gd = subprocess.run(
                                    ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                                    cwd=workspace, capture_output=True, text=True, timeout=10,
                                )
                                changed_files = [f for f in gd.stdout.strip().splitlines() if f]
                            except Exception as gd_err:
                                tlog.log_event("deploy_chain_git_diff_failed", {"error": str(gd_err)[:200]})

                        tlog.log_event("deploy_chain_starting", {"changed_files": changed_files, "branch": branch})

                        def _run_deploy_bg(files: list, cid: int) -> None:
                            try:
                                deploy_result = run_deploy(files, cid)
                                try:
                                    tlog.log_event("deploy_chain_complete", {"result": deploy_result})
                                except Exception:
                                    pass  # Log dir may not exist in tests
                                if cid:
                                    services = deploy_result.get("services_restarted", []) if isinstance(deploy_result, dict) else []
                                    svc_label = ", ".join(services) if services else "none"
                                    _gateway_notify(cid, f"🚀 Deploy complete — services restarted: {svc_label}")
                            except Exception as dep_err:
                                try:
                                    tlog.log_event("deploy_chain_error", {"error": str(dep_err)[:300]})
                                except Exception:
                                    pass
                                if cid:
                                    _gateway_notify(cid, f"⚠️ Deploy chain error: {str(dep_err)[:200]}")

                        threading.Thread(
                            target=_run_deploy_bg,
                            args=(changed_files, chat_id),
                            daemon=True,
                            name=f"deploy-{task_id}",
                        ).start()
                    except ImportError:
                        tlog.log_event("deploy_chain_unavailable", {"reason": "deploy_chain module not found"})

                if chat_id:
                    if mr.returncode == 0 and skip_deploy:
                        _gateway_notify(chat_id, f"✅ Merged to main (deploy not required for this task)")
                    elif mr.returncode == 0:
                        # Identify which changed files will trigger service restarts for the notification
                        _notify_files = (
                            task.get("executor", {}).get("changed_files")
                            or task.get("_changed_files")
                            or []
                        )
                        files_label = ", ".join(_notify_files[:5]) if _notify_files else "unknown"
                        _gateway_notify(
                            chat_id,
                            f"✅ Auto-merge OK: {branch} — deploying changed files: {files_label}"
                            + (" …" if len(_notify_files) > 5 else ""),
                        )
                    else:
                        _gateway_notify(chat_id, f"Auto-merge FAIL: {branch}")

        if chat_id:
            status_label = {"completed": "PASS", "passed_with_fallback": "PASS (fallback)", "failed": "FAIL"}
            _gateway_notify(chat_id, f"QA {status_label.get(qa_status, qa_status)}")
        return result
    except Exception as e:
        result = {**task, "status": "failed", "error": str(e)[:500], "completed_at": utc_iso()}
        save_json(processing, result)
        return result


def _trigger_coordinator_eval(task: Dict, result: Dict) -> None:
    """Auto-trigger coordinator eval after dev/test task completes.

    v6: Executor code creates eval task. AI doesn't control this flow.

    Chain-depth guard: if _chain_depth >= 3 in the task file, skip eval and
    archive a 'chain_limit' event instead to prevent infinite auto-chains.
    """
    chain_depth = int(task.get("_chain_depth", 0))

    if chain_depth >= 3:
        print(f"[executor] chain_depth={chain_depth} >= 3, skipping eval (chain_limit)")
        try:
            from task_orchestrator import TaskOrchestrator
            orchestrator = TaskOrchestrator()
            orchestrator._auto_archive(
                task.get("project_id", ""),
                task["task_id"],
                None,
                {},
                trigger_reason="chain_limit",
            )
        except Exception as e:
            print(f"[executor] chain_limit archive failed: {e}")
        return

    try:
        from task_orchestrator import TaskOrchestrator
        orchestrator = TaskOrchestrator()
        orchestrator.handle_dev_complete(
            task_id=task["task_id"],
            project_id=task.get("project_id", ""),
            token=task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", "")),
            chat_id=int(task.get("chat_id", 0)),
            ai_report={
                "summary": result.get("acceptance", {}).get("summary", ""),
                "changed_files": result.get("executor", {}).get("changed_files", result.get("executor", {}).get("git_changed_files", [])),
                "test_results": result.get("executor", {}).get("test_results", {}),
                "_before_snapshot": {"commit": task.get("_git_checkpoint", "HEAD~1")},
                "_chain_depth": chain_depth,
                "_evidence": result.get("executor", {}).get("evidence", {}),
            },
        )
    except ImportError:
        # task_orchestrator not available yet, skip
        print("[executor] task_orchestrator not available, skipping eval trigger")
    except Exception as e:
        print(f"[executor] coordinator eval error: {e}")


def process_coordinator_chat(task: Dict, processing: Path) -> Dict:
    """Process coordinator_chat via TaskOrchestrator (v6.2).

    Routes through:
      ContextAssembler (对话历史+记忆+状态) →
      AILifecycleManager (启动 Claude CLI) →
      DecisionValidator (4层校验) →
      回复 + 保存对话历史
    """
    prompt_text = task.get("prompt", "")
    project_id = task.get("project_id", "")
    chat_id = int(task.get("chat_id", 0))
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    tlog = TaskLogger(task.get("task_id", "coord-unknown"))
    tlog.log_event("coordinator_chat_start", {"prompt": prompt_text[:200], "project_id": project_id})
    tlog.write_file("prompt.txt", prompt_text)

    try:
        from task_orchestrator import TaskOrchestrator
        orchestrator = TaskOrchestrator()
        tlog.log_event("orchestrator_loaded")

        # Use TaskOrchestrator — handles context, validation, reply, history
        api_result = orchestrator.handle_user_message(
            chat_id=chat_id,
            text=prompt_text,
            project_id=project_id,
            token=token,
        )

        reply = api_result.get("reply", "处理完成")
        tlog.log_event("coordinator_reply", {
            "reply_len": len(reply),
            "actions_executed": api_result.get("actions_executed", 0),
            "actions_rejected": api_result.get("actions_rejected", 0),
        })
        tlog.write_file("reply.txt", reply)

        # Send reply to Telegram via Gateway
        if chat_id:
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            try:
                import json as _json
                requests.post(
                    f"{gov_url}/gateway/reply",
                    headers={"Content-Type": "application/json", "X-Gov-Token": token},
                    data=_json.dumps({"chat_id": chat_id, "text": _escape_telegram(reply[:4000])}),
                    timeout=10,
                )
            except Exception:
                pass

        result = {
            **task,
            "status": "completed",
            "completed_at": utc_iso(),
            "executor": {
                "action": "coordinator_chat",
                "reply": reply[:1000],
                "actions_executed": api_result.get("actions_executed", 0),
                "actions_rejected": api_result.get("actions_rejected", 0),
            },
        }
        save_json(processing, result)
        return result

    except Exception as e:
        error_msg = str(e)[:500]
        if chat_id:
            _gateway_notify(chat_id, f"Coordinator 处理失败: {error_msg[:200]}")
        result = {**task, "status": "failed", "error": error_msg, "completed_at": utc_iso()}
        save_json(processing, result)
        return result


def _gateway_notify(chat_id: int, text: str) -> None:
    """Send notification via Gateway API (not direct Telegram)."""
    if not chat_id:
        return
    try:
        import requests
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        token = os.getenv("GOV_COORDINATOR_TOKEN", "")
        text = _escape_telegram(text)
        requests.post(f"{gov_url}/gateway/reply",
            headers={"Content-Type": "application/json", "X-Gov-Token": token},
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=10)
    except Exception:
        pass  # Non-critical


def _escape_telegram(text: str) -> str:
    """Escape MarkdownV2 special chars or strip markdown for plain text."""
    # Simple approach: strip common markdown formatting for plain text
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Remove `code` → code
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove _italic_ → italic
    text = re.sub(r'_(.+?)_', r'\1', text)
    return text


# ── Health Check Server ───────────────────────────────────────────────────────

_HEALTH_DEGRADED_THRESHOLD = int(os.getenv("EXECUTOR_HEALTH_DEGRADED_THRESHOLD", "10"))
_executor_start_time: float = time.time()


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for GET /health.

    _base_path and _start_time are set per-server via a subclass created in
    start_health_server(), so requests always use the correct task directories.
    """
    _base_path: Path = None  # overridden by start_health_server
    _start_time: float = 0.0  # overridden by start_health_server

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        try:
            base = self._base_path
            processing_dir = base / "processing"
            pending_dir = base / "pending"
            active = len(list(processing_dir.glob("*.json"))) if processing_dir.exists() else 0
            queued = len(
                [p for p in pending_dir.glob("*.json") if not p.name.endswith(".tmp.json")]
            ) if pending_dir.exists() else 0
        except Exception:
            active, queued = 0, 0
        threshold = int(os.getenv("EXECUTOR_HEALTH_DEGRADED_THRESHOLD", str(_HEALTH_DEGRADED_THRESHOLD)))
        status = "degraded" if active > threshold else "ok"
        body = _json.dumps({
            "status": status,
            "active_count": active,
            "queued_count": queued,
            "uptime_seconds": int(time.time() - self._start_time),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass  # Suppress default access log noise


def start_health_server() -> None:
    """Start HTTP health check server in a background daemon thread.

    Port: EXECUTOR_HEALTH_PORT env var (default 40020).
    Captures tasks_root() at call time so the handler always uses the correct
    path even after env vars change.  Exceptions are isolated — a failure here
    never crashes the main executor.
    """
    global _executor_start_time
    _executor_start_time = time.time()
    port = int(os.getenv("EXECUTOR_HEALTH_PORT", "40020"))
    base = tasks_root()  # Capture at startup; not re-evaluated per request
    # Create a per-server handler subclass with bound path and start time
    handler_cls = type("_BoundHealthHandler", (_HealthHandler,), {
        "_base_path": base,
        "_start_time": _executor_start_time,
    })
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), handler_cls)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="health-check")
        t.start()
        print(f"[executor] health check server started on port {port}")
    except OSError as e:
        print(f"[executor] health check server port {port} conflict: {e}")
    except Exception as e:
        print(f"[executor] health check server failed to start: {e}")


def _recover_stale_tasks() -> int:
    """Startup recovery: scan processing/ for stale tasks and re-queue or mark failed."""
    processing_dir = tasks_root() / "processing"
    if not processing_dir.exists():
        return 0
    recovered = 0
    for f in processing_dir.glob("*.json"):
        try:
            task = load_json(f)
            task_id = task.get("task_id", "")
            # Skip tasks taken over by observer
            if task.get("status") == "manual_override":
                continue
            # Check if stale (no heartbeat for > 5 minutes)
            import datetime
            started = task.get("started_at", "")
            if started:
                try:
                    ts = datetime.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
                    age_min = (datetime.datetime.utcnow() - ts).total_seconds() / 60
                    if age_min < 5:
                        continue  # Still fresh, might be running
                except Exception:
                    pass
            # Check if worker process is alive
            worker_pid = task.get("worker_pid")
            if worker_pid:
                try:
                    os.kill(int(worker_pid), 0)  # signal 0 = check alive
                    continue  # Process alive, skip
                except (OSError, ProcessLookupError):
                    # Process dead → kill its tree just in case
                    try:
                        import subprocess
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(worker_pid)],
                                     capture_output=True, timeout=5)
                    except Exception:
                        pass

            # Re-queue: move back to pending
            pending_path = tasks_root() / "pending" / f.name
            shutil.move(str(f), str(pending_path))
            print(f"[executor] recovered stale task: {task_id}")
            recovered += 1
        except Exception as e:
            print(f"[executor] recovery error for {f.name}: {e}")
    return recovered


def _register_lease() -> str:
    """Register executor lease with governance. Returns lease_id."""
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    if not token:
        return ""
    result = _gov_api("POST", "/api/agent/register",
        data={
            "project_id": os.getenv("GOV_PROJECT_ID", "amingClaw"),
            "expected_duration_sec": 86400,
            "worker_id": _worker_id(),
            "worker_pid": os.getpid(),
        },
        token=token)
    lease_id = result.get("lease_id", "")
    if lease_id:
        print(f"[executor] registered lease: {lease_id}")
    return lease_id


def _heartbeat_lease(lease_id: str, status: str = "idle") -> None:
    """Renew lease heartbeat."""
    if not lease_id:
        return
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    _gov_api("POST", "/api/agent/heartbeat",
        data={"lease_id": lease_id, "status": status, "worker_pid": os.getpid()},
        token=token)


def _deregister_lease(lease_id: str) -> None:
    """Release lease on exit."""
    if not lease_id:
        return
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    _gov_api("POST", "/api/agent/deregister",
        data={"lease_id": lease_id},
        token=token)
    print(f"[executor] deregistered lease: {lease_id}")


def _reconcile_stale_claimed() -> int:
    """Reconcile stale 'claimed' tasks in governance DB on startup.

    Checks each claimed task against local file system:
    - In processing/ → still active, skip
    - In results/ → already done, update governance to completed
    - Not found → stale, reset to failed
    """
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    if not token:
        return 0
    reconciled = 0
    try:
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        # Query all claimed tasks for known projects
        for pid in ("amingClaw", "toolboxClient"):
            try:
                resp = requests.get(
                    f"{gov_url}/api/task/{pid}",
                    headers={"X-Gov-Token": token},
                    params={"status": "claimed"},
                    timeout=10)
                if resp.status_code != 200:
                    continue
                tasks = resp.json().get("tasks", [])
                root = tasks_root()
                for t in tasks:
                    tid = t.get("task_id", "")
                    if not tid:
                        continue
                    in_processing = (root / "processing" / f"{tid}.json").exists()
                    in_results = (root / "results" / f"{tid}.json").exists()
                    if in_processing:
                        continue  # Still active
                    new_status = "completed" if in_results else "failed"
                    try:
                        requests.put(
                            f"{gov_url}/api/task/{pid}/{tid}/status",
                            headers={"X-Gov-Token": token, "Content-Type": "application/json"},
                            json={"status": new_status, "reason": "startup_reconcile"},
                            timeout=5)
                        print(f"[executor] reconciled {tid}: claimed → {new_status}")
                        reconciled += 1
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception as e:
        print(f"[executor] reconcile failed: {e}")
    return reconciled


def _cleanup_orphans() -> int:
    """Check for orphaned agents and kill zombie processes.

    Two strategies:
    1. Query /api/agent/orphans for sessions with worker_pid
    2. Scan OS processes for claude.exe not tracked by any active task
    """
    import signal
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    cleaned = 0

    # Strategy 1: Governance orphan API (may have worker_pid)
    if token:
        try:
            result = _gov_api("GET", "/api/agent/orphans", token=token)
            orphans = result.get("orphans", [])
            for orphan in orphans:
                pid = orphan.get("worker_pid")
                session_id = orphan.get("session_id", "")
                if pid:
                    try:
                        os.kill(int(pid), 0)
                        print(f"[executor] killing orphan PID={pid} (session={session_id})")
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                capture_output=True, timeout=10)
                        else:
                            os.kill(int(pid), signal.SIGTERM)
                        cleaned += 1
                    except (ProcessLookupError, OSError):
                        pass

            if orphans:
                project_id = os.getenv("GOV_PROJECT_ID", "amingClaw")
                _gov_api("POST", "/api/agent/cleanup",
                    data={"project_id": project_id}, token=token)
        except Exception as e:
            print(f"[executor] orphan API check failed: {e}")

    # Strategy 2: Only kill claude.exe processes that the executor SPAWNED.
    # NEVER kill arbitrary claude.exe processes — they may be user sessions
    # (e.g., Claude Code CLI) that are unrelated to the executor.
    if sys.platform == "win32" and _EXECUTOR_SPAWNED_PIDS:
        stale_pids = set()
        processing_dir = tasks_root() / "processing"
        tracked_pids = set()
        if processing_dir.exists():
            for f in processing_dir.glob("*.json"):
                try:
                    t = load_json(f)
                    wp = t.get("worker_pid", 0)
                    if wp:
                        tracked_pids.add(int(wp))
                except Exception:
                    pass
        for pid in list(_EXECUTOR_SPAWNED_PIDS):
            if pid in tracked_pids:
                continue
            try:
                os.kill(pid, 0)  # Check alive
                stale_pids.add(pid)
            except (ProcessLookupError, OSError):
                _EXECUTOR_SPAWNED_PIDS.discard(pid)
        for pid in stale_pids:
            try:
                print(f"[executor] killing executor-spawned orphan PID={pid}")
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10)
                _EXECUTOR_SPAWNED_PIDS.discard(pid)
                cleaned += 1
            except Exception:
                pass

    return cleaned


def run() -> None:
    """Serial executor loop (original mode). Picks tasks one at a time."""
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[executor] another executor instance is already running; exit")
        return

    # Register graceful-shutdown signal handlers
    _shutdown_requested.clear()
    try:
        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)
    except (OSError, ValueError):
        pass  # Signal registration may fail in non-main threads or on some platforms

    # Startup recovery
    recovered = _recover_stale_tasks()
    if recovered:
        print(f"[executor] recovered {recovered} stale tasks")

    # Orphan cleanup
    cleaned = _cleanup_orphans()
    if cleaned:
        print(f"[executor] cleaned {cleaned} orphan processes")

    # Register lease
    lease_id = _register_lease()

    poll_sec = float(os.getenv("EXECUTOR_POLL_SEC", "1"))
    orphan_interval = 60  # Check orphans every 60s
    last_orphan_check = time.time()

    # Start Executor API server for session intervention
    try:
        from executor_api import start_api_server, set_shared_state
        start_api_server()
        # Share v6 components with API if available
        try:
            from task_orchestrator import TaskOrchestrator
            from ai_lifecycle import AILifecycleManager
            orch = TaskOrchestrator()
            set_shared_state(ai_manager=orch.ai_manager, orchestrator=orch)
            print("[executor] v6 orchestrator initialized")
        except Exception as e:
            print(f"[executor] v6 orchestrator not available: {e}")
    except Exception as e:
        print(f"[executor] API server failed to start: {e}")

    # Start health check HTTP server
    start_health_server()

    print("[executor] started (serial mode)")
    try:
        while not _shutdown_requested.is_set():
            try:
                # Periodic orphan check + lease heartbeat
                now = time.time()
                if now - last_orphan_check >= orphan_interval:
                    _cleanup_orphans()
                    _heartbeat_lease(lease_id, status="idle")
                    last_orphan_check = now

                pending = pick_pending_task()
                if pending is not None:
                    _heartbeat_lease(lease_id, status="busy")
                    process_task(pending)
                    _heartbeat_lease(lease_id, status="idle")
                else:
                    time.sleep(poll_sec)
            except Exception as exc:
                if _shutdown_requested.is_set():
                    break
                print("[executor] error:", exc)
                time.sleep(max(1.0, poll_sec))
        print("[executor] stopped by shutdown request")
    finally:
        _wait_for_idle_or_timeout()
        _deregister_lease(lease_id)


# ── Parallel mode ────────────────────────────────────────────────────────────

def process_task_in_workspace(task_path: Path, workspace_info: Dict) -> None:
    """Process a task within a specific workspace context.

    This is the callback used by ParallelDispatcher's WorkspaceWorker.
    It sets the thread-local workspace before delegating to process_task().
    """
    from workspace import thread_workspace_context
    ws_path = Path(workspace_info["path"])
    with thread_workspace_context(ws_path):
        process_task(task_path)


_MANAGER_SINGLETON_PORT: int = int(os.getenv("MANAGER_SINGLETON_PORT", "39103"))


def _is_service_manager_running() -> bool:
    """Return True if service_manager's singleton port is bound (i.e. already running)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", _MANAGER_SINGLETON_PORT))
        # We could bind → port is free → service_manager is NOT running
        sock.close()
        return False
    except OSError:
        # Port already bound → service_manager IS running
        try:
            sock.close()
        except Exception:
            pass
        return True


def _ensure_service_manager_running() -> None:
    """Start service_manager subprocess if it is not already running."""
    global _service_manager_proc
    if _is_service_manager_running():
        print("[executor] service_manager already running")
        return
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen(
        [sys.executable, "-m", "service_manager"],
        cwd=agent_dir,
    )
    _service_manager_proc = proc
    print(f"[executor] service_manager started (PID={proc.pid})")


def _cleanup_service_manager() -> None:
    """Terminate the service_manager subprocess started by this executor, if any."""
    global _service_manager_proc
    if _service_manager_proc is None:
        return
    proc = _service_manager_proc
    _service_manager_proc = None
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print(f"[executor] service_manager (PID={proc.pid}) stopped")


def run_parallel() -> None:
    """Parallel executor: dispatches tasks to workspace-specific worker threads."""
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[executor] another executor instance is already running; exit")
        return

    # Register graceful-shutdown signal handlers
    _shutdown_requested.clear()
    try:
        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)
    except (OSError, ValueError):
        pass  # Signal registration may fail in non-main threads or on some platforms

    from parallel_dispatcher import get_dispatcher, shutdown_dispatcher
    from workspace_registry import list_workspaces, ensure_current_workspace_registered

    # Ensure at least the current workspace is registered
    ensure_current_workspace_registered()

    # Ensure service_manager is running so it can restart executor after code merges
    _ensure_service_manager_running()

    workspaces = list_workspaces()
    if not workspaces:
        print("[executor] no workspaces registered, falling back to serial mode")
        run()
        return

    # Startup recovery: re-queue stale tasks from processing/
    recovered = _recover_stale_tasks()
    if recovered:
        print(f"[executor] recovered {recovered} stale tasks")

    # Reconcile stale claimed tasks in governance DB
    reconciled = _reconcile_stale_claimed()
    if reconciled:
        print(f"[executor] reconciled {reconciled} stale claimed tasks in governance")

    # Orphan cleanup
    cleaned = _cleanup_orphans()
    if cleaned:
        print(f"[executor] cleaned {cleaned} orphan processes")

    dispatcher = get_dispatcher(task_processor=process_task_in_workspace)
    dispatcher.start()

    # Start Executor API server for session intervention
    try:
        from executor_api import start_api_server, set_shared_state
        start_api_server()
        try:
            from task_orchestrator import TaskOrchestrator
            orch = TaskOrchestrator()
            set_shared_state(ai_manager=orch.ai_manager, orchestrator=orch)
            print("[executor] v6 orchestrator initialized")
        except Exception as e:
            print(f"[executor] v6 orchestrator not available: {e}")
    except Exception as e:
        print(f"[executor] API server failed to start: {e}")

    poll_sec = float(os.getenv("EXECUTOR_POLL_SEC", "1"))
    refresh_interval = float(os.getenv("DISPATCHER_REFRESH_SEC", "30"))
    last_refresh = time.time()

    # Start health check HTTP server
    start_health_server()

    print("[executor] started (parallel mode, {} workspaces)".format(len(workspaces)))

    _skipped_tasks: set = set()  # Tasks we already tried to dispatch but queue was full

    try:
        while not _shutdown_requested.is_set():
            try:
                pending = pick_pending_task()
                if pending is not None:
                    task_id = pending.stem  # filename without .json
                    if task_id in _skipped_tasks:
                        # Already tried, queue was full — don't spin, wait
                        time.sleep(poll_sec)
                        continue
                    # Move to processing/ BEFORE dispatching to prevent
                    # the main loop from re-picking and duplicating the task.
                    processing_path = move_task(pending, "processing")
                    if not dispatcher.dispatch(processing_path):
                        # Queue full — move back to pending for later retry
                        try:
                            move_task(processing_path, "pending")
                        except Exception:
                            pass
                        _skipped_tasks.add(task_id)
                        print(f"[executor] dispatch failed for {task_id}, will retry later")
                        time.sleep(poll_sec)
                else:
                    time.sleep(poll_sec)
                    # Clear skipped set periodically — workers may have freed up
                    _skipped_tasks.clear()

                # Periodically refresh workers from registry
                now = time.time()
                if now - last_refresh >= refresh_interval:
                    dispatcher.refresh_workers()
                    _skipped_tasks.clear()  # Re-check skipped tasks after refresh
                    last_refresh = now

            except Exception as exc:
                if _shutdown_requested.is_set():
                    break
                print("[executor] error:", exc)
                time.sleep(max(1.0, poll_sec))
        print("[executor] stopping parallel dispatcher...")
    finally:
        _wait_for_idle_or_timeout()
        shutdown_dispatcher()
        _cleanup_service_manager()
        print("[executor] stopped")


def acquire_single_instance_lock() -> Optional[socket.socket]:
    port = int(os.getenv("EXECUTOR_SINGLETON_PORT", "39101"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return None


if __name__ == "__main__":
    mode = os.getenv("EXECUTOR_MODE", "auto").strip().lower()
    if mode == "parallel":
        run_parallel()
    elif mode == "serial":
        run()
    else:
        # Auto: use parallel if multiple workspaces registered, serial otherwise
        from workspace_registry import list_workspaces as _lw
        if len(_lw()) > 1:
            run_parallel()
        else:
            run()
