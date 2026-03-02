import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
    KNOWN_BACKENDS, KNOWN_CLAUDE_MODELS, PIPELINE_PRESETS,
    format_pipeline_stages, get_agent_backend, get_claude_model, get_model_provider,
    get_pipeline_stages, set_agent_backend, set_claude_model, set_pipeline_stages,
)
from model_registry import get_available_models, make_label
from auth import debug_verify_otp, get_auth_state, init_authenticator, verify_otp
from workspace import (
    clear_workspace_override,
    resolve_active_workspace,
    set_workspace_override,
)
from task_state import (
    archive_task_result,
    clear_active_tasks,
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
from git_rollback import (
    commit_after_acceptance,
    rollback_to_checkpoint,
)
from interactive_menu import (
    main_menu_keyboard,
    system_menu_keyboard,
    archive_menu_keyboard,
    ops_menu_keyboard,
    security_menu_keyboard,
    backend_select_keyboard,
    pipeline_preset_keyboard,
    cancel_keyboard,
    back_to_menu_keyboard,
    confirm_cancel_keyboard,
    task_list_action_keyboard,
    set_pending_action,
    get_pending_action,
    peek_pending_action,
    clear_pending_action,
    WELCOME_TEXT,
    HELP_TEXT,
    SUBMENU_TEXTS,
    PENDING_PROMPTS,
)


def workspace_pick_state_file() -> Path:
    return tasks_root() / "state" / "workspace_pick_state.json"


def load_workspace_pick_state() -> Dict:
    path = workspace_pick_state_file()
    if not path.exists():
        return {}
    try:
        data = load_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_workspace_pick_state(data: Dict) -> None:
    save_json(workspace_pick_state_file(), data)


def _pick_state_key(chat_id: int, user_id: int) -> str:
    return "{}:{}".format(chat_id, user_id)


def store_workspace_candidates(chat_id: int, user_id: int, query: str, items: List[Path]) -> None:
    state = load_workspace_pick_state()
    state[_pick_state_key(chat_id, user_id)] = {
        "query": query,
        "updated_at": utc_iso(),
        "items": [str(p) for p in items],
    }
    save_workspace_pick_state(state)


def read_workspace_candidates(chat_id: int, user_id: int) -> List[Path]:
    state = load_workspace_pick_state()
    entry = state.get(_pick_state_key(chat_id, user_id)) or {}
    out: List[Path] = []
    for item in (entry.get("items") or []):
        try:
            p = Path(str(item))
            if p.exists() and p.is_dir() and (p / ".git").exists():
                out.append(p.resolve())
        except Exception:
            continue
    return out


def clear_workspace_candidates(chat_id: int, user_id: int) -> None:
    state = load_workspace_pick_state()
    key = _pick_state_key(chat_id, user_id)
    if key in state:
        del state[key]
        save_workspace_pick_state(state)


def resolve_workspace_search_roots() -> List[Path]:
    raw = os.getenv("WORKSPACE_SEARCH_ROOTS", "").strip()
    roots: List[Path] = []
    if raw:
        for part in raw.split(os.pathsep):
            v = part.strip().strip('"').strip("'")
            if not v:
                continue
            p = Path(v).expanduser()
            if p.exists() and p.is_dir():
                roots.append(p.resolve())
    if not roots:
        active = resolve_active_workspace().resolve()
        for p in [active, active.parent]:
            if p.exists() and p.is_dir():
                roots.append(p)
    dedup: List[Path] = []
    seen: Set[str] = set()
    for p in roots:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(p)
    return dedup


def _normalize_query(query: str) -> List[str]:
    text = (query or "").strip().lower()
    text = text.replace("\\", " ").replace("/", " ")
    return [x for x in re.split(r"\s+", text) if x]


def find_git_workspace_candidates(query: str) -> List[Path]:
    roots = resolve_workspace_search_roots()
    if not roots:
        return []
    query_parts = _normalize_query(query)
    scan_limit = int(os.getenv("WORKSPACE_SCAN_LIMIT", "2000"))
    result_limit = int(os.getenv("WORKSPACE_MATCH_LIMIT", "20"))
    found: List[Path] = []
    seen: Set[str] = set()
    scanned = 0
    for root in roots:
        try:
            for git_marker in root.rglob(".git"):
                if scanned >= scan_limit:
                    break
                scanned += 1
                repo = git_marker.parent.resolve()
                key = str(repo).lower()
                if key in seen:
                    continue
                searchable = "{} {}".format(repo.name.lower(), str(repo).lower().replace("\\", "/"))
                if query_parts and not all(part in searchable for part in query_parts):
                    continue
                found.append(repo)
                seen.add(key)
                if len(found) >= result_limit:
                    break
        except Exception:
            continue
        if scanned >= scan_limit or len(found) >= result_limit:
            break
    found.sort(key=lambda p: str(p).lower())
    return found


def parse_allow_pairs() -> Set[Tuple[int, int]]:
    raw = os.getenv("OPS_ALLOWED_CHAT_USER_PAIRS", "").strip()
    out: Set[Tuple[int, int]] = set()
    if not raw:
        return out
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        left, right = item.split(":", 1)
        try:
            out.add((int(left.strip()), int(right.strip())))
        except Exception:
            continue
    return out


def is_ops_allowed(chat_id: int, user_id: int) -> bool:
    pairs = parse_allow_pairs()
    if not pairs:
        return False
    return (chat_id, user_id) in pairs


def is_screenshot_text(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    keys = [
        "/screenshot",
        "截图",
        "截屏",
        "screen",
        "screenshot",
        "屏幕",
        "多屏",
        "双屏",
        "all screens",
    ]
    return any(k in low for k in keys)


def parse_task_text(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("/task "):
        return t[6:].strip()
    if t.startswith("/task"):
        return None
    return None


def infer_action(text: str) -> str:
    if is_screenshot_text(text):
        return "screenshot"
    return get_agent_backend()


def run_codex_chat(text: str) -> str:
    workspace = str(resolve_active_workspace())
    if not Path(workspace).exists():
        raise RuntimeError("CODEX_WORKSPACE does not exist: {}".format(workspace))
    timeout_sec = int(os.getenv("CHAT_TIMEOUT_SEC", "300"))
    max_retries = int(os.getenv("CHAT_TIMEOUT_RETRIES", "1"))
    model = os.getenv("CODEX_MODEL", "").strip()
    codex_bin = os.getenv("CODEX_BIN", "").strip()
    if not codex_bin:
        codex_bin = "codex.cmd" if os.name == "nt" else "codex"
    dangerous = os.getenv("CODEX_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}

    output_last = tasks_root() / "logs" / ("chat-" + str(int(time.time() * 1000)) + ".txt")
    prompt = text

    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "-C",
        workspace,
        "-o",
        str(output_last),
        prompt,
    ]
    if dangerous:
        cmd.insert(2, "--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.insert(2, "workspace-write")
        cmd.insert(2, "--sandbox")
    if model:
        cmd.insert(2, model)
        cmd.insert(2, "--model")
    proc = None
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            break
        except subprocess.TimeoutExpired:
            if attempt >= max_retries:
                raise
            time.sleep(min(5, 1 + attempt))
    if proc is None:
        raise RuntimeError("chat exec timeout")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err[:1200] or "codex chat failed")
    out = ""
    if output_last.exists():
        try:
            out = output_last.read_text(encoding="utf-8").strip()
        except Exception:
            out = ""
    if not out:
        out = (proc.stdout or "").strip()
    if not out:
        return "(empty response)"
    return out[-3500:]


def _claude_chat_via_api(text: str, provider: str, model: str) -> str:
    """Direct API chat for Anthropic or OpenAI (no CLI needed)."""
    import requests as _req
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        resp = _req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": text}],
                  "max_tokens": 4096},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()[-3500:]
    else:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": model or "claude-sonnet-4-6",
                  "max_tokens": 8192,
                  "messages": [{"role": "user", "content": text}]},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()[-3500:]


def run_claude_chat(text: str) -> str:
    """One-shot chat: routes to API (if provider stored) or Claude Code CLI."""
    model = get_claude_model()
    provider = get_model_provider()

    if provider in ("anthropic", "openai"):
        return _claude_chat_via_api(text, provider, model)

    # Fallback: Claude Code CLI
    import shutil
    claude_bin = os.getenv("CLAUDE_BIN", "").strip()
    if not claude_bin:
        claude_bin = (
            shutil.which("claude.cmd") or shutil.which("claude")
            or ("claude.cmd" if os.name == "nt" else "claude")
        )
    timeout_sec = int(os.getenv("CHAT_TIMEOUT_SEC", "300"))
    cmd = [claude_bin, "-p", text, "--output-format", "text",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")}
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True,
                              timeout=timeout_sec, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude chat timeout")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err[:1200] or "claude chat failed")
    return (proc.stdout or "").strip()[-3500:] or "(empty response)"


def run_chat(text: str) -> str:
    """Route direct chat to the active backend (codex / claude)."""
    backend = get_agent_backend()
    if backend == "claude":
        return run_claude_chat(text)
    # codex / pipeline default → codex chat
    return run_codex_chat(text)


def run_screenshot_once(chat_id: int, text: str) -> None:
    base_url = os.getenv("EXECUTOR_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    token = os.getenv("EXECUTOR_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing EXECUTOR_API_TOKEN")
    payload = {
        "task_id": "chat-screenshot-" + str(int(time.time() * 1000)),
        "action": "take_screenshot",
        "command_text": text or "请截图",
    }
    resp = requests.post(
        base_url + "/execute",
        json=payload,
        headers={"X-Executor-Token": token},
        timeout=int(os.getenv("SCREENSHOT_TIMEOUT_SEC", "90")),
    )
    data = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError("executor-gateway http {}: {}".format(resp.status_code, data))
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "screenshot failed")
    files = ((data.get("details") or {}).get("files") or [])
    sent = 0
    for f in files:
        p = Path(f)
        if p.exists():
            send_document(chat_id, p, caption="screenshot: {}".format(p.name))
            sent += 1
    timings = ((data.get("details") or {}).get("timings_ms") or {})
    if ("耗时" in text) or ("timing" in text.lower()):
        send_text(
            chat_id,
            "截图耗时: total={}ms capture={}ms copy={}ms".format(
                timings.get("total_ms", 0),
                timings.get("capture_ms", 0),
                timings.get("copy_ms", 0),
            ),
        )
    send_text(chat_id, "截图完成，已回传 {} 张图片。".format(sent))


def find_task(task_ref: str) -> Optional[Dict]:
    task_id = resolve_task_ref(task_ref) or task_ref
    for stage in ("pending", "processing", "results"):
        p = task_file(stage, task_id)
        if p.exists():
            obj = load_json(p)
            obj["_stage"] = stage
            obj["_task_ref"] = task_ref
            return obj
    return None


def task_status_snapshot(task_id: str) -> Optional[Dict]:
    try:
        return load_task_status(task_id)
    except Exception:
        return None


def merge_task_with_status(task: Dict) -> Dict:
    out = dict(task)
    st = task_status_snapshot(str(task.get("task_id") or ""))
    if not st:
        return out
    out["_status_snapshot"] = st
    out["status"] = st.get("status", out.get("status"))
    out["updated_at"] = st.get("updated_at", out.get("updated_at"))
    out["_stage"] = st.get("stage", out.get("_stage", out.get("stage", "unknown")))
    return out


def status_elapsed_ms(task: Dict) -> int:
    executor = task.get("executor") or {}
    if executor.get("elapsed_ms") is not None:
        return int(executor.get("elapsed_ms") or 0)
    st = task.get("_status_snapshot") if isinstance(task.get("_status_snapshot"), dict) else {}
    started = str(st.get("started_at") or "")
    ended = str(st.get("ended_at") or "")
    if not started or not ended:
        return 0
    try:
        start_ts = int(time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")) * 1000)
        end_ts = int(time.mktime(time.strptime(ended, "%Y-%m-%dT%H:%M:%SZ")) * 1000)
        if end_ts >= start_ts:
            return end_ts - start_ts
    except Exception:
        return 0
    return 0


def build_status_summary(task: Dict) -> str:
    status_snapshot = task.get("_status_snapshot") if isinstance(task.get("_status_snapshot"), dict) else {}
    status_summary = str(status_snapshot.get("summary") or "").strip()
    if status_summary:
        return status_summary[:300]
    executor = task.get("executor") or {}
    summary = (executor.get("last_message") or "").strip()
    if summary:
        return summary[:300]
    noop_reason = (executor.get("noop_reason") or "").strip()
    if noop_reason:
        return ("失败原因: " + noop_reason)[:300]
    err = (task.get("error") or "").strip()
    if err:
        return ("错误: " + err)[:300]
    return "(暂无概要)"


def status_tag(status: str) -> str:
    mapping = {
        "pending": "待处理",
        "processing": "执行中",
        "pending_acceptance": "待验收",
        "accepted": "验收通过",
        "rejected": "验收拒绝",
        "completed": "已完成",
        "failed": "执行失败",
    }
    return mapping.get(str(status or "").strip().lower(), str(status or "unknown"))


def acceptance_tag(task: Dict) -> str:
    stage = str(task.get("_stage") or task.get("stage") or "").strip().lower()
    status = str(task.get("status") or "").strip().lower()
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    state = str(acceptance.get("state") or "").strip().lower()
    if status == "accepted" or state == "accepted":
        return "验收通过"
    if status == "rejected" or state == "rejected":
        return "验收拒绝"
    if status == "pending_acceptance" or state == "pending":
        return "待验收"
    if stage in {"pending", "processing"}:
        return "未到验收阶段"
    if stage == "results" and status in {"completed", "failed"}:
        # Backward compatibility for historical result files before explicit pending_acceptance migration.
        return "待验收(兼容旧任务)"
    return "未知"


def acceptance_next_action(task: Dict) -> str:
    code = str(task.get("task_code") or task.get("task_id") or "-")
    tag = acceptance_tag(task)
    if tag in {"待验收", "待验收(兼容旧任务)", "验收拒绝"}:
        return "通过 /accept {code} 验收通过归档；或 /reject {code} <原因> 保持不归档".format(code=code)
    if tag == "验收通过":
        return "已验收通过，可用 /archive_show {code} 查看归档详情".format(code=code)
    return "当前无需验收操作"


def task_stage_file(task: Dict) -> Path:
    return task_file(str(task.get("_stage") or "results"), str(task.get("task_id") or ""))


def parse_reject_command(text: str) -> Tuple[Optional[str], str]:
    m = re.match(r"^/reject\s+(\S+)(?:\s+(.+))?\s*$", (text or "").strip(), re.IGNORECASE)
    if not m:
        return None, ""
    return m.group(1).strip(), (m.group(2) or "").strip()


def build_archive_list_text(items: List[Dict], title: str) -> str:
    lines = [title]
    for item in items:
        lines.append(
            "[{code}] {status} {action} {archive_id}\n任务ID={task_id}\n概要: {summary}".format(
                code=item.get("task_code", "-"),
                status=item.get("status", "unknown"),
                action=item.get("action", "unknown"),
                archive_id=item.get("archive_id", ""),
                task_id=item.get("task_id", ""),
                summary=str(item.get("summary", "")).strip()[:120],
            )
        )
    return "\n\n".join(lines[:50])


def build_archive_grouped_text(items: List[Dict], title: str, limit_per_group: int = 5) -> str:
    grouped = group_archive_entries(items, limit_per_group=limit_per_group)
    if not grouped:
        return title + "\n(无结果)"
    lines = [title]
    for action, info in grouped.items():
        lines.append("类型 {}: {} 条".format(action, info.get("count", 0)))
        for item in (info.get("items") or []):
            lines.append(
                "  [{code}] {status} {archive_id}\n  任务ID={task_id}\n  概要: {summary}".format(
                    code=item.get("task_code", "-"),
                    status=item.get("status", "unknown"),
                    archive_id=item.get("archive_id", ""),
                    task_id=item.get("task_id", ""),
                    summary=str(item.get("summary", "")).strip()[:80],
                )
            )
    return "\n".join(lines[:200])


def task_inline_keyboard(task_ref: str) -> Dict:
    ref = str(task_ref or "").strip()
    return {
        "inline_keyboard": [
            [
                {"text": "查看状态", "callback_data": "status:{}".format(ref)},
                {"text": "验收通过", "callback_data": "accept:{}".format(ref)},
                {"text": "验收拒绝", "callback_data": "reject:{}".format(ref)},
            ],
            [
                {"text": "查看事件", "callback_data": "events:{}".format(ref)},
            ],
        ]
    }


def build_events_text(task_id: str, task_code: str = "", limit: int = 12) -> str:
    rows = read_task_events(task_id, limit=limit)
    if not rows:
        return "任务 [{}] {} 暂无事件记录。".format(task_code or "-", task_id)
    lines = ["任务 [{}] {} 最近事件:".format(task_code or "-", task_id)]
    for row in rows:
        evt = str(row.get("event") or "unknown")
        ts = str(row.get("ts") or "")
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        status = str(data.get("status") or "")
        stage = str(data.get("stage") or "")
        summary = ""
        if status or stage:
            summary = " status={} stage={}".format(status or "-", stage or "-")
        lines.append("- {} {}{}".format(ts, evt, summary))
    return "\n".join(lines[:60])


def handle_callback_query(cb: Dict) -> None:
    cb_id = str(cb.get("id") or "")
    data = str(cb.get("data") or "").strip()
    msg = cb.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    user_id = int((cb.get("from") or {}).get("id") or 0)
    if not cb_id:
        return
    if not data or not chat_id:
        answer_callback_query(cb_id, "无效按钮")
        return
    try:
        # ---- Main menu callbacks ----
        if data.startswith("menu:"):
            _handle_menu_callback(cb_id, data, chat_id, user_id)
            return

        # ---- Backend selection callbacks ----
        if data.startswith("backend_sel:"):
            _handle_backend_select_callback(cb_id, data, chat_id, user_id)
            return

        # ---- Task-specific callbacks ----
        if data.startswith("status:"):
            ref = data.split(":", 1)[1].strip()
            handle_command(chat_id, user_id, "/status {}".format(ref))
            answer_callback_query(cb_id, "已查询状态")
            return
        if data.startswith("events:"):
            ref = data.split(":", 1)[1].strip()
            handle_command(chat_id, user_id, "/events {}".format(ref))
            answer_callback_query(cb_id, "已查询事件")
            return
        if data.startswith("accept:"):
            ref = data.split(":", 1)[1].strip()
            if _requires_acceptance_2fa():
                set_pending_action(chat_id, user_id, "accept_otp", {"task_ref": ref})
                send_text(
                    chat_id,
                    "验收任务 [{}] 需要2FA认证。\n请输入6位OTP验证码：".format(ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, "请输入OTP")
            else:
                handle_command(chat_id, user_id, "/accept {}".format(ref))
                answer_callback_query(cb_id, "验收已提交")
            return
        if data.startswith("reject:"):
            ref = data.split(":", 1)[1].strip()
            if _requires_acceptance_2fa():
                set_pending_action(chat_id, user_id, "reject_otp", {"task_ref": ref})
                send_text(
                    chat_id,
                    "拒绝任务 [{}] 需要2FA认证。\n请输入: <OTP> [拒绝原因]".format(ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, "请输入OTP和原因")
            else:
                set_pending_action(chat_id, user_id, "reject_reason", {"task_ref": ref})
                send_text(
                    chat_id,
                    "请输入拒绝任务 [{}] 的原因：".format(ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, "请输入拒绝原因")
            return
        if data.startswith("model_select:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, "无权限", show_alert=True)
                return
            rest = data[len("model_select:"):]
            if ":" in rest:
                provider, model = rest.split(":", 1)
            else:
                provider, model = "", rest
            set_claude_model(model, provider=provider, changed_by=user_id)
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            answer_callback_query(cb_id, "已切换: {} {}".format(tag, model))
            send_text(
                chat_id,
                "模型已切换为: {} `{}`".format(tag, model),
                reply_markup=back_to_menu_keyboard(),
            )
            return

        # ---- Pipeline preset selection callbacks ----
        if data.startswith("pipeline_preset:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, "无权限", show_alert=True)
                return
            preset_name = data[len("pipeline_preset:"):]
            if preset_name in PIPELINE_PRESETS:
                stages = PIPELINE_PRESETS[preset_name]
                set_pipeline_stages(stages, changed_by=user_id)
                answer_callback_query(cb_id, "流水线已配置")
                send_text(
                    chat_id,
                    "\u2699\ufe0f \u6d41\u6c34\u7ebf\u5df2\u914d\u7f6e\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    "\u9884\u8bbe: {}\n"
                    "\u9636\u6bb5: {}\n\n"
                    "\u65b0\u5efa\u4efb\u52a1\u5c06\u6309\u6b64\u6d41\u6c34\u7ebf\u6267\u884c".format(
                        preset_name, format_pipeline_stages(stages)
                    ),
                    reply_markup=back_to_menu_keyboard(),
                )
            else:
                answer_callback_query(cb_id, "未知预设", show_alert=True)
            return

        # ---- Confirm callbacks (destructive actions) ----
        if data.startswith("confirm:"):
            _handle_confirm_callback(cb_id, data, chat_id, user_id)
            return

        answer_callback_query(cb_id, "未知按钮")
    except Exception as exc:
        answer_callback_query(cb_id, "操作失败", show_alert=True)
        send_text(chat_id, "按钮操作失败: {}".format(str(exc)[:500]))


def _handle_menu_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle all menu:* callback queries."""
    action = data.split(":", 1)[1].strip()

    # -- Return to main menu --
    if action == "main":
        clear_pending_action(chat_id, user_id)
        active_workspace = resolve_active_workspace()
        auth_ready = "已启用" if get_auth_state() else "未初始化"
        backend = get_agent_backend()
        model = get_claude_model() or "(未设置)"
        send_text(
            chat_id,
            WELCOME_TEXT.format(
                workspace=str(active_workspace),
                backend=backend,
                model=model,
                auth=auth_ready,
            ),
            reply_markup=main_menu_keyboard(),
        )
        answer_callback_query(cb_id, "主菜单")
        return

    # -- Cancel pending action --
    if action == "cancel":
        clear_pending_action(chat_id, user_id)
        send_text(
            chat_id,
            "\u5df2\u53d6\u6d88\u64cd\u4f5c\u3002",
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, "已取消")
        return

    # -- Sub-menu: System Settings --
    if action == "sub_system":
        backend = get_agent_backend()
        model = get_claude_model() or "(\u672a\u8bbe\u7f6e)"
        provider = get_model_provider() or "(\u672a\u8bbe\u7f6e)"
        send_text(
            chat_id,
            SUBMENU_TEXTS["system"].format(
                backend=backend,
                model=model,
                provider=provider,
            ),
            reply_markup=system_menu_keyboard(),
        )
        answer_callback_query(cb_id, "系统设置")
        return

    # -- Sub-menu: Archive Management --
    if action == "sub_archive":
        send_text(
            chat_id,
            SUBMENU_TEXTS["archive"],
            reply_markup=archive_menu_keyboard(),
        )
        answer_callback_query(cb_id, "归档管理")
        return

    # -- Sub-menu: Operations --
    if action == "sub_ops":
        send_text(
            chat_id,
            SUBMENU_TEXTS["ops"],
            reply_markup=ops_menu_keyboard(),
        )
        answer_callback_query(cb_id, "运维操作")
        return

    # -- Sub-menu: Security --
    if action == "sub_security":
        send_text(
            chat_id,
            SUBMENU_TEXTS["security"],
            reply_markup=security_menu_keyboard(),
        )
        answer_callback_query(cb_id, "安全认证")
        return

    # -- New Task: prompt for description --
    if action == "new_task":
        set_pending_action(chat_id, user_id, "new_task")
        send_text(
            chat_id,
            PENDING_PROMPTS["new_task"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入任务内容")
        return

    # -- Task List: execute directly --
    if action == "task_list":
        handle_command(chat_id, user_id, "/status")
        answer_callback_query(cb_id, "查询任务列表")
        return

    # -- Clear Task List: confirm before clearing (keeps running tasks) --
    if action == "clear_tasks":
        active = list_active_tasks(chat_id=chat_id)
        if not active:
            send_text(chat_id, "当前没有活动任务，无需清空。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无活动任务")
            return
        running = [t for t in active if str(t.get("status") or "").strip().lower() == "processing"]
        clearable = len(active) - len(running)
        if clearable <= 0:
            send_text(chat_id, "当前所有任务均在运行中，无法清空。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "全部运行中")
            return
        msg = "确认清空任务列表？\n将移除 {} 个已归档/待验收任务。".format(clearable)
        if running:
            msg += "\n（{} 个运行中的任务将保留）".format(len(running))
        send_text(
            chat_id,
            msg,
            reply_markup=confirm_cancel_keyboard("clear_tasks"),
        )
        answer_callback_query(cb_id, "请确认清空")
        return

    # -- Screenshot: prompt for description --
    if action == "screenshot":
        set_pending_action(chat_id, user_id, "screenshot")
        send_text(
            chat_id,
            PENDING_PROMPTS["screenshot"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入截图说明")
        return

    # -- System Info: execute directly --
    if action == "info":
        handle_command(chat_id, user_id, "/info")
        send_text(chat_id, "", reply_markup=back_to_menu_keyboard()) if False else None
        answer_callback_query(cb_id, "系统信息")
        return

    # -- Switch Backend: show selection keyboard --
    if action == "switch_backend":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "无权限执行此操作。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        current = get_agent_backend()
        send_text(
            chat_id,
            "当前后端: {}\n请选择新的执行后端：".format(current),
            reply_markup=backend_select_keyboard(),
        )
        answer_callback_query(cb_id, "选择后端")
        return

    # -- Switch Model: show model list --
    if action == "switch_model":
        handle_command(chat_id, user_id, "/switch_model")
        answer_callback_query(cb_id, "选择模型")
        return

    # -- Pipeline Config: show preset selection --
    if action == "pipeline_config":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        preset_info = "\n".join("  {} \u2192 {}".format(k, format_pipeline_stages(v)) for k, v in PIPELINE_PRESETS.items())
        send_text(
            chat_id,
            "\u2699\ufe0f \u6d41\u6c34\u7ebf\u914d\u7f6e\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\u70b9\u51fb\u9884\u8bbe\u76f4\u63a5\u5e94\u7528\uff0c\u6216\u9009\u62e9\u81ea\u5b9a\u4e49:\n\n"
            "{}".format(preset_info),
            reply_markup=pipeline_preset_keyboard(),
        )
        answer_callback_query(cb_id, "选择流水线配置")
        return

    # -- Pipeline Config Custom: prompt for text --
    if action == "pipeline_config_custom":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "pipeline_config_custom")
        send_text(
            chat_id,
            PENDING_PROMPTS["pipeline_config_custom"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入配置")
        return

    # -- Pipeline Status: execute directly --
    if action == "pipeline_status":
        handle_command(chat_id, user_id, "/show_pipeline")
        answer_callback_query(cb_id, "流水线状态")
        return

    # -- Archive Overview: execute directly --
    if action == "archive":
        handle_command(chat_id, user_id, "/archive")
        answer_callback_query(cb_id, "归档概览")
        return

    # -- Archive Search: prompt for keyword --
    if action == "archive_search":
        set_pending_action(chat_id, user_id, "archive_search")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_search"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入关键词")
        return

    # -- Archive Show: prompt for ID --
    if action == "archive_show":
        set_pending_action(chat_id, user_id, "archive_show")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_show"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入ID")
        return

    # -- Archive Log: prompt for keyword --
    if action == "archive_log":
        set_pending_action(chat_id, user_id, "archive_log")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_log"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入关键词")
        return

    # -- Mgr Restart: prompt for OTP --
    if action == "mgr_restart":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "无权限执行此操作。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "mgr_restart")
        send_text(
            chat_id,
            PENDING_PROMPTS["mgr_restart"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入OTP")
        return

    # -- Mgr Reinit (Self-Update): prompt for OTP --
    if action == "mgr_reinit":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "无权限执行此操作。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "mgr_reinit")
        send_text(
            chat_id,
            PENDING_PROMPTS["mgr_reinit"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入OTP")
        return

    # -- Ops Restart: prompt for OTP --
    if action == "ops_restart":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "ops_restart")
        send_text(
            chat_id,
            PENDING_PROMPTS["ops_restart"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入OTP")
        return

    # -- Mgr Status: execute directly --
    if action == "mgr_status":
        handle_command(chat_id, user_id, "/mgr_status")
        answer_callback_query(cb_id, "管理服务状态")
        return

    # -- Auth Init: execute directly --
    if action == "auth_init":
        handle_command(chat_id, user_id, "/auth_init")
        answer_callback_query(cb_id, "2FA初始化")
        return

    # -- Auth Status: execute directly --
    if action == "auth_status":
        handle_command(chat_id, user_id, "/auth_status")
        answer_callback_query(cb_id, "2FA状态")
        return

    # -- Whoami: execute directly --
    if action == "whoami":
        handle_command(chat_id, user_id, "/ops_whoami")
        answer_callback_query(cb_id, "身份信息")
        return

    # -- Auth Debug: prompt for OTP --
    if action == "auth_debug":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "auth_debug")
        send_text(
            chat_id,
            PENDING_PROMPTS["auth_debug"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入OTP")
        return

    # -- Set Workspace: prompt for path + OTP --
    if action == "set_workspace":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "无权限执行此操作。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "set_workspace")
        send_text(
            chat_id,
            PENDING_PROMPTS["set_workspace"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入工作区+OTP")
        return

    # -- Reset Workspace: prompt for OTP --
    if action == "reset_workspace":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "无权限执行此操作。", reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, "无权限", show_alert=True)
            return
        set_pending_action(chat_id, user_id, "reset_workspace")
        send_text(
            chat_id,
            PENDING_PROMPTS["reset_workspace"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入OTP")
        return

    # -- Workspace List: execute directly --
    if action == "workspace_list":
        handle_command(chat_id, user_id, "/workspace_list")
        answer_callback_query(cb_id, "工作目录列表")
        return

    # -- Workspace Add: prompt for path --
    if action == "workspace_add":
        set_pending_action(chat_id, user_id, "workspace_add")
        send_text(
            chat_id,
            PENDING_PROMPTS["workspace_add"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, "请输入路径")
        return

    # -- Dispatch Status: execute directly --
    if action == "dispatch_status":
        handle_command(chat_id, user_id, "/dispatch_status")
        answer_callback_query(cb_id, "调度器状态")
        return

    answer_callback_query(cb_id, "未知菜单操作")


def _handle_confirm_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle confirm:* callback queries for destructive actions."""
    # data format: "confirm:<action>" or "confirm:<action>:<context>"
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "clear_tasks":
        removed = clear_active_tasks(chat_id)
        send_text(
            chat_id,
            "已清空任务列表，共移除 {} 个已归档/待验收任务（运行中的任务已保留）。".format(removed),
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, "已清空 {} 个任务".format(removed))
        return

    answer_callback_query(cb_id, "未知确认操作")


def _handle_backend_select_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle backend_sel:* callback queries."""
    backend = data.split(":", 1)[1].strip()
    if not is_ops_allowed(chat_id, user_id):
        answer_callback_query(cb_id, "无权限", show_alert=True)
        return
    handle_command(chat_id, user_id, "/switch_backend {}".format(backend))
    answer_callback_query(cb_id, "已切换: {}".format(backend))


def handle_pending_action(chat_id: int, user_id: int, text: str) -> bool:
    """Check if there is a pending action and handle the user's text input.

    Returns True if a pending action was handled, False otherwise.
    """
    pending = get_pending_action(chat_id, user_id)
    if not pending:
        return False

    action = pending.get("action", "")
    context = pending.get("context") or {}
    t = (text or "").strip()

    if not t:
        return False

    # -- New Task --
    if action == "new_task":
        task_id = create_task(chat_id, user_id, "/task {}".format(t))
        task = load_json(task_file("pending", task_id))
        task_code = task.get("task_code", "-")
        send_text(
            chat_id,
            "任务已创建: [{code}] {task_id}\n状态: pending\n内容: {text}".format(
                code=task_code,
                task_id=task_id,
                text=t[:200],
            ),
            reply_markup=task_inline_keyboard(task_code),
        )
        return True

    # -- Screenshot --
    if action == "screenshot":
        try:
            run_screenshot_once(chat_id, t or "请截图")
        except Exception as exc:
            send_text(chat_id, "截图失败: {}".format(str(exc)[:1000]))
        return True

    # -- Archive Search --
    if action == "archive_search":
        handle_command(chat_id, user_id, "/archive {}".format(t))
        return True

    # -- Pipeline Config --
    if action == "pipeline_config":
        handle_command(chat_id, user_id, "/set_pipeline {}".format(t))
        return True

    # -- Mgr Restart (needs OTP) --
    if action == "mgr_restart":
        handle_command(chat_id, user_id, "/mgr_restart {}".format(t))
        return True

    # -- Mgr Reinit (needs OTP) --
    if action == "mgr_reinit":
        handle_command(chat_id, user_id, "/mgr_reinit {}".format(t))
        return True

    # -- Ops Restart (needs OTP) --
    if action == "ops_restart":
        handle_command(chat_id, user_id, "/ops_restart {}".format(t))
        return True

    # -- Set Workspace (path + OTP) --
    if action == "set_workspace":
        handle_command(chat_id, user_id, "/ops_set_workspace {}".format(t))
        return True

    # -- Reset Workspace (needs OTP) --
    if action == "reset_workspace":
        handle_command(chat_id, user_id, "/ops_set_workspace default {}".format(t))
        return True

    # -- Accept with OTP --
    if action == "accept_otp":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/accept {} {}".format(ref, t))
        return True

    # -- Reject with OTP + reason --
    if action == "reject_otp":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/reject {} {}".format(ref, t))
        return True

    # -- Reject with reason only --
    if action == "reject_reason":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/reject {} {}".format(ref, t))
        return True

    # -- Archive Show --
    if action == "archive_show":
        handle_command(chat_id, user_id, "/archive_show {}".format(t))
        return True

    # -- Archive Log --
    if action == "archive_log":
        handle_command(chat_id, user_id, "/archive_log {}".format(t))
        return True

    # -- Auth Debug --
    if action == "auth_debug":
        handle_command(chat_id, user_id, "/auth_debug {}".format(t))
        return True

    # -- Pipeline Config Custom --
    if action == "pipeline_config_custom":
        handle_command(chat_id, user_id, "/set_pipeline {}".format(t))
        return True

    # -- Workspace Add --
    if action == "workspace_add":
        handle_command(chat_id, user_id, "/workspace_add {}".format(t))
        return True

    return False


def run_restart_all(operator_chat_id: int, operator_user_id: int) -> Tuple[bool, str]:
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "restart-from-telegram.ps1"
    if not script.exists():
        return False, "missing script: {}".format(script)
    request_id = "tg-" + str(int(time.time() * 1000))
    caller_pid = os.getpid()
    # Fire-and-forget: restart is executed by a detached script so coordinator
    # does not deadlock/kill itself during in-process restart.
    proc = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OperatorChatId",
            str(operator_chat_id),
            "-OperatorUserId",
            str(operator_user_id),
            "-RequestId",
            request_id,
            "-CallerPid",
            str(caller_pid),
            "-BypassMutex",
            "-HardRestart",
            "-NoHealthWait",
        ],
        cwd=str(repo),
        text=True,
    )
    if proc.pid:
        return True, "restart dispatched (pid={}, request_id={})".format(proc.pid, request_id)
    return False, "failed to dispatch restart job"


def write_manager_signal(action: str, args: dict, requested_by: int) -> str:
    """Write a control signal for manager.py to act on."""
    request_id = "mgr-{}".format(int(time.time() * 1000))
    sig_path = tasks_root() / "state" / "manager_signal.json"
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(sig_path, {
        "action": action,
        "args": args,
        "requested_by": requested_by,
        "requested_at": utc_iso(),
        "request_id": request_id,
    })
    return request_id


def read_manager_status() -> Optional[Dict]:
    status_path = tasks_root() / "state" / "manager_status.json"
    if not status_path.exists():
        return None
    try:
        return load_json(status_path)
    except Exception:
        return None


def is_risky_workspace(path: Path) -> bool:
    p = str(path.resolve()).lower().replace("\\", "/")
    blocked = [
        "/.ssh",
        "/.aws",
        "/.gnupg",
        "/windows/system32",
        "/program files",
    ]
    return any(key in p for key in blocked)


def parse_otp(text: str) -> Optional[str]:
    # Accept plain "123456" and wrapped forms like "<123456>".
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text or "")
    if not m:
        return None
    return m.group(1)


def parse_set_workspace_command(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.match(r"^/ops_set_workspace\s+(.+?)\s+(\d{6})\s*$", (text or "").strip(), re.IGNORECASE)
    if not m:
        return None, None
    raw_path = m.group(1).strip().strip('"').strip("'")
    otp = m.group(2).strip()
    return raw_path, otp


def parse_pick_workspace_command(text: str) -> Tuple[Optional[int], Optional[str]]:
    m = re.match(r"^/ops_set_workspace_pick\s+(\d+)\s+(\d{6})\s*$", (text or "").strip(), re.IGNORECASE)
    if not m:
        return None, None
    try:
        idx = int(m.group(1))
    except Exception:
        return None, None
    return idx, m.group(2).strip()


def verify_risky_operation(chat_id: int, user_id: int, otp: Optional[str], usage: str) -> Tuple[bool, Optional[str]]:
    if not is_ops_allowed(chat_id, user_id):
        return False, "not authorized for {}".format(usage)
    if not get_auth_state():
        return False, "2FA 未初始化。请先执行 /auth_init"
    token = (otp or "").strip()
    if not token:
        return False, "用法: {}".format(usage)
    otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
    if not verify_otp(token, window=otp_window):
        return False, "二次认证失败：OTP 无效或已过期"
    return True, None


def _requires_acceptance_2fa() -> bool:
    """Return True when TASK_STRICT_ACCEPTANCE=1 AND 2FA has been initialized."""
    if os.getenv("TASK_STRICT_ACCEPTANCE", "0") != "1":
        return False
    return bool(get_auth_state())


def handle_command(chat_id: int, user_id: int, text: str) -> bool:
    t = (text or "").strip()
    if t.startswith("/menu") or t.startswith("/start"):
        active_workspace = resolve_active_workspace()
        auth_ready = "已启用" if get_auth_state() else "未初始化"
        backend = get_agent_backend()
        model = get_claude_model() or "(未设置)"
        send_text(
            chat_id,
            WELCOME_TEXT.format(
                workspace=str(active_workspace),
                backend=backend,
                model=model,
                auth=auth_ready,
            ),
            reply_markup=main_menu_keyboard(),
        )
        return True

    if t.startswith("/help"):
        send_text(
            chat_id,
            HELP_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/screenshot"):
        try:
            body = t[12:].strip() if len(t) > 11 else ""
            run_screenshot_once(chat_id, body or "请截图")
        except Exception as exc:
            send_text(chat_id, "截图失败: {}".format(str(exc)[:1000]))
        return True

    if t.startswith("/ops_whoami"):
        active_workspace = resolve_active_workspace()
        send_text(
            chat_id,
            "身份信息\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "chat_id={}\n"
            "user_id={}\n"
            "ops_allowed={}\n"
            "workspace={}\n"
            "2fa_initialized={}".format(
                chat_id,
                user_id,
                str(is_ops_allowed(chat_id, user_id)).lower(),
                str(active_workspace),
                str(bool(get_auth_state())).lower(),
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/info"):
        backend = get_agent_backend()
        model = get_claude_model() or "(未设置)"
        provider = get_model_provider() or "(未设置)"
        active_workspace = resolve_active_workspace()
        lines = [
            "系统信息",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "当前后端: {}".format(backend),
            "AI模型: {}".format(model),
            "模型提供商: {}".format(provider),
            "工作区: {}".format(str(active_workspace)),
            "2FA: {}".format("已启用" if get_auth_state() else "未初始化"),
        ]
        if backend == "pipeline":
            stages = get_pipeline_stages()
            lines.append("流水线: {}".format(format_pipeline_stages(stages)))
        send_text(chat_id, "\n".join(lines), reply_markup=back_to_menu_keyboard())
        return True

    if t.startswith("/auth_init"):
        st = init_authenticator(issuer="aming-claw", account_name="telegram-ops")
        secret_line = (
            "secret(base32)={}".format(st.get("secret_b32", ""))
            if st.get("created")
            else "secret(base32)={}".format(st.get("masked_secret", ""))
        )
        send_text(
            chat_id,
            (
                "2FA {}。\n"
                "{}\n"
                "otpauth_uri={}\n"
                "period={}秒, digits={}\n"
                "seed_file={}\n"
                "请将 secret/二维码信息保存到你的 authenticator。"
            ).format(
                "已初始化" if st.get("created") else "已存在，沿用现有配置",
                secret_line,
                st.get("otpauth_uri", ""),
                st.get("period_sec", 60),
                st.get("digits", 6),
                st.get("seed_file", ""),
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/auth_status"):
        st = get_auth_state()
        if not st:
            send_text(
                chat_id,
                "2FA 未初始化。请先执行 /auth_init",
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        send_text(
            chat_id,
            "2FA 已启用\nsecret={}\nperiod={}秒, digits={}\nupdated_at={}".format(
                st.get("secret_b32", "")[:4] + "***" + st.get("secret_b32", "")[-4:],
                st.get("period_sec", 60),
                st.get("digits", 6),
                st.get("updated_at", ""),
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/auth_debug"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /auth_debug")
            return True
        otp = parse_otp(t)
        if not otp:
            send_text(chat_id, "用法: /auth_debug <6位OTP>")
            return True
        otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
        info = debug_verify_otp(otp, window=otp_window)
        checks = info.get("checks") or []
        lines = [
            "auth_debug:",
            "ok={}".format(str(bool(info.get("ok"))).lower()),
            "should_pass_verify={}".format(str(bool(info.get("should_pass_verify"))).lower()),
            "reason={}".format(info.get("reason", "")),
            "now_ts={}".format(info.get("now_ts", "")),
            "configured_period_sec={}".format(info.get("configured_period_sec", "")),
            "allow_30_fallback={}".format(str(bool(info.get("allow_30_fallback"))).lower()),
            "digits={}".format(info.get("digits", "")),
            "window={}".format(info.get("window", "")),
        ]
        for ch in checks:
            lines.append("period {} matched={}".format(ch.get("period_sec"), str(bool(ch.get("matched"))).lower()))
        send_text(chat_id, "\n".join(lines))
        return True

    if t.startswith("/switch_backend"):
        parts = t.split(maxsplit=1)
        backend = parts[1].strip().lower() if len(parts) >= 2 else ""
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /switch_backend")
            return True
        if backend not in KNOWN_BACKENDS:
            send_text(
                chat_id,
                "用法: /switch_backend <{}>\n当前后端: {}".format(
                    "|".join(sorted(KNOWN_BACKENDS)), get_agent_backend()
                ),
            )
            return True
        set_agent_backend(backend, changed_by=user_id)
        if backend == "pipeline":
            stages = get_pipeline_stages()
            if stages:
                send_text(
                    chat_id,
                    "后端已切换为: pipeline\n流水线: {}".format(format_pipeline_stages(stages)),
                )
            else:
                send_text(
                    chat_id,
                    "后端已切换为: pipeline\n⚠️ 尚未配置流水线阶段，请用 /set_pipeline 配置。\n"
                    "示例: /set_pipeline plan:claude code:claude verify:codex",
                )
        else:
            send_text(chat_id, "后端已切换为: {}\n新建任务将使用 {} 执行。".format(backend, backend))
        return True

    if t.startswith("/switch_model"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /switch_model")
            return True
        parts = t.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) >= 2 else ""
        current_model = get_claude_model() or "(默认)"
        current_provider = get_model_provider() or "claude-cli"

        if arg:
            # 直接指定：自动检测 provider
            if arg.startswith("claude"):
                provider = "anthropic"
            elif arg.startswith(("gpt-", "o1", "o3")):
                provider = "openai"
            else:
                provider = ""
            set_claude_model(arg, provider=provider, changed_by=user_id)
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            send_text(chat_id, "模型已切换为: {} `{}`".format(tag, arg))
            return True

        # 无参数 → 从 API 拉取并展示 inline keyboard
        send_text(chat_id, "正在获取可用模型列表...")
        try:
            models = get_available_models()
        except Exception:
            models = []

        # 如果 API 不可用，用内置列表兜底
        if not models:
            models = [{"id": m, "provider": "anthropic"} for m in KNOWN_CLAUDE_MODELS]

        # 每行一个按钮，最多显示 20 个（inline_keyboard 限制）
        rows = []
        for i, m in enumerate(models[:20]):
            label = "{}. {}".format(i + 1, make_label(m))
            cb = "model_select:{}:{}".format(m["provider"], m["id"])
            rows.append([{"text": label, "callback_data": cb}])

        keyboard = {"inline_keyboard": rows}
        send_text(
            chat_id,
            "当前模型: `{}` ({})\n\n选择新模型:".format(current_model, current_provider),
            reply_markup=keyboard,
        )
        return True

    if t.startswith("/set_pipeline"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /set_pipeline")
            return True
        parts = t.split(maxsplit=1)
        raw = parts[1].strip().lower() if len(parts) >= 2 else ""
        if not raw:
            preset_list = "\n".join("  {} → {}".format(k, format_pipeline_stages(v)) for k, v in PIPELINE_PRESETS.items())
            send_text(
                chat_id,
                "用法: /set_pipeline <stage:backend ...>\n"
                "  示例: /set_pipeline plan:claude code:claude verify:codex\n\n"
                "内置预设:\n{}\n\n"
                "可用 backend: codex | claude\n"
                "可用 stage: plan, code, implement, verify, test, review, ...".format(preset_list),
            )
            return True
        # Check for preset name
        if raw in PIPELINE_PRESETS:
            stages = PIPELINE_PRESETS[raw]
        else:
            from config import _parse_pipeline_stages
            stages = _parse_pipeline_stages(raw)
        if not stages:
            send_text(chat_id, "无法解析流水线配置: {!r}".format(raw))
            return True
        set_pipeline_stages(stages, changed_by=user_id)
        send_text(
            chat_id,
            "流水线已配置并激活:\n{}\n\n"
            "新建任务将按此流水线执行。\n"
            "用 /show_pipeline 查看详情，/switch_backend codex 恢复单后端模式。".format(
                format_pipeline_stages(stages)
            ),
        )
        return True

    if t.startswith("/show_pipeline"):
        backend = get_agent_backend()
        stages = get_pipeline_stages()
        if backend != "pipeline":
            send_text(
                chat_id,
                "当前后端: {} (非流水线模式)\n"
                "已保存流水线: {}\n\n"
                "用 /switch_backend pipeline 激活流水线模式。".format(
                    backend, format_pipeline_stages(stages) if stages else "(未配置)"
                ),
            )
            return True
        if not stages:
            send_text(
                chat_id,
                "当前后端: pipeline，但流水线阶段未配置。\n"
                "请用 /set_pipeline 配置，例:\n"
                "/set_pipeline plan:claude code:claude verify:codex",
            )
            return True
        lines = ["当前流水线 (后端=pipeline):"]
        for i, s in enumerate(stages, 1):
            lines.append("  {}. {}({})".format(i, s.get("name", "?"), s.get("backend", "?")))
        lines.append("\n内置预设: " + ", ".join(PIPELINE_PRESETS.keys()))
        send_text(chat_id, "\n".join(lines))
        return True

    if t.startswith("/mgr_status"):
        status = read_manager_status()
        if not status:
            send_text(chat_id, "manager 未运行或状态文件不存在。\n可通过 start.ps1 启动 manager 服务。")
            return True
        services = status.get("services") or {}
        lines = ["管理服务状态 (更新: {})".format(status.get("updated_at", "-"))]
        for name, svc_status in services.items():
            lines.append("  {}: {}".format(name, svc_status))
        lines.append("当前后端: {}".format(get_agent_backend()))
        lines.append("manager pid: {}".format(status.get("pid", "-")))
        send_text(chat_id, "\n".join(lines))
        return True

    if t.startswith("/mgr_restart"):
        otp = parse_otp(t)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/mgr_restart <6位OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        request_id = write_manager_signal("restart", {}, user_id)
        send_text(
            chat_id,
            "重启信号已发送 (request_id={})。\n"
            "manager 将在 {}s 内响应，重启 coordinator + executor。".format(
                request_id, os.getenv("MANAGER_POLL_SEC", "5")
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/mgr_reinit"):
        otp = parse_otp(t)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/mgr_reinit <6位OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        request_id = write_manager_signal("reinit", {}, user_id)
        send_text(
            chat_id,
            "自我迭代更新信号已发送 (request_id={})。\n"
            "manager 将执行: git pull → 重启所有服务。\n"
            "服务重启期间 Telegram 消息可能短暂无响应。".format(request_id),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/ops_restart"):
        otp = parse_otp(t)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/ops_restart <6位OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        send_text(chat_id, "开始执行 restart-all...")
        ok, msg = run_restart_all(chat_id, user_id)
        send_text(
            chat_id,
            "restart-all: {}\n{}".format("ok" if ok else "failed", msg),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if t.startswith("/ops_set_workspace_pick"):
        idx, otp = parse_pick_workspace_command(t)
        ok, msg = verify_risky_operation(
            chat_id,
            user_id,
            otp,
            "/ops_set_workspace_pick <序号> <6位OTP>",
        )
        if not ok:
            send_text(chat_id, msg or "operation blocked")
            return True
        if idx is None or idx <= 0:
            send_text(chat_id, "用法: /ops_set_workspace_pick <序号> <6位OTP>")
            return True
        candidates = read_workspace_candidates(chat_id, user_id)
        if not candidates:
            send_text(chat_id, "没有可选候选，请先执行 /ops_set_workspace <path|关键词> <6位OTP>")
            return True
        if idx > len(candidates):
            send_text(chat_id, "序号越界。当前可选范围: 1-{}".format(len(candidates)))
            return True
        target = candidates[idx - 1]
        if is_risky_workspace(target):
            send_text(chat_id, "拒绝切换到高风险目录: {}".format(str(target)))
            return True
        set_workspace_override(target, changed_by=user_id)
        clear_workspace_candidates(chat_id, user_id)
        send_text(chat_id, "workspace 已切换为: {}".format(str(target)))
        return True

    if t.startswith("/ops_set_workspace"):
        raw_path, otp = parse_set_workspace_command(t)
        ok, msg = verify_risky_operation(
            chat_id,
            user_id,
            otp,
            "/ops_set_workspace <path|default> <6位OTP>",
        )
        if not ok:
            send_text(chat_id, msg or "operation blocked")
            return True
        if not raw_path:
            send_text(chat_id, "用法: /ops_set_workspace <path|default> <6位OTP>")
            return True
        if raw_path.lower() in {"default", "reset"}:
            clear_workspace_override(changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, "workspace 已恢复为环境变量 CODEX_WORKSPACE")
            return True
        p = Path(raw_path).expanduser()
        if p.exists() and p.is_dir():
            rp = p.resolve()
            if is_risky_workspace(rp):
                send_text(chat_id, "拒绝切换到高风险目录: {}".format(str(rp)))
                return True
            set_workspace_override(rp, changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, "workspace 已切换为: {}".format(str(rp)))
            return True

        candidates = find_git_workspace_candidates(raw_path)
        if not candidates:
            send_text(chat_id, "未找到匹配的 Git 工作目录: {}".format(raw_path))
            return True
        if len(candidates) == 1:
            target = candidates[0]
            if is_risky_workspace(target):
                send_text(chat_id, "拒绝切换到高风险目录: {}".format(str(target)))
                return True
            set_workspace_override(target, changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, "workspace 已切换为: {}".format(str(target)))
            return True

        store_workspace_candidates(chat_id, user_id, raw_path, candidates)
        lines = [
            "检索到多个 Git 工作目录，请使用 /ops_set_workspace_pick <序号> <6位OTP> 选择："
        ]
        for idx, candidate in enumerate(candidates, 1):
            lines.append("{}. {}".format(idx, str(candidate)))
        send_text(chat_id, "\n".join(lines[:25]))
        return True

    if t.startswith("/accept"):
        parts = t.split(maxsplit=2)
        if len(parts) < 2:
            send_text(chat_id, "用法: /accept <task_id|代号> [OTP]")
            return True
        task_ref = parts[1].strip()
        otp_token = parts[2].strip() if len(parts) >= 3 else None
        if _requires_acceptance_2fa():
            otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
            if not otp_token:
                send_text(
                    chat_id,
                    "2FA is required for acceptance.\n"
                    "Usage: /accept {} <6-digit OTP>".format(task_ref),
                )
                return True
            if not verify_otp(otp_token, window=otp_window):
                send_text(
                    chat_id,
                    "2FA failed: OTP invalid or expired.\n"
                    "Retry: /accept {} <OTP>".format(task_ref),
                )
                return True
        found = find_task(task_ref)
        if not found:
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    "任务已归档，无需重复验收。\narchive_id={}\nstatus={}".format(
                        archived.get("archive_id", ""),
                        status_tag(archived.get("status", "unknown")),
                    ),
                )
                return True
            send_text(chat_id, "任务不存在: {}".format(task_ref))
            return True
        stage = str(found.get("_stage") or "")
        if stage != "results":
            send_text(chat_id, "任务尚未进入验收阶段，当前 stage={}".format(stage))
            return True
        if str(found.get("status") or "") not in {"pending_acceptance", "rejected", "completed", "failed"}:
            send_text(chat_id, "该任务当前状态无需验收: {}".format(status_tag(found.get("status", "unknown"))))
            return True

        found["status"] = "accepted"
        found["updated_at"] = utc_iso()
        found["completed_at"] = utc_iso()
        acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
        acceptance["state"] = "accepted"
        acceptance["acceptance_required"] = True
        acceptance["archive_allowed"] = True
        acceptance["accepted_at"] = utc_iso()
        acceptance["accepted_by"] = int(user_id)
        acceptance["updated_at"] = utc_iso()
        found["acceptance"] = acceptance
        result_path = task_stage_file(found)
        save_json(result_path, found)
        runlog_path = Path(str((found.get("executor") or {}).get("runlog_file") or ""))
        update_task_runtime(found, status="accepted", stage="results")
        mark_task_finished(
            found,
            status="accepted",
            stage="results",
            result_file=str(result_path),
            runlog_file=str(runlog_path) if runlog_path.exists() else "",
            summary=build_status_summary(found),
            error=str(found.get("error") or ""),
        )
        archive_meta = archive_task_result(found, result_path, runlog_path if runlog_path.exists() else None)

        # ── Git: commit changes after acceptance ──
        git_commit_msg = ""
        try:
            commit_result = commit_after_acceptance(
                task_id=found.get("task_id", ""),
                task_code=found.get("task_code", ""),
                task_text=found.get("text", ""),
            )
            if commit_result.get("success"):
                sha = commit_result.get("commit_sha", "")
                files = commit_result.get("committed_files", [])
                if files:
                    git_commit_msg = "\nGit: 已提交变更 (commit={}, {} 个文件)".format(sha, len(files))
                else:
                    git_commit_msg = "\nGit: 无新增变更需要提交"
            elif commit_result.get("error"):
                git_commit_msg = "\nGit: 提交失败 - {}".format(commit_result["error"])
        except Exception as exc:
            git_commit_msg = "\nGit: 提交异常 - {}".format(str(exc)[:200])

        send_text(
            chat_id,
            "任务 [{code}] {task_id} 验收通过并归档。\n状态: 验收通过\narchive_id={archive_id}{git_msg}\n可用 /archive_show {archive_id} 查看归档详情。".format(
                code=found.get("task_code", "-"),
                task_id=found.get("task_id", ""),
                archive_id=archive_meta.get("archive_id", ""),
                git_msg=git_commit_msg,
            ),
        )
        archive_path = Path(str(archive_meta.get("archive_file") or ""))
        if archive_path.exists():
            send_text(chat_id, "归档文件已生成: {}".format(str(archive_path)))
        return True

    if t.startswith("/reject"):
        raw_reject = t[len("/reject"):].strip()
        reject_parts = raw_reject.split(None, 2)
        if not reject_parts:
            send_text(chat_id, "用法: /reject <task_id|代号> [OTP] [原因]")
            return True
        task_ref = reject_parts[0]
        if _requires_acceptance_2fa():
            otp_token = reject_parts[1] if len(reject_parts) >= 2 else None
            reason = reject_parts[2] if len(reject_parts) >= 3 else "(not provided)"
            otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
            if not otp_token:
                send_text(
                    chat_id,
                    "2FA is required for rejection.\n"
                    "Usage: /reject {} <6-digit OTP> [reason]".format(task_ref),
                )
                return True
            if not verify_otp(otp_token, window=otp_window):
                send_text(
                    chat_id,
                    "2FA failed: OTP invalid or expired.\n"
                    "Retry: /reject {} <OTP> [reason]".format(task_ref),
                )
                return True
        else:
            reason = " ".join(reject_parts[1:]) if len(reject_parts) >= 2 else "(未提供)"
        if not task_ref:
            send_text(chat_id, "用法: /reject <task_id|代号> [OTP] [原因]")
            return True
        found = find_task(task_ref)
        if not found:
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    "任务已归档，无法拒绝验收。\narchive_id={}\nstatus={}".format(
                        archived.get("archive_id", ""),
                        status_tag(archived.get("status", "unknown")),
                    ),
                )
                return True
            send_text(chat_id, "任务不存在: {}".format(task_ref))
            return True
        stage = str(found.get("_stage") or "")
        if stage != "results":
            send_text(chat_id, "任务尚未进入验收阶段，当前 stage={}".format(stage))
            return True
        if str(found.get("status") or "") not in {"pending_acceptance", "rejected", "completed", "failed"}:
            send_text(chat_id, "该任务当前状态不可拒绝: {}".format(status_tag(found.get("status", "unknown"))))
            return True

        found["status"] = "rejected"
        found["updated_at"] = utc_iso()
        acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
        acceptance["state"] = "rejected"
        acceptance["acceptance_required"] = True
        acceptance["archive_allowed"] = False
        acceptance["rejected_at"] = utc_iso()
        acceptance["rejected_by"] = int(user_id)
        acceptance["reason"] = reason or "(未提供)"
        acceptance["updated_at"] = utc_iso()
        found["acceptance"] = acceptance
        result_path = task_stage_file(found)
        save_json(result_path, found)
        update_task_runtime(found, status="rejected", stage="results")
        mark_task_finished(
            found,
            status="rejected",
            stage="results",
            result_file=str(result_path),
            runlog_file=str((found.get("executor") or {}).get("runlog_file") or ""),
            summary=build_status_summary(found),
            error=str(found.get("error") or ""),
        )
        # ── Git: rollback to checkpoint on rejection ──
        git_rollback_msg = ""
        checkpoint = str(found.get("_git_checkpoint") or "")
        if checkpoint:
            try:
                rb_result = rollback_to_checkpoint(checkpoint)
                if rb_result.get("success"):
                    git_rollback_msg = "\nGit: 已回退到检查点 {} (回退前: {})".format(
                        rb_result.get("current_commit", ""),
                        rb_result.get("reverted_commit", ""),
                    )
                elif rb_result.get("error"):
                    git_rollback_msg = "\nGit: 回退失败 - {}".format(rb_result["error"])
            except Exception as exc:
                git_rollback_msg = "\nGit: 回退异常 - {}".format(str(exc)[:200])
        else:
            git_rollback_msg = "\nGit: 无检查点记录，跳过回退"

        send_text(
            chat_id,
            "任务 [{code}] {task_id} 已标记为验收拒绝。\n状态: 验收拒绝\n原因: {reason}{git_msg}\n可用 /status {code} 继续跟踪；通过 /accept {code} 可改为验收通过后归档。".format(
                code=found.get("task_code", "-"),
                task_id=found.get("task_id", ""),
                reason=acceptance.get("reason", "(未提供)"),
                git_msg=git_rollback_msg,
            ),
        )
        return True

    if t.startswith("/clear_tasks"):
        active = list_active_tasks(chat_id=chat_id)
        if not active:
            send_text(chat_id, "当前没有活动任务，无需清空。", reply_markup=back_to_menu_keyboard())
            return True
        removed = clear_active_tasks(chat_id)
        if removed == 0:
            send_text(chat_id, "当前所有任务均在运行中，无法清空。", reply_markup=back_to_menu_keyboard())
        else:
            send_text(
                chat_id,
                "已清空任务列表，共移除 {} 个已归档/待验收任务（运行中的任务已保留）。".format(removed),
                reply_markup=back_to_menu_keyboard(),
            )
        return True

    if t.startswith("/status"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            active = list_active_tasks(chat_id=chat_id)
            state_items = [x for x in list_task_state_candidates() if int(x.get("chat_id") or 0) == int(chat_id)]
            # Merge by task_id, prefer state snapshot freshness.
            merged_by_id: Dict[str, Dict] = {}
            for item in active:
                task_id = str(item.get("task_id") or "")
                if task_id:
                    merged_by_id[task_id] = dict(item)
            for st in state_items:
                task_id = str(st.get("task_id") or "")
                if not task_id or task_id not in merged_by_id:
                    # Skip tasks not in active list (already cleared/archived)
                    continue
                base = merged_by_id[task_id]
                base["_status_snapshot"] = st
                base["status"] = st.get("status", base.get("status"))
                base["_stage"] = st.get("stage", base.get("_stage", "unknown"))
                base["updated_at"] = st.get("updated_at", base.get("updated_at", ""))
                base["task_code"] = st.get("task_code", base.get("task_code", "-"))
                merged_by_id[task_id] = base
            merged = sorted(merged_by_id.values(), key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            if not merged:
                send_text(
                    chat_id,
                    "当前没有活动任务（含待验收）。",
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            lines = ["活动任务列表（含待验收）:"]
            for item in merged[:20]:
                lines.append(
                    "[{code}] {status}({status_tag}) {action}\n验收: {acceptance}\n任务ID={task_id}\n更新时间={updated}\n内容: {text}".format(
                        code=item.get("task_code", "-"),
                        status=item.get("status", "unknown"),
                        status_tag=status_tag(item.get("status", "unknown")),
                        action=item.get("action", "codex"),
                        acceptance=acceptance_tag(item),
                        task_id=item.get("task_id", ""),
                        updated=item.get("updated_at", ""),
                        text=str(item.get("text", "")).strip()[:80],
                    )
                )
            send_text(chat_id, "\n\n".join(lines[:50]), reply_markup=task_list_action_keyboard())
            return True
        task_ref = parts[1].strip()
        found = find_task(task_ref)
        if not found:
            resolved = resolve_task_ref(task_ref)
            if resolved:
                st = task_status_snapshot(resolved)
                if st:
                    send_text(
                        chat_id,
                        "任务 [{code}] {task_id} 状态: {status}({status_tag})\naction={action}\nstage={stage}\nupdated_at={updated}\nstarted_at={started}\nended_at={ended}\n结束标记={end_marker}\n概要: {summary}".format(
                            code=st.get("task_code", "-"),
                            task_id=st.get("task_id", ""),
                            status=st.get("status", "unknown"),
                            status_tag=status_tag(st.get("status", "unknown")),
                            action=st.get("action", "codex"),
                            stage=st.get("stage", "unknown"),
                            updated=st.get("updated_at", ""),
                            started=st.get("started_at", ""),
                            ended=st.get("ended_at", ""),
                            end_marker=str(bool(st.get("has_end_marker"))).lower(),
                            summary=str(st.get("summary", "")).strip()[:300] or "(暂无概要)",
                        ),
                    )
                    return True
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    "归档任务 [{code}] 状态: {status}({status_tag})\n验收: 验收通过(已归档)\naction={action}\narchive_id={archive_id}\ntask_id={task_id}\ncompleted_at={completed_at}\n概要: {summary}".format(
                        code=archived.get("task_code", "-"),
                        status=archived.get("status", "unknown"),
                        status_tag=status_tag(archived.get("status", "unknown")),
                        action=archived.get("action", "unknown"),
                        archive_id=archived.get("archive_id", ""),
                        task_id=archived.get("task_id", ""),
                        completed_at=archived.get("completed_at", ""),
                        summary=archived.get("summary", ""),
                    ),
                )
                return True
            send_text(chat_id, "任务不存在: {}".format(task_ref))
            return True
        found = merge_task_with_status(found)
        executor = found.get("executor") or {}
        code = found.get("task_code", "-")
        acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
        st = found.get("_status_snapshot") if isinstance(found.get("_status_snapshot"), dict) else {}
        send_text(
            chat_id,
            "任务 [{code}] {task_id} 状态: {status}({status_tag})\n验收标识: {acceptance_tag}\naction={action}\nstage={stage}\nupdated_at={updated}\nstarted_at={started}\nended_at={ended}\n结束标记={end_marker}\nelapsed_ms={elapsed}\n概要: {summary}\n下一步: {next_action}\n验收文档: {doc_file}\n验收用例: {cases_file}".format(
                code=code,
                task_id=found.get("task_id", ""),
                status=found.get("status", "unknown"),
                status_tag=status_tag(found.get("status", "unknown")),
                acceptance_tag=acceptance_tag(found),
                action=found.get("action", "codex"),
                stage=found.get("_stage", "unknown"),
                updated=found.get("updated_at", ""),
                started=st.get("started_at", ""),
                ended=st.get("ended_at", ""),
                end_marker=str(bool(st.get("has_end_marker"))).lower(),
                elapsed=status_elapsed_ms(found),
                summary=build_status_summary(found),
                next_action=acceptance_next_action(found),
                doc_file=acceptance.get("doc_file", ""),
                cases_file=acceptance.get("cases_file", ""),
            ),
        )
        task_id = str(found.get("task_id") or "")
        if task_id:
            send_text(chat_id, build_events_text(task_id, str(found.get("task_code") or "-"), limit=8))
        return True

    if t.startswith("/events"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, "用法: /events <task_id|代号>")
            return True
        task_ref = parts[1].strip()
        task_id = resolve_task_ref(task_ref) or task_ref
        st = task_status_snapshot(task_id)
        if not st:
            found = find_task(task_ref)
            if found:
                task_id = str(found.get("task_id") or task_id)
                code = str(found.get("task_code") or "-")
                send_text(chat_id, build_events_text(task_id, code))
                return True
            archived = find_archive_entry(task_ref)
            if archived:
                task_id = str(archived.get("task_id") or task_id)
                code = str(archived.get("task_code") or "-")
                send_text(chat_id, build_events_text(task_id, code))
                return True
            send_text(chat_id, "任务不存在: {}".format(task_ref))
            return True
        send_text(chat_id, build_events_text(str(st.get("task_id") or task_id), str(st.get("task_code") or "-")))
        return True

    if t.startswith("/archive_show"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, "用法: /archive_show <archive_id|task_id|代号>")
            return True
        ref = parts[1].strip()
        item = find_archive_entry(ref)
        if not item:
            suggest = search_archive_entries(ref, limit=5)
            if not suggest:
                send_text(chat_id, "未找到归档任务: {}".format(ref))
                return True
            send_text(chat_id, build_archive_list_text(suggest, "未精确命中，以下为相关归档任务:"))
            return True
        send_text(
            chat_id,
            "归档详情\narchive_id={archive_id}\n任务代号={code}\ntask_id={task_id}\naction={action}\nstatus={status}\ncompleted_at={completed_at}\n概要: {summary}".format(
                archive_id=item.get("archive_id", ""),
                code=item.get("task_code", "-"),
                task_id=item.get("task_id", ""),
                action=item.get("action", "unknown"),
                status=item.get("status", "unknown"),
                completed_at=item.get("completed_at", ""),
                summary=item.get("summary", ""),
            ),
        )
        for key, caption in [
            ("archive_file", "archive"),
            ("result_file", "result"),
            ("run_log_file", "runlog"),
        ]:
            p = Path(str(item.get(key) or ""))
            if p.exists() and p.is_file():
                send_text(chat_id, "{} 文件: {}".format(caption, str(p)))
        return True

    if t.startswith("/archive_log"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, "用法: /archive_log <语意关键词|archive_id|task_id|代号>")
            return True
        ref = parts[1].strip()
        exact = find_archive_entry(ref)
        if exact:
            send_text(
                chat_id,
                "归档日志\narchive_id={archive_id}\n任务代号={code}\ntask_id={task_id}\naction={action}\nstatus={status}\n概要: {summary}".format(
                    archive_id=exact.get("archive_id", ""),
                    code=exact.get("task_code", "-"),
                    task_id=exact.get("task_id", ""),
                    action=exact.get("action", "unknown"),
                    status=exact.get("status", "unknown"),
                    summary=exact.get("summary", ""),
                ),
            )
            for key, caption in [
                ("run_log_file", "runlog"),
                ("result_file", "result"),
                ("archive_file", "archive"),
            ]:
                p = Path(str(exact.get(key) or ""))
                if p.exists() and p.is_file():
                    send_text(chat_id, "{} 文件: {}".format(caption, str(p)))
            return True
        matches = search_archive_entries(ref, limit=30)
        if not matches:
            send_text(chat_id, "未找到相关归档日志: {}".format(ref))
            return True
        send_text(
            chat_id,
            build_archive_grouped_text(
                matches,
                "归档日志检索结果（关键词: {}）:".format(ref),
                limit_per_group=4,
            )
            + "\n可继续用 /archive_log <archive_id|task_id|代号> 拉取具体日志文件。",
        )
        return True

    if t.startswith("/archive"):
        query = t[8:].strip() if len(t) > 8 else ""
        if not query:
            grouped = grouped_archive_overview(limit_per_group=3)
            if not grouped:
                send_text(chat_id, "暂无归档任务。", reply_markup=back_to_menu_keyboard())
                return True
            lines = ["归档任务分类概览:"]
            for action, info in grouped.items():
                lines.append("类型 {}: {} 条".format(action, info.get("count", 0)))
                for item in (info.get("items") or []):
                    lines.append(
                        "  [{code}] {archive_id} {status} | {summary}".format(
                            code=item.get("task_code", "-"),
                            archive_id=item.get("archive_id", ""),
                            status=item.get("status", "unknown"),
                            summary=str(item.get("summary", "")).strip()[:60],
                        )
                    )
            lines.append("点击 [归档检索] 可搜索归档任务。")
            send_text(chat_id, "\n".join(lines[:120]), reply_markup=back_to_menu_keyboard())
            return True
        matches = search_archive_entries(query, limit=20)
        if not matches:
            send_text(chat_id, "未找到相关归档任务: {}".format(query), reply_markup=back_to_menu_keyboard())
            return True
        if len(matches) > 8:
            send_text(
                chat_id,
                build_archive_grouped_text(
                    matches,
                    "归档检索结果（关键词: {}）:".format(query),
                    limit_per_group=4,
                ),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            send_text(
                chat_id,
                build_archive_list_text(matches, "归档检索结果（关键词: {}）:".format(query)),
                reply_markup=back_to_menu_keyboard(),
            )
        return True

    # ── Workspace registry commands ──────────────────────────────────────────

    if t.startswith("/workspace_add"):
        parts = t.split(maxsplit=2)
        if len(parts) < 2:
            send_text(chat_id, "用法: /workspace_add <路径> [标签]")
            return True
        raw_path = parts[1].strip()
        label = parts[2].strip() if len(parts) >= 3 else ""
        p = Path(raw_path).expanduser()
        if not p.exists() or not p.is_dir():
            send_text(chat_id, "路径不存在或不是目录: {}".format(raw_path))
            return True
        if is_risky_workspace(p.resolve()):
            send_text(chat_id, "拒绝添加高风险目录: {}".format(str(p.resolve())))
            return True
        try:
            from workspace_registry import add_workspace
            ws = add_workspace(p, label=label, created_by=user_id)
            send_text(
                chat_id,
                "工作目录已添加:\n"
                "ID: {id}\n"
                "标签: {label}\n"
                "路径: {path}\n"
                "默认: {default}".format(
                    id=ws["id"],
                    label=ws["label"],
                    path=ws["path"],
                    default="是" if ws.get("is_default") else "否",
                ),
                reply_markup=back_to_menu_keyboard(),
            )
        except ValueError as exc:
            send_text(chat_id, "添加失败: {}".format(str(exc)))
        return True

    if t.startswith("/workspace_remove"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, "用法: /workspace_remove <工作目录ID>")
            return True
        ws_id = parts[1].strip()
        from workspace_registry import remove_workspace
        if remove_workspace(ws_id):
            send_text(chat_id, "工作目录已移除: {}".format(ws_id), reply_markup=back_to_menu_keyboard())
        else:
            send_text(chat_id, "工作目录未找到: {}".format(ws_id))
        return True

    if t.startswith("/workspace_default"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, "用法: /workspace_default <工作目录ID>")
            return True
        ws_id = parts[1].strip()
        from workspace_registry import set_default_workspace, get_workspace
        if set_default_workspace(ws_id):
            ws = get_workspace(ws_id)
            send_text(
                chat_id,
                "默认工作目录已设置: {} ({})".format(ws_id, ws.get("label", "") if ws else ""),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            send_text(chat_id, "工作目录未找到: {}".format(ws_id))
        return True

    if t.startswith("/workspace_list") or t == "/workspaces":
        from workspace_registry import list_workspaces as _list_ws
        workspaces = _list_ws(include_inactive=True)
        if not workspaces:
            send_text(
                chat_id,
                "尚未注册任何工作目录。\n使用 /workspace_add <路径> [标签] 添加。",
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        lines = ["注册的工作目录:"]
        for ws in workspaces:
            flags = []
            if ws.get("is_default"):
                flags.append("默认")
            if not ws.get("active", True):
                flags.append("停用")
            flag_str = " [{}]".format(",".join(flags)) if flags else ""
            lines.append(
                "{label}{flags}\n  ID: {id}\n  路径: {path}\n  并发: {concurrent}".format(
                    label=ws.get("label", ws["id"]),
                    flags=flag_str,
                    id=ws["id"],
                    path=ws["path"],
                    concurrent=ws.get("max_concurrent", 1),
                )
            )
        send_text(chat_id, "\n\n".join(lines), reply_markup=back_to_menu_keyboard())
        return True

    if t.startswith("/workspace_status") or t == "/dispatch_status":
        from parallel_dispatcher import get_dispatcher_status
        status = get_dispatcher_status()
        workers = status.get("workers", {})
        if not workers:
            send_text(
                chat_id,
                "并行调度器未运行或无工作线程。\n"
                "确保 EXECUTOR_MODE=parallel 或有多个工作目录注册。",
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        lines = ["并行调度器状态:"]
        for ws_id, w in workers.items():
            state = "运行中" if w.get("running") else "已停止"
            busy = "忙碌({})".format(w.get("current_task_id", "")) if w.get("busy") else "空闲"
            lines.append(
                "{label} ({state})\n"
                "  ID: {id}\n"
                "  {busy} | 队列: {queue} | 完成: {done} | 失败: {fail}".format(
                    label=w.get("ws_label", ws_id),
                    state=state,
                    id=ws_id,
                    busy=busy,
                    queue=w.get("queue_size", 0),
                    done=w.get("tasks_completed", 0),
                    fail=w.get("tasks_failed", 0),
                )
            )
        send_text(chat_id, "\n\n".join(lines), reply_markup=back_to_menu_keyboard())
        return True

    return False


def _extract_workspace_target(text: str) -> Tuple[str, str]:
    """Extract @workspace:<label> prefix from text. Returns (label, remaining_text)."""
    if text.startswith("@workspace:"):
        parts = text.split(None, 1)
        if parts:
            label = parts[0][len("@workspace:"):]
            remaining = parts[1] if len(parts) > 1 else ""
            return label, remaining
    return "", text


def create_task(chat_id: int, user_id: int, raw_text: str) -> str:
    task_id = new_task_id()
    text = parse_task_text(raw_text) or raw_text.strip()
    action = infer_action(raw_text)

    # Parse workspace targeting prefix
    target_ws_label, clean_text = _extract_workspace_target(text)

    task = {
        "task_id": task_id,
        "chat_id": chat_id,
        "requested_by": user_id,
        "action": action,
        "text": clean_text if target_ws_label else text,
        "status": "pending",
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
    }

    # Add workspace routing info if specified
    if target_ws_label:
        task["target_workspace"] = target_ws_label

    task["task_code"] = register_task_created(task)
    save_json(task_file("pending", task_id), task)
    return task_id
