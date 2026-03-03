import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
from pathlib import Path
from typing import Dict, Optional
import socket

import requests

from utils import (
    answer_callback_query,
    load_json,
    new_task_id,
    save_json,
    send_document,
    send_text,
    task_file,
    tasks_root,
    telegram_token,
    utc_iso,
)
from config import (
    KNOWN_BACKENDS, PIPELINE_PRESETS,
    format_pipeline_stages, get_agent_backend, get_pipeline_stages,
    set_agent_backend, set_pipeline_stages,
)
from auth import debug_verify_otp, get_auth_state, init_authenticator, verify_otp
from workspace import (
    clear_workspace_override,
    resolve_active_workspace,
    set_workspace_override,
)
from task_state import (
    archive_task_result,
    list_task_state_candidates,
    find_archive_entry,
    group_archive_entries,
    grouped_archive_overview,
    list_active_tasks,
    load_task_status,
    mark_task_finished,
    mark_task_completion_notified,
    mark_task_timeout,
    read_task_events,
    register_task_created,
    resolve_task_ref,
    search_archive_entries,
    update_task_runtime,
)
from bot_commands import (
    handle_command,
    handle_callback_query,
    handle_pending_action,
    create_task,
    parse_task_text,
    is_screenshot_text,
    run_screenshot_once,
    run_codex_chat,
    run_chat,
    acceptance_tag,
    acceptance_next_action,
    task_inline_keyboard,
    build_status_summary,
    status_tag,
)

# Re-export bot_commands symbols for backward compatibility
# (imports above already make them available in this module's namespace)


def state_file() -> Path:
    return tasks_root() / "state" / "coordinator_offset.json"


def read_offset() -> int:
    path = state_file()
    if not path.exists():
        return 0
    try:
        return int(load_json(path).get("offset", 0))
    except Exception:
        return 0


def write_offset(offset: int) -> None:
    save_json(state_file(), {"offset": offset, "updated_at": utc_iso()})


def maybe_timeout_stale_tasks() -> None:
    """Mark tasks as timeout when heartbeat_at is stale beyond task_timeout_sec."""
    import datetime

    task_timeout_sec = int(os.getenv("TASK_TIMEOUT_SEC", "1800"))
    snapshots = list_task_state_candidates()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for st in snapshots:
        if not isinstance(st, dict):
            continue
        if st.get("status") not in ("processing", "running"):
            continue
        task_id = str(st.get("task_id") or "")
        chat_id = int(st.get("chat_id") or 0)
        if not task_id:
            continue
        # Use heartbeat_at if available, otherwise fall back to started_at
        ts_str = str(st.get("heartbeat_at") or st.get("started_at") or "").strip()
        if not ts_str:
            continue
        try:
            ts = datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc
            )
            age_sec = (now_utc - ts).total_seconds()
        except Exception:
            continue
        if age_sec <= task_timeout_sec:
            continue
        try:
            mark_task_timeout(task_id, chat_id)
            task_code = str(st.get("task_code") or task_id)
            if chat_id:
                send_text(
                    chat_id,
                    "\u26a0\ufe0f \u4efb\u52a1\u8d85\u65f6\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    "\u4efb\u52a1: [{code}] {task_id}\n"
                    "\u539f\u56e0: >{timeout}s \u65e0\u5fc3\u8df3".format(
                        code=task_code,
                        task_id=task_id,
                        timeout=task_timeout_sec,
                    ),
                    reply_markup=task_inline_keyboard(task_code),
                )
        except Exception as exc:
            print("[coordinator] timeout mark error for {}: {}".format(task_id, exc))


def maybe_push_completion_notifications() -> None:
    snapshots = list_task_state_candidates()
    for st in snapshots:
        if not isinstance(st, dict):
            continue
        task_id = str(st.get("task_id") or "")
        chat_id = int(st.get("chat_id") or 0)
        if not task_id or not chat_id:
            continue
        if not bool(st.get("has_end_marker")):
            continue
        if str(st.get("completion_notified_at") or "").strip():
            continue
        status = str(st.get("status") or "unknown")
        stage = str(st.get("stage") or "unknown")
        summary = str(st.get("summary") or "").strip()
        task_code = str(st.get("task_code") or "-")
        send_text(
            chat_id,
            "\u2705 \u4efb\u52a1\u5b8c\u6210\u901a\u77e5\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\u4efb\u52a1: [{code}] {task_id}\n"
            "\u72b6\u6001: {status}({status_tag})\n"
            "\u9636\u6bb5: {stage}\n"
            "\u6982\u8981: {summary}".format(
                code=task_code,
                task_id=task_id,
                status=status,
                status_tag=status_tag(status),
                stage=stage,
                summary=summary[:300] if summary else "(\u65e0\u6982\u8981)",
            ),
            reply_markup=task_inline_keyboard(task_code),
        )
        mark_task_completion_notified(task_id)


