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
    download_telegram_file,
    extract_photos_from_message,
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
    format_pipeline_stages, get_agent_backend, get_config_language,
    get_pipeline_stages, set_agent_backend, set_pipeline_stages,
)
from auth import debug_verify_otp, get_auth_state, init_authenticator, verify_otp
from i18n import t
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
    run_claude_chat_with_image,
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
                    t("msg.task_timeout",
                      code=task_code,
                      task_id=task_id,
                      timeout=task_timeout_sec),
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
            t("msg.task_completed",
              code=task_code,
              task_id=task_id,
              status=status,
              status_tag=status_tag(status),
              stage=stage,
              summary=summary[:300] if summary else t("msg.no_summary")),
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
        {"command": "menu", "description": t("bot_cmd.menu")},
        {"command": "help", "description": t("bot_cmd.help")},
        {"command": "task", "description": t("bot_cmd.task")},
        {"command": "status", "description": t("bot_cmd.status")},
        {"command": "accept", "description": t("bot_cmd.accept")},
        {"command": "reject", "description": t("bot_cmd.reject")},
        {"command": "retry", "description": t("bot_cmd.retry")},
        {"command": "info", "description": t("bot_cmd.info")},
        {"command": "switch_backend", "description": t("bot_cmd.switch_backend")},
        {"command": "switch_model", "description": t("bot_cmd.switch_model")},
        {"command": "show_pipeline", "description": t("bot_cmd.show_pipeline")},
        {"command": "archive", "description": t("bot_cmd.archive")},
        {"command": "screenshot", "description": t("bot_cmd.screenshot")},
        {"command": "clear_tasks", "description": t("bot_cmd.clear_tasks")},
        {"command": "auth_init", "description": t("bot_cmd.auth_init")},
        {"command": "workspace_list", "description": t("bot_cmd.workspace_list")},
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
    # Load persisted language setting
    from i18n import set_language
    persisted_lang = get_config_language()
    set_language(persisted_lang)
    print("[coordinator] language={}".format(persisted_lang))
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
                caption = (msg.get("caption") or "").strip()
                chat_id = int((msg.get("chat") or {}).get("id") or 0)
                user_id = int((msg.get("from") or {}).get("id") or 0)
                if not chat_id:
                    continue

                # Extract photo info from message
                photos = extract_photos_from_message(msg)
                has_photo = len(photos) > 0

                # For photo messages, use caption as text if no text field
                if has_photo and not text:
                    text = caption

                # Unsupported media types (video, audio, document without photo)
                if not has_photo and not text and (msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document")):
                    send_text(chat_id, "当前仅支持文本和图片消息")
                    continue

                # Check for pending interactive action first (from button clicks)
                if not text.startswith("/") and handle_pending_action(chat_id, user_id, text, photos=photos):
                    continue

                if not has_photo and handle_command(chat_id, user_id, text):
                    continue

                task_text = parse_task_text(text)
                if task_text:
                    task_id = create_task(chat_id, user_id, text, photos=photos)
                    task = load_json(task_file("pending", task_id))
                    task_code = task.get("task_code", "-")
                    img_hint = "（含 {} 张附件）".format(len(photos)) if photos else ""
                    send_text(
                        chat_id,
                        t("msg.task_created",
                          code=task_code,
                          task_id=task_id,
                          text=task_text[:200]) + img_hint,
                        reply_markup=task_inline_keyboard(task_code),
                    )
                    continue

                if text.startswith("/"):
                    send_text(chat_id, t("msg.unknown_command"))
                    continue

                if not has_photo and is_screenshot_text(text):
                    try:
                        run_screenshot_once(chat_id, text)
                    except Exception as exc:
                        send_text(chat_id, t("msg.screenshot_failed", err=str(exc)[:1000]))
                    continue

                # Chat mode (text or image)
                if has_photo:
                    try:
                        chat_text = text or "请描述这张图片的内容"
                        # Download image to temp dir for chat
                        import tempfile
                        with tempfile.TemporaryDirectory(prefix="aming_chat_img_") as tmpdir:
                            image_paths = []
                            for p in photos:
                                try:
                                    local = download_telegram_file(p["file_id"], Path(tmpdir))
                                    image_paths.append(str(local))
                                except Exception as dl_err:
                                    send_text(chat_id, "图片下载失败: {}".format(str(dl_err)[:500]))
                                    break
                            else:
                                from bot_commands import run_claude_chat_with_image
                                reply = run_claude_chat_with_image(chat_text, image_paths)
                                send_text(chat_id, reply)
                    except Exception as exc:
                        send_text(chat_id, t("msg.chat_failed", err=str(exc)[:1000]))
                else:
                    try:
                        reply = run_chat(text)
                        send_text(chat_id, reply)
                    except Exception as exc:
                        send_text(chat_id, t("msg.chat_failed", err=str(exc)[:1000]))

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
