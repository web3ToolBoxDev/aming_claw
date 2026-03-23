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
import json as _json
import shutil
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
    return "executor-{}".format(socket.gethostname())


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
        "workspace": str(resolve_workspace()) if is_timeout else None,
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
        if task.get("action") == "coordinator_chat":
            result = process_coordinator_chat(task, processing)
        elif task.get("action") == "screenshot":
            result = process_screenshot(task, processing)
        elif task.get("action") == "claude" and task.get("type") == "dev_task" and task.get("project_id"):
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
        # v6 path: dev_task/coordinator_chat with project_id → skip old finalize
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
                elif task_type == "test_task" and exec_status == "succeeded":
                    from task_orchestrator import TaskOrchestrator
                    TaskOrchestrator().handle_test_complete(
                        task_id=task["task_id"], project_id=task.get("project_id", ""),
                        token=task.get("_gov_token", ""), chat_id=chat_id,
                        test_report=result.get("executor", {}))
                # qa_task triggers merge inside process_qa_task_v6 directly
            except Exception as chain_err:
                print(f"[executor] chain trigger failed: {chain_err}")
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

        # 1. Git stash + create branch (clean workspace for evidence)
        workspace = str(resolve_workspace())
        stashed = False
        if not branch:
            branch = f"dev/{task_id}"
            try:
                stash_r = subprocess.run(
                    ["git", "stash", "push", "-m", f"auto-stash-{task_id}"],
                    cwd=workspace, capture_output=True, text=True, timeout=10)
                stashed = "Saved working directory" in stash_r.stdout
                if stashed:
                    tlog.log_event("git_stash", {"stashed": True})
                subprocess.run(["git", "checkout", "-b", branch],
                    cwd=workspace, capture_output=True, timeout=10)
                print(f"[executor-v6] created branch: {branch} (stashed={stashed})")
            except Exception as e:
                print(f"[executor-v6] branch creation failed: {e}")
                branch = ""

        # 2. Before snapshot
        before = evidence.collect_before_snapshot()

        # 3. Assemble dev context
        dev_context = ctx_asm.assemble(
            project_id=project_id, chat_id=chat_id,
            role="dev", prompt=prompt,
        )

        # 4. Start dev AI session
        session = ai_mgr.create_session(
            role="dev",
            prompt=prompt,
            context=dev_context,
            project_id=project_id,
            timeout_sec=300,
            workspace=workspace,
        )

        raw = ai_mgr.wait_for_output(session.session_id)
        tlog.log_event("ai_session_complete", {"status": raw.get("status")})
        tlog.write_file("stdout.txt", raw.get("stdout", ""))
        if raw.get("stderr"):
            tlog.write_file("stderr.txt", raw.get("stderr", ""))

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

        # 10. Checkout main + restore stash
        try:
            subprocess.run(["git", "checkout", "main"],
                cwd=workspace, capture_output=True, timeout=10)
            if stashed:
                subprocess.run(["git", "stash", "pop"],
                    cwd=workspace, capture_output=True, timeout=10)
                tlog.log_event("git_stash_pop")
        except Exception as e:
            tlog.log_event("git_restore_error", {"error": str(e)[:200]})

        return result

    except ImportError as e:
        # v6 modules import failed — log and fail, don't silently fallback
        error_msg = f"v6 module import failed: {e}"
        print(f"[executor-v6] {error_msg}")
        tlog.log_event("v6_import_error", {"error": str(e)})
        result = {**task, "status": "failed", "error": error_msg, "completed_at": utc_iso()}
        save_json(processing, result)
        return result
    except Exception as e:
        error_msg = str(e)[:500]
        if chat_id:
            _gateway_notify(chat_id, f"Dev v6 执行失败: {error_msg[:200]}")
        result = {**task, "status": "failed", "error": error_msg, "completed_at": utc_iso()}
        save_json(processing, result)
        return result


