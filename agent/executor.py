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
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

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
from backends import (
    process_codex,
    process_claude,
    process_pipeline,
    resolve_workspace,
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
)


def pick_pending_task() -> Optional[Path]:
    pending_dir = tasks_root() / "pending"
    items = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
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
    chat_id = int(task["chat_id"])

    sent = []
    for path in files:
        p = Path(path)
        if p.exists():
            send_document(chat_id, p, caption="screenshot: {}".format(p.name))
            sent.append(str(p))

    send_text(chat_id, "截图完成，已回传 {} 张图片。".format(len(sent)))

    timings = details.get("timings_ms") or {}
    if wants_timing(task):
        send_text(
            chat_id,
            "截图耗时: total={}ms, capture={}ms, copy={}ms, gateway={}ms".format(
                timings.get("total_ms", 0),
                timings.get("capture_ms", 0),
                timings.get("copy_ms", 0),
                gateway.get("_elapsed_ms", 0),
            ),
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
    return "executor-{}".format(socket.gethostname())


def process_task(path: Path) -> None:
    task = load_json(path)
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

    processing = move_task(path, "processing")
    task = load_json(processing)
    task["status"] = "processing"
    task["started_at"] = utc_iso()
    task["updated_at"] = utc_iso()
    task["worker_id"] = _worker_id()
    task["attempt"] = int(task.get("attempt") or 0) + 1
    task.setdefault("action", get_agent_backend())
    save_json(processing, task)
    update_task_runtime(task, status="processing", stage="processing")
    mark_task_started(task, stage="processing")

    # Start heartbeat thread
    _hb_stop = threading.Event()
    _hb_interval = float(os.getenv("EXECUTOR_HEARTBEAT_SEC", "30"))
    _hb_thread = threading.Thread(target=_heartbeat_loop, args=(task_id, _hb_stop, _hb_interval), daemon=True)
    _hb_thread.start()

    chat_id = int(task["chat_id"])
    silent_mode = os.getenv("TASK_SILENT_MODE", "1").strip().lower() not in {"0", "false", "no"}
    log_path: Optional[Path] = None

    # ── Git checkpoint: auto-commit uncommitted changes before task execution ──
    checkpoint_info: Dict = {}
    try:
        checkpoint_info = pre_task_checkpoint(task_id=task_id)
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
        if task.get("action") == "screenshot":
            result = process_screenshot(task, processing)
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
        # Store git checkpoint info in result for accept/reject
        result["_git_checkpoint"] = task.get("_git_checkpoint", "")
        save_json(result_path, result)

        task_code = result.get("task_code", "-")
        if not silent_mode:
            send_text(
                chat_id,
                acceptance_notice_text(result, task["task_id"], task_code, detailed=True),
                reply_markup=task_inline_keyboard(task_code, task["task_id"]),
            )
        else:
            send_text(
                chat_id,
                acceptance_notice_text(result, task["task_id"], task_code, detailed=False),
                reply_markup=task_inline_keyboard(task_code, task["task_id"]),
            )

        # Send acceptance doc TEXT CONTENT directly (not just file paths)
        doc_file = str(acceptance.get("doc_file") or "")
        if doc_file:
            doc_path = Path(doc_file)
            if doc_path.exists():
                try:
                    doc_content = doc_path.read_text(encoding="utf-8")
                    # Telegram message limit is 4096 chars; split if needed
                    _MAX_TG_MSG = 4000
                    if len(doc_content) <= _MAX_TG_MSG:
                        send_text(chat_id, doc_content)
                    else:
                        # Send in chunks
                        for i in range(0, len(doc_content), _MAX_TG_MSG):
                            chunk = doc_content[i:i + _MAX_TG_MSG]
                            send_text(chat_id, chunk)
                except Exception:
                    send_text(chat_id, "验收文档: {}".format(doc_file))

        # Add git checkpoint info to notification
        ckpt = task.get("_git_checkpoint", "")
        if ckpt:
            send_text(
                chat_id,
                "Git回滚点: {}\n验收通过将commit变更；验收拒绝将回退到此检查点。".format(ckpt[:12]),
            )

        mark_task_completion_notified(task["task_id"])
    except subprocess.TimeoutExpired:
        _action = task.get("action", "codex")
        _timeout_env = "CLAUDE_TIMEOUT_RETRIES" if _action == "claude" else "CODEX_TIMEOUT_RETRIES"
        run_data = {
            "returncode": None,
            "elapsed_ms": None,
            "cmd": None,
            "timeout_retries": int(os.getenv(_timeout_env, "1")),
            "workspace": str(resolve_workspace()),
            "git_changed_files": None,
            "noop_reason": None,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        _timeout_err = "{} timeout".format(_action)
        result = finalize_codex_task(task, processing, run_data, "failed", error=_timeout_err)
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
            error=str(result.get("error") or _timeout_err),
        )
        send_text(
            chat_id,
            "任务 [{code}] {task_id} 失败: {err}\n状态: pending_acceptance(待验收)\n通过: /accept {code}\n拒绝: /reject {code} <原因>".format(
                code=result.get("task_code", "-"),
                task_id=task["task_id"],
                err=_timeout_err,
            ),
            reply_markup=task_inline_keyboard(result.get("task_code", "-"), task["task_id"]),
        )
        mark_task_completion_notified(task["task_id"])
    except Exception as exc:
        _action = task.get("action", "codex")
        _timeout_env = "CLAUDE_TIMEOUT_RETRIES" if _action == "claude" else "CODEX_TIMEOUT_RETRIES"
        run_data = {
            "returncode": None,
            "elapsed_ms": None,
            "cmd": None,
            "timeout_retries": int(os.getenv(_timeout_env, "1")),
            "workspace": None,
            "git_changed_files": None,
            "noop_reason": None,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        result = finalize_codex_task(task, processing, run_data, "failed", error=str(exc))
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
            error=str(result.get("error") or str(exc)),
        )
        send_text(
            chat_id,
            "任务 [{code}] {task_id} 失败: {error}\n状态: pending_acceptance(待验收)\n通过: /accept {code}\n拒绝: /reject {code} <原因>".format(
                code=result.get("task_code", "-"),
                task_id=task["task_id"],
                error=str(exc)[:500],
            ),
            reply_markup=task_inline_keyboard(result.get("task_code", "-"), task["task_id"]),
        )
        mark_task_completion_notified(task["task_id"])
    finally:
        _hb_stop.set()


def run() -> None:
    """Serial executor loop (original mode). Picks tasks one at a time."""
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[executor] another executor instance is already running; exit")
        return
    poll_sec = float(os.getenv("EXECUTOR_POLL_SEC", "1"))
    print("[executor] started (serial mode)")
    while True:
        try:
            pending = pick_pending_task()
            if pending is not None:
                process_task(pending)
            else:
                time.sleep(poll_sec)
        except KeyboardInterrupt:
            print("[executor] stopped by keyboard")
            return
        except Exception as exc:
            print("[executor] error:", exc)
            time.sleep(max(1.0, poll_sec))


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


def run_parallel() -> None:
    """Parallel executor: dispatches tasks to workspace-specific worker threads."""
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[executor] another executor instance is already running; exit")
        return

    from parallel_dispatcher import get_dispatcher, shutdown_dispatcher
    from workspace_registry import list_workspaces, ensure_current_workspace_registered

    # Ensure at least the current workspace is registered
    ensure_current_workspace_registered()

    workspaces = list_workspaces()
    if not workspaces:
        print("[executor] no workspaces registered, falling back to serial mode")
        run()
        return

    dispatcher = get_dispatcher(task_processor=process_task_in_workspace)
    dispatcher.start()

    poll_sec = float(os.getenv("EXECUTOR_POLL_SEC", "1"))
    refresh_interval = float(os.getenv("DISPATCHER_REFRESH_SEC", "30"))
    last_refresh = time.time()

    print("[executor] started (parallel mode, {} workspaces)".format(len(workspaces)))

    try:
        while True:
            try:
                pending = pick_pending_task()
                if pending is not None:
                    if not dispatcher.dispatch(pending):
                        # Fallback: process in main thread if dispatch fails
                        print("[executor] dispatch failed, processing in main thread")
                        process_task(pending)
                else:
                    time.sleep(poll_sec)

                # Periodically refresh workers from registry
                now = time.time()
                if now - last_refresh >= refresh_interval:
                    dispatcher.refresh_workers()
                    last_refresh = now

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print("[executor] error:", exc)
                time.sleep(max(1.0, poll_sec))
    except KeyboardInterrupt:
        print("[executor] stopping parallel dispatcher...")
    finally:
        shutdown_dispatcher()
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