def poll_updates(offset: int) -> Dict:
    token = telegram_token()
    url = "https://api.telegram.org/bot{}/getUpdates".format(token)
    resp = requests.get(
        url,
        params={
            "timeout": 30,
            "offset": offset,
            "allowed_updates": '["message","edited_message","callback_query"]',
        },
        timeout=40,
    )
    return resp.json()


def register_bot_commands() -> None:
    """Register aming-claw commands with Telegram via setMyCommands."""
    from utils import tg_post
    commands = [
        {"command": "menu", "description": "打开主菜单"},
        {"command": "help", "description": "显示命令列表"},
        {"command": "task", "description": "创建新任务"},
        {"command": "status", "description": "查看任务列表或单个任务"},
        {"command": "accept", "description": "验收任务"},
        {"command": "reject", "description": "拒绝任务"},
        {"command": "retry", "description": "重新开发任务"},
        {"command": "info", "description": "查看系统信息"},
        {"command": "switch_backend", "description": "切换后端"},
        {"command": "switch_model", "description": "切换AI模型"},
        {"command": "show_pipeline", "description": "查看流水线配置"},
        {"command": "archive", "description": "归档概览/搜索"},
        {"command": "screenshot", "description": "截图"},
        {"command": "clear_tasks", "description": "清空任务列表"},
        {"command": "auth_init", "description": "初始化2FA"},
        {"command": "workspace_list", "description": "查看工作目录列表"},
    ]
    try:
        tg_post("setMyCommands", {"commands": commands})
        print("[coordinator] bot commands registered ({} commands)".format(len(commands)))
    except Exception as exc:
        print("[coordinator] failed to register bot commands: {}".format(exc))


def run() -> None:
    lock = acquire_single_instance_lock()
    if lock is None:
        print("[coordinator] another coordinator instance is already running; exit")
        return
    register_bot_commands()
    interval_sec = float(os.getenv("COORDINATOR_POLL_INTERVAL_SEC", "1"))
    reconcile_interval_sec = float(os.getenv("COORDINATOR_RECONCILE_SEC", "5"))
    last_reconcile_ts = 0.0
    offset = read_offset()
    print("[coordinator] started, offset={}".format(offset))
    while True:
        try:
            data = poll_updates(offset)
            if not data.get("ok"):
                raise RuntimeError("telegram getUpdates failed: {}".format(data))

            for upd in data.get("result", []):
                update_id = int(upd.get("update_id", 0))
                if update_id >= offset:
                    offset = update_id + 1
                    write_offset(offset)

                cb = upd.get("callback_query")
                if cb:
                    handle_callback_query(cb)
                    continue

                msg = upd.get("message") or upd.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = int((msg.get("chat") or {}).get("id") or 0)
                user_id = int((msg.get("from") or {}).get("id") or 0)
                if not chat_id:
                    continue

                # Check for pending interactive action first (from button clicks)
                if not text.startswith("/") and handle_pending_action(chat_id, user_id, text):
                    continue

                if handle_command(chat_id, user_id, text):
                    continue

                task_text = parse_task_text(text)
                if task_text:
                    task_id = create_task(chat_id, user_id, text)
                    task = load_json(task_file("pending", task_id))
                    task_code = task.get("task_code", "-")
                    send_text(
                        chat_id,
                        "\U0001f4dd \u4efb\u52a1\u5df2\u521b\u5efa\n"
                        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        "\u4ee3\u53f7: [{code}]\n"
                        "\u4efb\u52a1ID: {task_id}\n"
                        "\u72b6\u6001: pending\n"
                        "\u5185\u5bb9: {text}".format(
                            code=task_code,
                            task_id=task_id,
                            text=task_text[:200],
                        ),
                        reply_markup=task_inline_keyboard(task_code),
                    )
                    continue

                if text.startswith("/"):
                    send_text(chat_id, "未知命令。发送 /help 查看可用命令。")
                    continue

                if is_screenshot_text(text):
                    try:
                        run_screenshot_once(chat_id, text)
                    except Exception as exc:
                        send_text(chat_id, "截图失败: {}".format(str(exc)[:1000]))
                    continue

                try:
                    reply = run_chat(text)
                    send_text(chat_id, reply)
                except Exception as exc:
                    send_text(chat_id, "对话失败: {}".format(str(exc)[:1000]))

            now_ts = time.time()
            if now_ts - last_reconcile_ts >= reconcile_interval_sec:
                maybe_timeout_stale_tasks()
                maybe_push_completion_notifications()
                last_reconcile_ts = now_ts
        except KeyboardInterrupt:
            print("[coordinator] stopped by keyboard")
            return
        except Exception as exc:
            print("[coordinator] error:", exc)
            time.sleep(max(1.0, interval_sec))
            continue

        time.sleep(interval_sec)


def acquire_single_instance_lock() -> Optional[socket.socket]:
    port = int(os.getenv("COORDINATOR_SINGLETON_PORT", "39102"))
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
    run()