def process_test_task_v6(task: Dict, processing: Path) -> Dict:
    """Run real tests via subprocess, not AI review."""
    task_id = task.get("task_id", "")
    project_id = task.get("project_id", "")
    chat_id = int(task.get("chat_id", 0))
    token = task.get("_gov_token", os.getenv("GOV_COORDINATOR_TOKEN", ""))
    tlog = TaskLogger(task_id)
    tlog.log_event("test_task_start", {"project_id": project_id})
    workspace = str(resolve_workspace())

    try:
        import re as _re
        tlog.log_event("running_unittest")
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "agent/tests", "-p", "test_*.py"],
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
    workspace = str(resolve_workspace())

    try:
        tlog.log_event("running_verify_loop")
        vl = subprocess.run(["bash", "scripts/verify_loop.sh", token, project_id],
            cwd=workspace, capture_output=True, text=True, timeout=60)
        tlog.write_file("verify_loop.txt", vl.stdout + vl.stderr)
        verify_pass = "0 fail" in vl.stdout
        tlog.log_event("verify_loop_complete", {"passed": verify_pass})

        tlog.log_event("running_gatekeeper")
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        gate = requests.get(f"{gov_url}/api/wf/{project_id}/release-gate",
            headers={"X-Gov-Token": token}, timeout=15).json()
        gate_pass = gate.get("release", False) and gate.get("gatekeeper", {}).get("pass", False)
        tlog.log_event("gatekeeper_complete", {"passed": gate_pass})

        all_pass = verify_pass and gate_pass
        result = {**task, "status": "completed" if all_pass else "failed", "completed_at": utc_iso(),
                  "executor": {"action": "qa_task_v6", "verify_pass": verify_pass, "gate_pass": gate_pass}}
        save_json(processing, result)

        if all_pass:
            tlog.log_event("triggering_auto_merge")
            branch = task.get("_branch", "")
            if not branch:
                br = subprocess.run(["git", "branch", "--list", "dev/*", "--sort=-committerdate"],
                    cwd=workspace, capture_output=True, text=True, timeout=5)
                branches = [b.strip().lstrip("* ") for b in br.stdout.strip().split("\n") if b.strip()]
                branch = branches[0] if branches else ""
            if branch:
                mr = subprocess.run(["bash", "scripts/merge-and-deploy.sh", branch],
                    cwd=workspace, capture_output=True, text=True, timeout=180,
                    env={**os.environ, "GOV_COORDINATOR_TOKEN": token})
                tlog.write_file("merge_output.txt", mr.stdout + mr.stderr)
                tlog.log_event("auto_merge", {"passed": mr.returncode == 0, "branch": branch})
                if chat_id:
                    _gateway_notify(chat_id, f"Auto-merge {'OK' if mr.returncode==0 else 'FAIL'}: {branch}")

        if chat_id:
            _gateway_notify(chat_id, f"QA {'PASS' if all_pass else 'FAIL'}")
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
                "changed_files": result.get("executor", {}).get("git_changed_files", []),
                "test_results": {},
                "_before_snapshot": {"commit": task.get("_git_checkpoint", "HEAD~1")},
                "_chain_depth": chain_depth,
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
    # Remove **bold** → bold
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Remove `code` → code
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove _italic_ → italic
    text = re.sub(r'_(.+?)_', r'\1', text)
    return text


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


def _cleanup_orphans() -> int:
    """Check for orphaned agents and kill their zombie processes."""
    import signal
    token = os.getenv("GOV_COORDINATOR_TOKEN", "")
    if not token:
        return 0

    result = _gov_api("GET", "/api/agent/orphans", token=token)
    orphans = result.get("orphans", [])
    cleaned = 0

    for orphan in orphans:
        pid = orphan.get("worker_pid")
        session_id = orphan.get("session_id", "")

        if pid:
            try:
                # Check if process is alive
                os.kill(int(pid), 0)  # Signal 0 = check existence
                # Process alive but orphaned → kill
                print(f"[executor] killing orphan process PID={pid} (session={session_id})")
                os.kill(int(pid), signal.SIGTERM)
                cleaned += 1
            except (ProcessLookupError, OSError):
                # Process already dead
                pass

    # Cleanup orphan records
    if orphans:
        project_id = os.getenv("GOV_PROJECT_ID", "amingClaw")
        _gov_api("POST", "/api/agent/cleanup",
            data={"project_id": project_id},
            token=token)

    return cleaned


def run() -> None:
    """Serial executor loop (original mode). Picks tasks one at a time."""
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[executor] another executor instance is already running; exit")
        return

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

    print("[executor] started (serial mode)")
    try:
        while True:
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
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print("[executor] error:", exc)
                time.sleep(max(1.0, poll_sec))
    except KeyboardInterrupt:
        print("[executor] stopped by keyboard")
    finally:
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
