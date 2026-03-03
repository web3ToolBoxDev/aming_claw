import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

from i18n import t
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
    ROLE_DEFINITIONS, ROLE_PIPELINE_ORDER, STAGE_EMOJI,
    format_pipeline_stages, format_role_pipeline_stages,
    get_agent_backend, get_claude_model, get_model_provider,
    get_pipeline_stages, get_role_pipeline_stages,
    set_agent_backend, set_claude_model, set_pipeline_stages,
    set_role_pipeline_stages, set_role_stage_model,
    add_workspace_search_root, get_workspace_search_roots,
    remove_workspace_search_root, set_workspace_search_roots,
    set_config_language,
    get_skill_english_practice, set_skill_english_practice,
)
from model_registry import get_available_models, make_label, format_model_list_text, find_model
from auth import debug_verify_otp, get_auth_state, init_authenticator, verify_otp
from workspace import (
    clear_workspace_override,
    resolve_active_workspace,
    set_workspace_override,
)
from task_state import (
    append_task_event,
    archive_task_result,
    clear_active_tasks,
    list_task_state_candidates,
    find_archive_entry,
    group_archive_entries,
    grouped_archive_overview,
    list_active_tasks,
    load_runtime_state,
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
    needs_service_restart,
    rollback_to_checkpoint,
)
from task_accept import run_post_acceptance_tests
from interactive_menu import (
    main_menu_keyboard,
    system_menu_keyboard,
    archive_menu_keyboard,
    ops_menu_keyboard,
    security_menu_keyboard,
    skills_menu_keyboard,
    workspace_menu_keyboard,
    workspace_select_keyboard,
    fuzzy_workspace_add_keyboard,
    search_roots_keyboard,
    backend_select_keyboard,
    pipeline_preset_keyboard,
    pipeline_stage_overview_keyboard,
    pipeline_stage_model_keyboard,
    role_pipeline_config_keyboard,
    role_model_select_keyboard,
    model_list_keyboard,
    cancel_keyboard,
    back_to_menu_keyboard,
    confirm_cancel_keyboard,
    pending_tasks_keyboard,
    task_list_action_keyboard,
    task_mgmt_menu_keyboard,
    task_status_list_keyboard,
    task_detail_keyboard,
    tasks_overview_keyboard,
    archive_detail_keyboard,
    language_select_keyboard,
    safe_callback_data,
    TASK_STATUS_LABELS,
    TASK_STATUS_EMPTY_LABELS,
    set_pending_action,
    get_pending_action,
    peek_pending_action,
    clear_pending_action,
    WELCOME_TEXT,
    HELP_TEXT,
    SUBMENU_TEXTS,
    PENDING_PROMPTS,
    eng_practice_confirm_keyboard,
)
from workspace_queue import (
    enqueue_task,
    dequeue_task,
    list_queue,
    list_all_queues,
    queue_length,
    should_queue_task,
    promote_next_queued_task,
    remove_from_queue,
)
from task_retry import (
    retry_task,
    build_retry_summary,
    get_max_retry_iterations,
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
    roots: List[Path] = []
    # 1. Persisted config (set via menu / /workspace_search_roots command)
    config_roots = get_workspace_search_roots()
    for v in config_roots:
        p = Path(v).expanduser()
        if p.exists() and p.is_dir():
            roots.append(p.resolve())
    # 2. Environment variable (additive)
    raw = os.getenv("WORKSPACE_SEARCH_ROOTS", "").strip()
    if raw:
        for part in raw.split(os.pathsep):
            v = part.strip().strip('"').strip("'")
            if not v:
                continue
            p = Path(v).expanduser()
            if p.exists() and p.is_dir():
                roots.append(p.resolve())
    # 3. Fallback: active workspace + parent
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


def _looks_like_path(text: str) -> bool:
    """Return True if text looks like a filesystem path rather than a keyword."""
    s = text.strip()
    if not s:
        return False
    # Windows absolute path: C:\ or C:/
    if len(s) >= 3 and s[1] == ":" and s[2] in ("\\/"):
        return True
    # Unix absolute path
    if s.startswith("/"):
        return True
    # Relative path with separators
    if "\\" in s or "/" in s:
        return True
    # Starts with ~
    if s.startswith("~"):
        return True
    # Dot-relative paths
    if s.startswith("./") or s.startswith(".\\") or s == ".":
        return True
    return False


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


def _looks_like_screenshot_task_tail(tail: str) -> bool:
    """Return True when `/screenshot <tail>` looks like engineering-task text."""
    if not tail:
        return False
    return bool(re.match(
        r"^(命令|command|功能|feature|模块|module|问题|issue|误判|任务|task|"
        r"报告|report|日志|log|流程|flow|逻辑|logic|修复|fix|排查|检查|优化|"
        r"失败|异常|debug|bug)",
        tail.strip().lower(),
    ))


def is_screenshot_text(text: str) -> bool:
    """Return True only when the user's PRIMARY intent is to take a screenshot.

    Avoids false positives when screenshot keywords appear incidentally in longer
    task descriptions (e.g. "修复截图功能", "测试报告、截图、日志").
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    # 1) Slash command usually means screenshot intent.
    #    But "/screenshot 命令误判修复" is a task description, not an action.
    if low.startswith("/screenshot"):
        tail = low[len("/screenshot"):].strip()
        if not tail:
            return True
        if _looks_like_screenshot_task_tail(tail):
            return False
        return True
    # 2) Guard against task descriptions that start with screenshot keywords.
    #    Examples: "截图命令误判修复", "screenshot command misclassification fix"
    if re.match(
        r"^(截图|截屏)\s*(命令|功能|模块|问题|误判|任务|报告|日志|流程|逻辑|修复|排查|检查|优化|失败|异常|bug)",
        low,
    ):
        return False
    if re.match(
        r"^screenshot\s*(command|feature|module|issue|task|report|log|flow|logic|fix|debug|bug)",
        low,
    ):
        return False
    # 3) Extra guard: texts that start with screenshot words but clearly describe
    #    engineering work (e.g. "截图上传失败修复") should not trigger screenshot.
    if re.match(r"^(截图|截屏|screenshot|take\s+a?\s*screenshot|screen\s*shot|screen\s*cap)", low):
        if re.search(
            r"(修复|排查|检查|优化|分析|定位|失败|异常|bug|issue|fix|debug|模块|功能|"
            r"命令|逻辑|流程|报告|日志|任务|上传)",
            low,
        ) and not re.search(
            r"(给我|发我|看下|看看|看一下|一下|当前|现在|please|now|for me|desktop)",
            low,
        ):
            return False
    # 4) Common polite-prefix + screenshot verb patterns
    if re.match(r"^(请|帮我|请帮我|请帮忙|帮忙)?(截图|截屏|截个图|截个屏)", low):
        return True
    if re.match(r"^(take\s+a?\s*)?(screenshot|screen\s*shot|screen\s*cap)", low):
        return True
    # 5) Very short text (<= 15 chars) with screen-related keywords
    if len(low) <= 15:
        keys = ["screen", "屏幕", "多屏", "双屏", "all screens"]
        return any(k in low for k in keys)
    return False


def parse_task_text(text: str) -> Optional[str]:
    txt = (text or "").strip()
    if not txt:
        return None
    if txt.startswith("/task "):
        return txt[6:].strip()
    if txt.startswith("/task"):
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
        if resp.status_code >= 400:
            _raise_api_error("OpenAI", resp)
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
        if resp.status_code >= 400:
            _raise_api_error("Anthropic", resp)
        return resp.json()["content"][0]["text"].strip()[-3500:]


def _raise_api_error(provider: str, resp) -> None:
    """Extract error details from API response body and raise informative RuntimeError."""
    try:
        body = resp.json()
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            err_type = err_obj.get("type", "")
            err_msg = err_obj.get("message", "")
            detail = "{}: {}".format(err_type, err_msg) if err_type else err_msg
        else:
            detail = str(err_obj)
    except Exception:
        detail = resp.text[:500] if resp.text else ""
    raise RuntimeError(
        "{} API {} (HTTP {}): {}".format(provider, "error", resp.status_code, detail or "unknown error")
    )


def run_claude_chat(text: str) -> str:
    """One-shot chat: routes to API (if provider stored) or Claude Code CLI."""
    model = get_claude_model()
    provider = get_model_provider()

    # Only non-Claude providers (e.g. OpenAI) use direct API;
    # Anthropic/Claude models go through Claude CLI to use Max subscription OAuth
    # instead of consuming API credits via ANTHROPIC_API_KEY.
    if provider == "openai":
        return _claude_chat_via_api(text, provider, model)

    # Claude Code CLI (uses Max subscription OAuth, not API credits)
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
    # Strip env vars that interfere with Claude CLI:
    # - CLAUDECODE/CLAUDE_CODE_*: prevents "nested session" rejection
    # - ANTHROPIC_API_KEY: forces CLI to use API credits instead of Max subscription OAuth
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
                         "ANTHROPIC_API_KEY")}
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
    """Route direct chat to the active backend (codex / claude / pipeline)."""
    backend = get_agent_backend()
    if backend in ("claude", "pipeline"):
        return run_claude_chat(text)
    # codex default → codex chat
    return run_codex_chat(text)


def run_screenshot_once(chat_id: int, text: str) -> None:
    base_url = os.getenv("EXECUTOR_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    token = os.getenv("EXECUTOR_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing EXECUTOR_API_TOKEN")
    payload = {
        "task_id": "chat-screenshot-" + str(int(time.time() * 1000)),
        "action": "take_screenshot",
        "command_text": text or "screenshot",
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
            t("msg.screenshot_timing", total=timings.get("total_ms", 0), capture=timings.get("capture_ms", 0), copy=timings.get("copy_ms", 0)),
        )
    send_text(chat_id, t("msg.screenshot_done", count=sent))


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
        return (t("summary.failure_reason", reason=noop_reason))[:300]
    err = (task.get("error") or "").strip()
    if err:
        return (t("summary.error_prefix", err=err))[:300]
    return t("msg.no_summary_short")


def format_stage_execution_summary(task: Dict) -> str:
    """Build stage execution summary text for pipeline tasks.

    Returns empty string for non-pipeline tasks or when no stage data exists.
    """
    executor = task.get("executor") or {}
    if executor.get("action") != "pipeline":
        return ""
    stages = executor.get("stages")
    if not stages:
        return ""
    model_info = task.get("stages_model_info") or []
    model_map = {m["stage"]: m for m in model_info if isinstance(m, dict) and "stage" in m}

    lines = [t("msg.pipeline_exec_header")]
    for s in stages:
        stage_name = s.get("stage", "?")
        idx = s.get("stage_index", "?")
        elapsed = s.get("elapsed_ms")
        noop = s.get("noop_reason", "")
        rc = s.get("returncode")
        role_def = ROLE_DEFINITIONS.get(stage_name, {})
        emoji = role_def.get("emoji", "")
        label = role_def.get("label", stage_name)

        # Determine model display: prefer stage-level (T3), fallback to stages_model_info, then backend
        mi = model_map.get(stage_name, {})
        model = s.get("model", "") or mi.get("model", "")
        provider = s.get("provider", "") or mi.get("provider", "")
        if model and model != "(default)":
            from config import _provider_tag
            tag = _provider_tag(provider)
            model_display = "{} {}".format(model, tag).rstrip()
        else:
            model_display = s.get("backend", "?")

        # Status icon
        if elapsed is None and rc is None:
            status_icon = t("status.icon_not_executed")
            time_str = ""
        elif noop:
            status_icon = t("status.icon_fail")
            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""
        elif rc == 0 or rc is None:
            status_icon = t("status.icon_pass")
            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""
        else:
            status_icon = t("status.icon_fail")
            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""

        line = t("msg.stage_line", idx=idx, emoji=emoji, label=label, model_display=model_display, status_icon=status_icon, time_str=time_str)
        if noop:
            line += " (noop: {})".format(noop[:40])
        lines.append(line)
    return "\n".join(lines)


def status_tag(status: str) -> str:
    mapping = {
        "pending": t("status.pending"),
        "processing": t("status.processing"),
        "pending_acceptance": t("status.pending_acceptance"),
        "accepted": t("status.accepted"),
        "rejected": t("status.rejected"),
        "completed": t("status.completed"),
        "succeeded": t("status.succeeded"),
        "failed": t("status.failed"),
    }
    return mapping.get(str(status or "").strip().lower(), str(status or t("status.unknown")))


def acceptance_tag(task: Dict) -> str:
    stage = str(task.get("_stage") or task.get("stage") or "").strip().lower()
    status = str(task.get("status") or "").strip().lower()
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    state = str(acceptance.get("state") or "").strip().lower()
    if status == "accepted" or state == "accepted":
        return t("acceptance.tag_accepted")
    if status == "rejected" or state == "rejected":
        return t("acceptance.tag_rejected")
    if status == "pending_acceptance" or state == "pending":
        return t("acceptance.tag_pending")
    if stage in {"pending", "processing"}:
        return t("acceptance.tag_not_ready")
    if stage == "results" and status in {"completed", "failed", "succeeded"}:
        # Backward compatibility for historical result files before explicit pending_acceptance migration.
        return t("acceptance.tag_pending_compat")
    if stage == "archive":
        # Task was archived (normally after acceptance); treat as accepted.
        return t("acceptance.tag_accepted")
    return t("acceptance.tag_unknown")


def acceptance_next_action(task: Dict) -> str:
    code = str(task.get("task_code") or task.get("task_id") or "-")
    tag = acceptance_tag(task)
    if tag in {t("acceptance.tag_pending"), t("acceptance.tag_pending_compat"), t("acceptance.tag_rejected")}:
        return t("acceptance.next_accept_or_reject", code=code)
    if tag == t("acceptance.tag_accepted"):
        return t("acceptance.next_already_accepted", code=code)
    return t("acceptance.next_no_action")


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
            t("msg.archive_list_item",
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
        return title + "\n" + t("msg.no_results")
    lines = [title]
    for action, info in grouped.items():
        lines.append(t("msg.type_count", action=action, count=info.get("count", 0)))
        for item in (info.get("items") or []):
            lines.append(
                t("msg.archive_group_item",
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
                {"text": t("task.view_progress"), "callback_data": "status:{}".format(ref)},
                {"text": t("task.accept"), "callback_data": "accept:{}".format(ref)},
                {"text": t("task.reject"), "callback_data": "reject:{}".format(ref)},
            ],
            [
                {"text": t("task.view_events"), "callback_data": "events:{}".format(ref)},
            ],
        ]
    }


def build_events_text(task_id: str, task_code: str = "", limit: int = 12) -> str:
    rows = read_task_events(task_id, limit=limit)
    if not rows:
        return t("msg.no_events", code=task_code or "-", task_id=task_id)
    lines = [t("msg.recent_events", code=task_code or "-", task_id=task_id)]
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
        answer_callback_query(cb_id, t("callback.invalid_button"))
        return
    try:
        # ---- Noop callbacks (section headers etc.) ----
        if data.startswith("noop:"):
            answer_callback_query(cb_id)
            return

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
            answer_callback_query(cb_id, t("callback.status_queried"))
            return
        if data.startswith("events:"):
            ref = data.split(":", 1)[1].strip()
            handle_command(chat_id, user_id, "/events {}".format(ref))
            answer_callback_query(cb_id, t("callback.events_queried"))
            return
        if data.startswith("accept:"):
            ref = data.split(":", 1)[1].strip()
            if _requires_acceptance_2fa():
                set_pending_action(chat_id, user_id, "accept_otp", {"task_ref": ref})
                send_text(
                    chat_id,
                    t("msg.accept_need_2fa", ref=ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.enter_otp"))
            else:
                handle_command(chat_id, user_id, "/accept {}".format(ref))
                answer_callback_query(cb_id, t("callback.acceptance_submitted"))
            return
        if data.startswith("reject:"):
            ref = data.split(":", 1)[1].strip()
            if _requires_acceptance_2fa():
                set_pending_action(chat_id, user_id, "reject_otp", {"task_ref": ref})
                send_text(
                    chat_id,
                    t("msg.reject_need_2fa", ref=ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.enter_otp_reason"))
            else:
                set_pending_action(chat_id, user_id, "reject_reason", {"task_ref": ref})
                send_text(
                    chat_id,
                    t("msg.enter_reject_reason", ref=ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.enter_reject_reason"))
            return
        if data.startswith("retry:"):
            ref = data.split(":", 1)[1].strip()
            if _requires_acceptance_2fa():
                set_pending_action(chat_id, user_id, "retry_otp", {"task_ref": ref})
                send_text(
                    chat_id,
                    t("msg.retry_need_2fa", ref=ref),
                    reply_markup=cancel_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.enter_otp"))
            else:
                handle_command(chat_id, user_id, "/retry {}".format(ref))
                answer_callback_query(cb_id, t("callback.retry_submitted"))
            return
        if data.startswith("restart:"):
            ref = data.split(":", 1)[1].strip()
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            answer_callback_query(cb_id, t("callback.restarting"))
            try:
                from service_manager import run_restart
                ok = run_restart()
                if ok:
                    send_text(chat_id, t("msg.restart_done", ref=ref))
                else:
                    send_text(chat_id, t("msg.restart_failed_script", ref=ref))
            except Exception as exc:
                send_text(chat_id, t("msg.restart_failed", err=str(exc)[:200], ref=ref))
            return
        if data.startswith("skip_restart:"):
            ref = data.split(":", 1)[1].strip()
            answer_callback_query(cb_id, t("callback.skip_restart"))
            send_text(chat_id, t("msg.restart_skipped", ref=ref))
            return
        if data.startswith("cmd_cancel:"):
            ref = data.split(":", 1)[1].strip()
            handle_command(chat_id, user_id, "/cancel {}".format(ref))
            answer_callback_query(cb_id, t("callback.cancelled"))
            return
        if data.startswith("model_select:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            rest = data[len("model_select:"):]
            if ":" in rest:
                provider, model = rest.split(":", 1)
            else:
                provider, model = "", rest
            set_claude_model(model, provider=provider, changed_by=user_id)
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            answer_callback_query(cb_id, t("callback.switched", tag=tag, model=model))
            send_text(
                chat_id,
                t("msg.model_switched", tag=tag, model=model),
                reply_markup=back_to_menu_keyboard(),
            )
            return

        # ---- Model default selection callbacks (from model list page) ----
        if data.startswith("model_default:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.perm_insufficient"), show_alert=True)
                return
            rest = data[len("model_default:"):]
            if ":" in rest:
                provider, model_id = rest.split(":", 1)
            else:
                provider, model_id = "", rest
            # Check model availability
            m = find_model(model_id)
            if m and m.get("status") == "unavailable":
                reason = m.get("unavailable_reason", t("msg.not_available"))
                answer_callback_query(cb_id, t("callback.model_unavailable"), show_alert=True)
                send_text(chat_id, t("msg.set_failed", model=model_id, reason=reason),
                          reply_markup=back_to_menu_keyboard())
                return
            set_claude_model(model_id, provider=provider, changed_by=user_id)
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            answer_callback_query(cb_id, t("callback.set_as_default"))
            send_text(
                chat_id,
                t("msg.default_model_set", tag=tag, model=model_id),
                reply_markup=back_to_menu_keyboard(),
            )
            return

        # ---- Pipeline preset selection callbacks ----
        if data.startswith("pipeline_preset:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            preset_name = data[len("pipeline_preset:"):]
            if preset_name in PIPELINE_PRESETS:
                stages = [dict(s) for s in PIPELINE_PRESETS[preset_name]]
                # For role_pipeline preset, merge per-role model config
                if preset_name == "role_pipeline":
                    role_stages = get_role_pipeline_stages()
                    role_config = {s["name"]: s for s in role_stages if "name" in s}
                    for stage in stages:
                        name = stage.get("name", "")
                        if name in role_config:
                            rc = role_config[name]
                            if rc.get("model"):
                                stage["model"] = rc["model"]
                                stage["provider"] = rc.get("provider", "")
                # Store stages in pending action instead of applying immediately
                set_pending_action(chat_id, user_id, "pipeline_configure", {
                    "preset_name": preset_name,
                    "stages": stages,
                })
                # Show stage overview page for per-stage model adjustment
                preset_display = {
                    "plan_code_verify": "plan + code + verify",
                    "plan_code": "plan + code",
                    "code_verify": "code + verify",
                    "claude_codex": "claude + codex",
                    "role_pipeline": t("msg.role_pipeline_config"),
                }.get(preset_name, preset_name)
                send_text(
                    chat_id,
                    t("msg.stage_config_overview", pipeline=preset_display),
                    reply_markup=pipeline_stage_overview_keyboard(stages),
                )
                answer_callback_query(cb_id, t("callback.select_preset", name=preset_display))
            else:
                answer_callback_query(cb_id, t("callback.unknown_preset"), show_alert=True)
            return

        # ---- Role pipeline config callbacks ----
        if data.startswith("role_cfg:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            role_name = data[len("role_cfg:"):]
            role_def = ROLE_DEFINITIONS.get(role_name)
            if not role_def:
                answer_callback_query(cb_id, t("callback.unknown_role"), show_alert=True)
                return
            all_models = get_available_models()
            # Show all models (including unavailable) so users can pre-configure
            # roles even when a provider key isn't set yet.
            models = all_models if all_models else [
                {"id": m, "provider": "anthropic"} for m in KNOWN_CLAUDE_MODELS
            ]
            send_text(
                chat_id,
                t("msg.select_role_model", emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name)),
                reply_markup=role_model_select_keyboard(role_name, models),
            )
            answer_callback_query(cb_id, t("callback.select_model"))
            return

        if data.startswith("role_model:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            # Format: role_model:<role>:<provider>:<model_id>
            parts = data[len("role_model:"):].split(":", 2)
            if len(parts) < 3:
                answer_callback_query(cb_id, t("callback.invalid_data"), show_alert=True)
                return
            role_name, provider, model_id = parts[0], parts[1], parts[2]
            role_def = ROLE_DEFINITIONS.get(role_name)
            if not role_def:
                answer_callback_query(cb_id, t("callback.unknown_role"), show_alert=True)
                return
            try:
                set_role_stage_model(role_name, model_id, provider=provider, changed_by=user_id)
            except ValueError as exc:
                answer_callback_query(cb_id, t("callback.save_failed"), show_alert=True)
                send_text(chat_id, t("msg.save_failed", err=str(exc)),
                          reply_markup=back_to_menu_keyboard())
                return
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            answer_callback_query(cb_id, t("callback.saved", tag=tag, model=model_id))
            # Refresh the role pipeline config view
            stages = get_role_pipeline_stages()
            send_text(
                chat_id,
                t("msg.role_pipeline_panel", title=t("msg.role_pipeline_config"), role_set=t("msg.role_set", emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name), tag=tag, model=model_id), config=t("msg.role_config_current", config=format_role_pipeline_stages(stages))),
                reply_markup=role_pipeline_config_keyboard(stages),
            )
            return

        # ---- Pipeline stage configuration callbacks (overview wizard) ----
        if data.startswith("pipeline_stage_cfg:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            idx_str = data[len("pipeline_stage_cfg:"):]
            pending = peek_pending_action(chat_id, user_id)
            if not pending or pending.get("action") != "pipeline_configure":
                # Pending action lost — fall back to preset selection
                send_text(
                    chat_id,
                    t("msg.config_expired"),
                    reply_markup=pipeline_preset_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.please_reselect"))
                return
            stages = pending.get("context", {}).get("stages", [])
            try:
                stage_index = int(idx_str)
            except (ValueError, TypeError):
                answer_callback_query(cb_id, t("callback.invalid_data"), show_alert=True)
                return
            if stage_index < 0 or stage_index >= len(stages):
                answer_callback_query(cb_id, t("callback.invalid_stage"), show_alert=True)
                return
            stage = stages[stage_index]
            stage_name = stage.get("name", "?")
            all_models = get_available_models()
            models = all_models if all_models else [
                {"id": m, "provider": "anthropic"} for m in KNOWN_CLAUDE_MODELS
            ]
            # Resolve emoji for title
            role_def = ROLE_DEFINITIONS.get(stage_name)
            emoji = role_def.get("emoji", "") if role_def else STAGE_EMOJI.get(stage_name, "\u2699\ufe0f")
            send_text(
                chat_id,
                t("msg.configure_stage_model", emoji=emoji, name=stage_name),
                reply_markup=pipeline_stage_model_keyboard(stage_index, stage_name, models),
            )
            answer_callback_query(cb_id, t("callback.select_model"))
            return

        if data.startswith("stage_model:"):
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            # Format: stage_model:<index>:<provider>:<model_id>
            parts = data[len("stage_model:"):].split(":", 2)
            if len(parts) < 3:
                answer_callback_query(cb_id, t("callback.invalid_data"), show_alert=True)
                return
            idx_str, provider, model_id = parts[0], parts[1], parts[2]
            pending = peek_pending_action(chat_id, user_id)
            if not pending or pending.get("action") != "pipeline_configure":
                send_text(
                    chat_id,
                    t("msg.config_expired"),
                    reply_markup=pipeline_preset_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.please_reselect"))
                return
            ctx = pending.get("context", {})
            stages = ctx.get("stages", [])
            try:
                stage_index = int(idx_str)
            except (ValueError, TypeError):
                answer_callback_query(cb_id, t("callback.invalid_data"), show_alert=True)
                return
            if stage_index < 0 or stage_index >= len(stages):
                answer_callback_query(cb_id, t("callback.invalid_stage"), show_alert=True)
                return
            # Update pending action stages in-place
            stages[stage_index]["model"] = model_id
            stages[stage_index]["provider"] = provider
            set_pending_action(chat_id, user_id, "pipeline_configure", ctx)
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            answer_callback_query(cb_id, t("callback.saved", tag=tag, model=model_id))
            # Return to overview page with updated stages
            preset_name = ctx.get("preset_name", "")
            preset_display = {
                "plan_code_verify": "plan + code + verify",
                "plan_code": "plan + code",
                "code_verify": "code + verify",
                "claude_codex": "claude + codex",
                "role_pipeline": t("msg.role_pipeline_config"),
            }.get(preset_name, preset_name)
            send_text(
                chat_id,
                t("msg.stage_config_overview", pipeline=preset_display),
                reply_markup=pipeline_stage_overview_keyboard(stages),
            )
            return

        if data == "pipeline_apply":
            if not is_ops_allowed(chat_id, user_id):
                answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
                return
            pending = get_pending_action(chat_id, user_id)
            if not pending or pending.get("action") != "pipeline_configure":
                send_text(
                    chat_id,
                    t("msg.config_expired"),
                    reply_markup=pipeline_preset_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.please_reselect"))
                return
            ctx = pending.get("context", {})
            stages = ctx.get("stages", [])
            preset_name = ctx.get("preset_name", "")
            # Apply: persist pipeline stages
            set_pipeline_stages(stages, changed_by=user_id)
            # If role_pipeline, also sync role_pipeline_stages
            if preset_name == "role_pipeline":
                set_role_pipeline_stages(stages, changed_by=user_id)
            # Build confirmation summary
            summary_lines = []
            for s in stages:
                name = s.get("name", "?")
                model = s.get("model", "")
                prov = s.get("provider", "")
                if model:
                    tag = "[C]" if prov == "anthropic" else "[O]" if prov == "openai" else ""
                    summary_lines.append("  {}: {} {}".format(name, model, tag).rstrip())
                else:
                    summary_lines.append("  {}: {}".format(name, t("task.stage_default")))
            summary = "\n".join(summary_lines)
            send_text(
                chat_id,
                t("msg.pipeline_applied", summary=summary),
                reply_markup=back_to_menu_keyboard(),
            )
            answer_callback_query(cb_id, t("callback.config_applied"))
            return

        # ---- Workspace selection callbacks ----
        if data.startswith("ws_task_select:"):
            ws_id = data.split(":", 1)[1].strip()
            from workspace_registry import get_workspace as _get_ws_sel
            ws = _get_ws_sel(ws_id)
            if not ws:
                answer_callback_query(cb_id, t("callback.workspace_not_found"), show_alert=True)
                return
            ws_label = ws.get("label", ws_id)
            eng_on = get_skill_english_practice()
            if eng_on:
                set_pending_action(chat_id, user_id, "eng_practice_input", {"ws_id": ws_id, "ws_label": ws_label})
                send_text(chat_id, str(PENDING_PROMPTS["eng_practice_input"]),
                          reply_markup=cancel_keyboard())
            else:
                set_pending_action(chat_id, user_id, "new_task_with_workspace", {"ws_id": ws_id, "ws_label": ws_label})
                send_text(
                    chat_id,
                    PENDING_PROMPTS["new_task_with_workspace"].format(ws_label=ws_label),
                    reply_markup=cancel_keyboard(),
                )
            answer_callback_query(cb_id, t("callback.selected", label=ws_label))
            return

        # ---- Project summary workspace selection callback ----
        if data.startswith("summary_ws:"):
            ws_id = data.split(":", 1)[1].strip()
            from workspace_registry import get_workspace as _get_ws_sum
            ws = _get_ws_sum(ws_id)
            if not ws:
                answer_callback_query(cb_id, t("callback.workspace_not_found"), show_alert=True)
                return
            answer_callback_query(cb_id, t("callback.generating_summary"))
            _generate_and_send_summary(chat_id, Path(ws["path"]))
            return

        if data.startswith("ws_remove:"):
            ws_id = data.split(":", 1)[1].strip()
            from workspace_registry import remove_workspace as _rm_ws
            if _rm_ws(ws_id):
                send_text(chat_id, t("msg.workspace_dir_removed", id=ws_id), reply_markup=back_to_menu_keyboard())
                answer_callback_query(cb_id, t("callback.deleted"))
            else:
                send_text(chat_id, t("msg.workspace_dir_not_found", id=ws_id))
                answer_callback_query(cb_id, t("callback.ws_not_found"), show_alert=True)
            return

        if data.startswith("ws_default:"):
            ws_id = data.split(":", 1)[1].strip()
            from workspace_registry import set_default_workspace as _set_def, get_workspace as _get_ws_d
            if _set_def(ws_id):
                ws = _get_ws_d(ws_id)
                send_text(
                    chat_id,
                    t("msg.default_workspace_set", id=ws_id, label=ws.get("label", "") if ws else ""),
                    reply_markup=back_to_menu_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.default_set"))
            else:
                send_text(chat_id, t("msg.workspace_dir_not_found", id=ws_id))
                answer_callback_query(cb_id, t("callback.ws_not_found"), show_alert=True)
            return

        if data.startswith("ws_fuzzy_add:"):
            idx_str = data.split(":", 1)[1].strip()
            try:
                idx = int(idx_str)
            except ValueError:
                answer_callback_query(cb_id, t("callback.invalid_index"), show_alert=True)
                return
            candidates = read_workspace_candidates(chat_id, user_id)
            if not candidates:
                send_text(chat_id, t("msg.candidates_expired"), reply_markup=back_to_menu_keyboard())
                answer_callback_query(cb_id, t("callback.expired"))
                return
            if idx < 1 or idx > len(candidates):
                send_text(chat_id, t("msg.index_out_of_range", max=len(candidates)))
                answer_callback_query(cb_id, t("callback.index_out_of_range"), show_alert=True)
                return
            target = candidates[idx - 1]
            clear_workspace_candidates(chat_id, user_id)
            if is_risky_workspace(target):
                send_text(chat_id, t("msg.reject_risky_dir", path=str(target)))
                answer_callback_query(cb_id, t("callback.risky_dir"), show_alert=True)
                return
            try:
                from workspace_registry import add_workspace as _add_ws_fuzzy
                ws = _add_ws_fuzzy(target, label=target.name, created_by=user_id)
                send_text(
                    chat_id,
                    t("msg.workspace_added", id=ws["id"], label=ws["label"], path=ws["path"], default=t("msg.yes") if ws.get("is_default") else t("msg.no")),
                    reply_markup=back_to_menu_keyboard(),
                )
                answer_callback_query(cb_id, t("callback.added"))
            except ValueError as exc:
                send_text(chat_id, t("msg.add_failed", err=str(exc)))
                answer_callback_query(cb_id, t("callback.add_failed"), show_alert=True)
            return

        # ---- Search root remove callbacks ----
        if data.startswith("sr_remove:"):
            idx_str = data.split(":", 1)[1].strip()
            try:
                idx = int(idx_str)
            except ValueError:
                answer_callback_query(cb_id, t("callback.invalid_index"), show_alert=True)
                return
            ok, msg = remove_workspace_search_root(idx, changed_by=user_id)
            if ok:
                roots = get_workspace_search_roots()
                send_text(
                    chat_id,
                    t("msg.search_root_deleted", path=msg),
                    reply_markup=search_roots_keyboard(roots),
                )
                answer_callback_query(cb_id, t("callback.deleted"))
            else:
                send_text(chat_id, t("msg.search_root_delete_failed", msg=msg))
                answer_callback_query(cb_id, t("callback.delete_failed"), show_alert=True)
            return

        # ---- Task management callbacks ----
        if data.startswith("task_detail:"):
            _handle_task_detail_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("stage_detail:"):
            _handle_stage_detail_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("task_cancel:"):
            ref = data.split(":", 1)[1].strip()
            send_text(
                chat_id,
                t("msg.confirm_cancel_task", ref=ref),
                reply_markup=confirm_cancel_keyboard("task_cancel", ref),
            )
            answer_callback_query(cb_id, t("callback.confirm_cancel"))
            return
        if data.startswith("task_delete:"):
            ref = data.split(":", 1)[1].strip()
            send_text(
                chat_id,
                t("msg.confirm_delete_task", ref=ref),
                reply_markup=confirm_cancel_keyboard("task_delete", ref),
            )
            answer_callback_query(cb_id, t("callback.confirm_delete"))
            return
        if data.startswith("task_doc:"):
            _handle_task_doc_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("task_summary:"):
            _handle_task_summary_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("task_log:"):
            _handle_task_log_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("archive_detail:"):
            _handle_archive_detail_callback(cb_id, data, chat_id, user_id)
            return
        if data.startswith("archive_delete:"):
            ref = data.split(":", 1)[1].strip()
            send_text(
                chat_id,
                t("msg.confirm_delete_archive", ref=ref),
                reply_markup=confirm_cancel_keyboard("archive_delete", ref),
            )
            answer_callback_query(cb_id, t("callback.confirm_delete"))
            return
        if data.startswith("tasks_page:"):
            _handle_tasks_page_callback(cb_id, data, chat_id, user_id)
            return

        # ---- Confirm callbacks (destructive actions) ----
        if data.startswith("confirm:"):
            _handle_confirm_callback(cb_id, data, chat_id, user_id)
            return

        answer_callback_query(cb_id, t("callback.unknown_button"))
    except Exception as exc:
        answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)
        send_text(chat_id, t("callback.button_failed", err=str(exc)[:500]))


def _handle_menu_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle all menu:* callback queries."""
    action = data.split(":", 1)[1].strip()

    # -- Return to main menu --
    if action == "main":
        clear_pending_action(chat_id, user_id)
        active_workspace = resolve_active_workspace()
        auth_ready = t("msg.enabled") if get_auth_state() else t("msg.not_initialized")
        backend = get_agent_backend()
        model = get_claude_model() or t("msg.not_set")
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
        answer_callback_query(cb_id, t("callback.main_menu"))
        return

    # -- Cancel pending action --
    if action == "cancel":
        clear_pending_action(chat_id, user_id)
        send_text(
            chat_id,
            t("msg.cancelled_op"),
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.cancelled"))
        return

    # -- Sub-menu: System Settings --
    if action == "sub_system":
        backend = get_agent_backend()
        model = get_claude_model() or t("msg.not_set")
        provider = get_model_provider() or t("msg.not_set")
        send_text(
            chat_id,
            SUBMENU_TEXTS["system"].format(
                backend=backend,
                model=model,
                provider=provider,
            ),
            reply_markup=system_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.system_settings"))
        return

    # -- Sub-menu: Archive Management --
    if action == "sub_archive":
        send_text(
            chat_id,
            SUBMENU_TEXTS["archive"],
            reply_markup=archive_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("submenu.archive_mgmt"))
        return

    # -- Sub-menu: Task Management --
    if action == "sub_task_mgmt":
        active_count = len(list_active_tasks(chat_id=chat_id))
        send_text(
            chat_id,
            SUBMENU_TEXTS["task_mgmt"].format(active_count=active_count),
            reply_markup=task_mgmt_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("submenu.task_mgmt_label"))
        return

    # -- Task Management: status-filtered lists --
    if action.startswith("tasks_"):
        _handle_task_status_menu(cb_id, action, chat_id, user_id)
        return

    # -- Sub-menu: Operations --
    if action == "sub_ops":
        send_text(
            chat_id,
            SUBMENU_TEXTS["ops"],
            reply_markup=ops_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Sub-menu: Security --
    if action == "sub_security":
        send_text(
            chat_id,
            SUBMENU_TEXTS["security"],
            reply_markup=security_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Sub-menu: Skills Management --
    if action == "sub_skills":
        send_text(
            chat_id,
            SUBMENU_TEXTS["skills"],
            reply_markup=skills_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Skill: English Practice Toggle --
    if action == "skill_eng_practice_toggle":
        current = get_skill_english_practice()
        new_val = not current
        set_skill_english_practice(new_val, changed_by=user_id)
        msg = t("callback.eng_practice_enabled") if new_val else t("callback.eng_practice_disabled")
        send_text(
            chat_id,
            msg,
            reply_markup=skills_menu_keyboard(),
        )
        answer_callback_query(cb_id, msg)
        return

    # -- English Practice Confirm: correct → create task --
    if action == "eng_confirm_correct":
        pending = get_pending_action(chat_id, user_id)
        if not pending or pending.get("action") != "eng_practice_confirm":
            answer_callback_query(cb_id, t("callback.expired"), show_alert=True)
            return
        ctx = pending.get("context") or {}
        corrected = ctx.get("corrected_text", ctx.get("original_text", ""))
        ws_id = ctx.get("ws_id", "")
        ws_label = ctx.get("ws_label", "")
        if ws_id:
            task_id = create_task_for_workspace(chat_id, user_id, corrected, ws_id, ws_label)
        else:
            task_id = create_task(chat_id, user_id, "/task {}".format(corrected))
        task = load_json(task_file("pending", task_id))
        task_code = task.get("task_code", "-")
        if ws_id:
            send_text(chat_id, t("msg.task_created_ws", code=task_code, task_id=task_id, ws=ws_label, text=corrected[:200]),
                      reply_markup=task_inline_keyboard(task_code))
        else:
            send_text(chat_id, t("msg.task_created_simple", code=task_code, task_id=task_id, text=corrected[:200]),
                      reply_markup=task_inline_keyboard(task_code))
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- English Practice Confirm: switch to Chinese input --
    if action == "eng_confirm_chinese":
        pending = get_pending_action(chat_id, user_id)
        if not pending or pending.get("action") != "eng_practice_confirm":
            answer_callback_query(cb_id, t("callback.expired"), show_alert=True)
            return
        ctx = pending.get("context") or {}
        ws_id = ctx.get("ws_id", "")
        ws_label = ctx.get("ws_label", "")
        if ws_id:
            set_pending_action(chat_id, user_id, "new_task_with_workspace", {"ws_id": ws_id, "ws_label": ws_label})
            send_text(chat_id, PENDING_PROMPTS["new_task_with_workspace"].format(ws_label=ws_label),
                      reply_markup=cancel_keyboard())
        else:
            set_pending_action(chat_id, user_id, "new_task")
            send_text(chat_id, str(PENDING_PROMPTS["new_task"]), reply_markup=cancel_keyboard())
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- English Practice Confirm: retry English input --
    if action == "eng_confirm_retry":
        pending = get_pending_action(chat_id, user_id)
        if not pending or pending.get("action") != "eng_practice_confirm":
            answer_callback_query(cb_id, t("callback.expired"), show_alert=True)
            return
        ctx = pending.get("context") or {}
        ws_id = ctx.get("ws_id", "")
        ws_label = ctx.get("ws_label", "")
        set_pending_action(chat_id, user_id, "eng_practice_input", {"ws_id": ws_id, "ws_label": ws_label})
        send_text(chat_id, str(PENDING_PROMPTS["eng_practice_input"]), reply_markup=cancel_keyboard())
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Sub-menu: Workspace Management --
    if action == "sub_workspace":
        send_text(
            chat_id,
            SUBMENU_TEXTS["workspace"],
            reply_markup=workspace_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Switch Language --
    if action == "switch_language":
        send_text(
            chat_id,
            t("msg.select_language"),
            reply_markup=language_select_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    if action in ("lang_zh", "lang_en"):
        lang = action.split("_", 1)[1]
        set_config_language(lang, changed_by=user_id)
        send_text(
            chat_id,
            t("msg.language_switched"),
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- New Task: show workspace selection if multiple workspaces --
    if action == "new_task":
        from workspace_registry import ensure_current_workspace_registered, list_workspaces as _list_ws_for_task
        ensure_current_workspace_registered()
        workspaces = _list_ws_for_task()
        eng_on = get_skill_english_practice()
        eng_status = t("msg.eng_practice_status_on") if eng_on else t("msg.eng_practice_status_off")
        if len(workspaces) > 1:
            send_text(
                chat_id,
                t("prompt.new_task_ws_select") + eng_status,
                reply_markup=workspace_select_keyboard(workspaces, "ws_task_select"),
            )
            answer_callback_query(cb_id, t("callback.submitted"))
        else:
            if eng_on:
                set_pending_action(chat_id, user_id, "eng_practice_input")
                send_text(chat_id, str(PENDING_PROMPTS["eng_practice_input"]),
                          reply_markup=cancel_keyboard())
            else:
                set_pending_action(chat_id, user_id, "new_task")
                send_text(
                    chat_id,
                    str(PENDING_PROMPTS["new_task"]) + eng_status,
                    reply_markup=cancel_keyboard(),
            )
            answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Task List: execute directly --
    if action == "task_list":
        handle_command(chat_id, user_id, "/status")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Clear Task List: confirm before clearing (keeps running tasks) --
    if action == "clear_tasks":
        active = list_active_tasks(chat_id=chat_id)
        if not active:
            send_text(chat_id, t("msg.no_active_tasks"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.submitted"))
            return
        running = [tsk for tsk in active if str(tsk.get("status") or "").strip().lower() == "processing"]
        clearable = len(active) - len(running)
        if clearable <= 0:
            send_text(chat_id, t("msg.all_tasks_running"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.submitted"))
            return
        msg = t("msg.confirm_clear", count=clearable)
        if running:
            msg += "\n" + t("msg.running_kept", count=len(running))
        send_text(
            chat_id,
            msg,
            reply_markup=confirm_cancel_keyboard("clear_tasks"),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Screenshot: prompt for description --
    if action == "screenshot":
        set_pending_action(chat_id, user_id, "screenshot")
        send_text(
            chat_id,
            PENDING_PROMPTS["screenshot"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- System Info: execute directly --
    if action == "info":
        handle_command(chat_id, user_id, "/info")
        send_text(chat_id, "", reply_markup=back_to_menu_keyboard()) if False else None
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Project Summary --
    if action == "summary":
        _do_summary_command(chat_id, user_id)
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Switch Backend: show selection keyboard --
    if action == "switch_backend":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        current = get_agent_backend()
        send_text(
            chat_id,
            t("msg.current_backend_select", backend=current),
            reply_markup=backend_select_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Switch Model: show model list --
    if action == "switch_model":
        handle_command(chat_id, user_id, "/switch_model")
        answer_callback_query(cb_id, t("callback.select_model"))
        return

    # -- Model List: show all models with status --
    if action in ("model_list", "model_list_refresh"):
        force = action == "model_list_refresh"
        models = get_available_models(force_refresh=force)
        text = format_model_list_text(models)
        current_default = get_claude_model()
        # Telegram message limit: 4096 chars
        header = t("msg.model_list_header")
        footer = t("msg.model_list_footer", default_model=current_default or t("msg.not_set"))
        full_text = header + text + footer
        if len(full_text) > 4000:
            full_text = full_text[:3990] + "\n..."
        send_text(
            chat_id,
            full_text,
            reply_markup=model_list_keyboard(models, current_default),
        )
        answer_callback_query(cb_id, t("msg.refreshed") if force else t("msg.model_list_label"))
        return

    # -- Pipeline Config: show preset selection --
    if action == "pipeline_config":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        preset_lines = []
        for k, v in PIPELINE_PRESETS.items():
            if k == "role_pipeline":
                display_stages = [dict(s) for s in v]
                role_stages = get_role_pipeline_stages()
                role_config = {s["name"]: s for s in role_stages if "name" in s}
                for stage in display_stages:
                    sname = stage.get("name", "")
                    if sname in role_config:
                        rc = role_config[sname]
                        if rc.get("model"):
                            stage["model"] = rc["model"]
                            stage["provider"] = rc.get("provider", "")
                preset_lines.append(t("msg.preset_arrow", key=k, value=format_role_pipeline_stages(display_stages)))
            else:
                preset_lines.append(t("msg.preset_arrow_inline", key=k, value=format_pipeline_stages(v)))
        preset_info = "\n".join(preset_lines)
        send_text(
            chat_id,
            t("msg.pipeline_config_panel", preset_info=preset_info),
            reply_markup=pipeline_preset_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Pipeline Stage Overview: return to overview from model selection --
    if action == "pipeline_stage_overview":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        pending = peek_pending_action(chat_id, user_id)
        if not pending or pending.get("action") != "pipeline_configure":
            # Pending action lost — fall back to preset selection
            send_text(
                chat_id,
                t("msg.config_expired"),
                reply_markup=pipeline_preset_keyboard(),
            )
            answer_callback_query(cb_id, t("callback.please_reselect"))
            return
        ctx = pending.get("context", {})
        stages = ctx.get("stages", [])
        preset_name = ctx.get("preset_name", "")
        preset_display = {
            "plan_code_verify": "plan + code + verify",
            "plan_code": "plan + code",
            "code_verify": "code + verify",
            "claude_codex": "claude + codex",
            "role_pipeline": t("msg.role_pipeline_config"),
        }.get(preset_name, preset_name)
        send_text(
            chat_id,
            t("msg.stage_config_overview", pipeline=preset_display),
            reply_markup=pipeline_stage_overview_keyboard(stages),
        )
        answer_callback_query(cb_id, t("msg.stage_overview_label"))
        return

    # -- Role Pipeline Config: show role config keyboard --
    if action == "role_pipeline_config":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        stages = get_role_pipeline_stages()
        send_text(
            chat_id,
            t("msg.role_pipeline_view", title=t("msg.role_pipeline_config"), config=t("msg.role_config_current", config=format_role_pipeline_stages(stages))),
            reply_markup=role_pipeline_config_keyboard(stages),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Pipeline Config Custom: prompt for text --
    if action == "pipeline_config_custom":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "pipeline_config_custom")
        send_text(
            chat_id,
            PENDING_PROMPTS["pipeline_config_custom"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Pipeline Status: execute directly --
    if action == "pipeline_status":
        handle_command(chat_id, user_id, "/show_pipeline")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Archive Overview: execute directly --
    if action == "archive":
        handle_command(chat_id, user_id, "/archive")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Archive Search: prompt for keyword --
    if action == "archive_search":
        set_pending_action(chat_id, user_id, "archive_search")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_search"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Archive Show: prompt for ID --
    if action == "archive_show":
        set_pending_action(chat_id, user_id, "archive_show")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_show"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Archive Log: prompt for keyword --
    if action == "archive_log":
        set_pending_action(chat_id, user_id, "archive_log")
        send_text(
            chat_id,
            PENDING_PROMPTS["archive_log"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Mgr Restart: prompt for OTP --
    if action == "mgr_restart":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "mgr_restart")
        send_text(
            chat_id,
            PENDING_PROMPTS["mgr_restart"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.enter_otp"))
        return

    # -- Mgr Reinit (Self-Update): prompt for OTP --
    if action == "mgr_reinit":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "mgr_reinit")
        send_text(
            chat_id,
            PENDING_PROMPTS["mgr_reinit"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.enter_otp"))
        return

    # -- Ops Restart: prompt for OTP --
    if action == "ops_restart":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "ops_restart")
        send_text(
            chat_id,
            PENDING_PROMPTS["ops_restart"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.enter_otp"))
        return

    # -- Mgr Status: execute directly --
    if action == "mgr_status":
        handle_command(chat_id, user_id, "/mgr_status")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Auth Init: execute directly --
    if action == "auth_init":
        handle_command(chat_id, user_id, "/auth_init")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Auth Status: execute directly --
    if action == "auth_status":
        handle_command(chat_id, user_id, "/auth_status")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Whoami: execute directly --
    if action == "whoami":
        handle_command(chat_id, user_id, "/ops_whoami")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Auth Debug: prompt for OTP --
    if action == "auth_debug":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "auth_debug")
        send_text(
            chat_id,
            PENDING_PROMPTS["auth_debug"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.enter_otp"))
        return

    # -- Set Workspace: prompt for path + OTP --
    if action == "set_workspace":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "set_workspace")
        send_text(
            chat_id,
            PENDING_PROMPTS["set_workspace"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Reset Workspace: prompt for OTP --
    if action == "reset_workspace":
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
            return
        set_pending_action(chat_id, user_id, "reset_workspace")
        send_text(
            chat_id,
            PENDING_PROMPTS["reset_workspace"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.enter_otp"))
        return

    # -- Workspace List: execute directly --
    if action == "workspace_list":
        handle_command(chat_id, user_id, "/workspace_list")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Workspace Add: prompt for path --
    if action == "workspace_add":
        set_pending_action(chat_id, user_id, "workspace_add")
        send_text(
            chat_id,
            PENDING_PROMPTS["workspace_add"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    # -- Workspace Remove: show selection or prompt --
    if action == "workspace_remove":
        from workspace_registry import list_workspaces as _list_ws_rm
        workspaces = _list_ws_rm(include_inactive=True)
        if not workspaces:
            send_text(chat_id, t("msg.no_workspaces_short"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("msg.no_workspaces_label"))
            return
        send_text(
            chat_id,
            t("msg.ws_remove_panel"),
            reply_markup=workspace_select_keyboard(workspaces, "ws_remove"),
        )
        answer_callback_query(cb_id, t("msg.select_ws_remove"))
        return

    # -- Workspace Set Default: show selection --
    if action == "workspace_set_default":
        from workspace_registry import list_workspaces as _list_ws_def
        workspaces = _list_ws_def()
        if not workspaces:
            send_text(chat_id, t("msg.no_workspaces_short"), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("msg.no_workspaces_label"))
            return
        send_text(
            chat_id,
            t("msg.ws_set_default_panel"),
            reply_markup=workspace_select_keyboard(workspaces, "ws_default"),
        )
        answer_callback_query(cb_id, t("msg.select_ws_default"))
        return

    # -- Workspace Search Roots: show current roots with management UI --
    if action == "workspace_search_roots":
        roots = get_workspace_search_roots()
        if roots:
            items_lines = []
            for idx, r in enumerate(roots, 1):
                items_lines.append("{}. {}".format(idx, r))
            text = t("msg.search_roots_has_items", items="\n".join(items_lines))
        else:
            text = t("msg.search_roots_empty")
        send_text(chat_id, text, reply_markup=search_roots_keyboard(roots))
        answer_callback_query(cb_id, t("msg.search_roots_label"))
        return

    # -- Search Root Add: prompt for path --
    if action == "search_root_add":
        set_pending_action(chat_id, user_id, "search_root_add")
        send_text(
            chat_id,
            PENDING_PROMPTS["search_root_add"],
            reply_markup=cancel_keyboard(),
        )
        answer_callback_query(cb_id, t("msg.enter_path"))
        return

    # -- Workspace Queue Status: show all queues --
    if action == "workspace_queue_status":
        all_queues = list_all_queues()
        if not all_queues or all(len(q) == 0 for q in all_queues.values()):
            send_text(
                chat_id,
                t("msg.no_queued_tasks_all"),
                reply_markup=back_to_menu_keyboard(),
            )
            answer_callback_query(cb_id, t("msg.no_queued_tasks"))
            return
        from workspace_registry import get_workspace as _get_ws_q
        lines = [t("msg.ws_queue_header")]
        for ws_id, tasks in all_queues.items():
            if not tasks:
                continue
            ws = _get_ws_q(ws_id)
            ws_label = ws.get("label", ws_id) if ws else ws_id
            lines.append(t("msg.ws_queue_section", label=ws_label, count=len(tasks)))
            for i, tsk in enumerate(tasks, 1):
                lines.append("  {}. [{}] {}".format(
                    i,
                    tsk.get("task_code", "-"),
                    (tsk.get("text", "") or "")[:60],
                ))
        send_text(chat_id, "\n".join(lines), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.queue_status_label"))
        return

    # -- Dispatch Status: execute directly --
    if action == "dispatch_status":
        handle_command(chat_id, user_id, "/dispatch_status")
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    answer_callback_query(cb_id, t("callback.unknown_button"))


def _handle_confirm_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle confirm:* callback queries for destructive actions."""
    # data format: "confirm:<action>" or "confirm:<action>:<context>"
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "clear_tasks":
        removed = clear_active_tasks(chat_id)
        send_text(
            chat_id,
            t("msg.tasks_cleared", count=removed),
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.submitted"))
        return

    if action == "task_cancel":
        ctx = parts[2] if len(parts) > 2 else ""
        if not ctx:
            answer_callback_query(cb_id, t("msg.invalid_operation"), show_alert=True)
            return
        found = find_task(ctx)
        if not found:
            send_text(chat_id, t("msg.task_not_found", ref=ctx), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("msg.task_not_found_short"))
            return
        current_status = str(found.get("status") or "").strip().lower()
        st = task_status_snapshot(str(found.get("task_id") or ""))
        if st:
            current_status = str(st.get("status") or current_status).strip().lower()
        if current_status != "pending":
            send_text(chat_id, t("msg.only_cancel_pending", status=current_status), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("msg.cannot_cancel"), show_alert=True)
            return
        task_id = str(found.get("task_id") or "")
        # Remove pending file
        pending_path = task_file("pending", task_id)
        if pending_path.exists():
            pending_path.unlink()
        # Update status
        update_task_runtime(found, status="cancelled", stage="results")
        mark_task_finished(found, status="cancelled", stage="results", error=t("msg.user_cancelled"))
        send_text(
            chat_id,
            t("msg.task_cancelled", ref=ctx),
            reply_markup=task_mgmt_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.cancelled"))
        return

    if action == "task_delete":
        ctx = parts[2] if len(parts) > 2 else ""
        if not ctx:
            answer_callback_query(cb_id, t("msg.invalid_operation"), show_alert=True)
            return
        task_id = resolve_task_ref(ctx) or ctx
        # Remove from active list
        state = load_runtime_state()
        active = state.get("active") or {}
        if task_id in active:
            del active[task_id]
            state["active"] = active
            from task_state import save_runtime_state
            save_runtime_state(state)
        # Remove task files
        for stage_name in ("pending", "processing", "results"):
            p = task_file(stage_name, task_id)
            if p.exists():
                p.unlink()
        send_text(
            chat_id,
            t("msg.task_deleted", ref=ctx),
            reply_markup=task_mgmt_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("callback.deleted"))
        return

    if action == "archive_delete":
        ctx = parts[2] if len(parts) > 2 else ""
        if not ctx:
            answer_callback_query(cb_id, t("msg.invalid_operation"), show_alert=True)
            return
        removed = _remove_archive_entry(ctx)
        if removed:
            send_text(
                chat_id,
                t("msg.archive_deleted", ref=ctx),
                reply_markup=task_mgmt_menu_keyboard(),
            )
            answer_callback_query(cb_id, t("callback.deleted"))
        else:
            send_text(chat_id, t("msg.archive_not_found", ref=ctx), reply_markup=back_to_menu_keyboard())
            answer_callback_query(cb_id, t("callback.ws_not_found"), show_alert=True)
        return

    answer_callback_query(cb_id, t("callback.unknown_button"))


# ---------------------------------------------------------------------------
# Task Management Helpers
# ---------------------------------------------------------------------------

def _collect_tasks_by_status(chat_id: int, status_key: str) -> List[Dict]:
    """Collect tasks matching the given status key for a chat.

    For 'accepted' includes both 'accepted' and 'completed'.
    For 'failed' includes both 'failed' and 'timeout'.
    For 'archived' reads from archive index.
    """
    if status_key == "archived":
        from task_state import _read_archive_index
        entries = list(reversed(_read_archive_index()))
        # Ensure critical fields have fallback values for old/incomplete entries
        for entry in entries:
            if not entry.get("archive_id"):
                entry["archive_id"] = str(entry.get("task_id") or entry.get("task_code") or "unknown")
            if not entry.get("task_code"):
                entry["task_code"] = str(entry.get("archive_id") or entry.get("task_id") or "-")[:10]
        return entries

    active = list_active_tasks(chat_id=chat_id)
    result: List[Dict] = []
    match_statuses: set
    if status_key == "accepted":
        match_statuses = {"accepted", "completed"}
    elif status_key == "failed":
        match_statuses = {"failed", "timeout"}
    else:
        match_statuses = {status_key}

    for item in active:
        task_id = str(item.get("task_id") or "")
        # Get latest status from task_state
        st = task_status_snapshot(task_id) if task_id else None
        current_status = str((st or item).get("status") or item.get("status") or "").strip().lower()
        if current_status in match_statuses:
            enriched = dict(item)
            if st:
                enriched["status"] = st.get("status", enriched.get("status"))
                enriched["task_code"] = st.get("task_code", enriched.get("task_code", "-"))
            result.append(enriched)
    return result


def _count_tasks_by_status(chat_id: int) -> Dict[str, int]:
    """Count tasks in each status category."""
    active = list_active_tasks(chat_id=chat_id)
    counts: Dict[str, int] = {
        "pending": 0,
        "processing": 0,
        "pending_acceptance": 0,
        "rejected": 0,
        "accepted": 0,
        "failed": 0,
    }
    for item in active:
        task_id = str(item.get("task_id") or "")
        st = task_status_snapshot(task_id) if task_id else None
        current_status = str((st or item).get("status") or item.get("status") or "").strip().lower()
        if current_status in ("accepted", "completed"):
            counts["accepted"] += 1
        elif current_status in ("failed", "timeout"):
            counts["failed"] += 1
        elif current_status in counts:
            counts[current_status] += 1
    # Count archived
    from task_state import _read_archive_index
    counts["archived"] = len(_read_archive_index())
    return counts


def _handle_task_status_menu(cb_id: str, action: str, chat_id: int, user_id: int) -> None:
    """Handle menu:tasks_* callbacks for status-filtered task lists."""
    status_map = {
        "tasks_pending": "pending",
        "tasks_processing": "processing",
        "tasks_pending_acceptance": "pending_acceptance",
        "tasks_rejected": "rejected",
        "tasks_accepted": "accepted",
        "tasks_failed": "failed",
        "tasks_archived": "archived",
        "tasks_overview": "overview",
    }
    status_key = status_map.get(action, "")
    if not status_key:
        answer_callback_query(cb_id, t("callback.unknown_button"))
        return

    if status_key == "overview":
        counts = _count_tasks_by_status(chat_id)
        total_active = sum(v for k, v in counts.items() if k != "archived")
        send_text(
            chat_id,
            t("msg.task_overview_panel", total=total_active),
            reply_markup=tasks_overview_keyboard(counts),
        )
        answer_callback_query(cb_id, t("msg.task_overview_label"))
        return

    tasks = _collect_tasks_by_status(chat_id, status_key)
    label = TASK_STATUS_LABELS.get(status_key, status_key)
    empty_label = TASK_STATUS_EMPTY_LABELS.get(status_key, status_key)

    if not tasks:
        send_text(
            chat_id,
            t("msg.no_tasks_in_category", label=empty_label),
            reply_markup=task_status_list_keyboard([], status_key),
        )
        answer_callback_query(cb_id, t("msg.no_tasks_label"))
        return

    header = t("msg.task_list_header", label=label, count=len(tasks))
    send_text(
        chat_id,
        header,
        reply_markup=task_status_list_keyboard(tasks, status_key, page=0),
    )
    answer_callback_query(cb_id, label[:20])


def _handle_tasks_page_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle tasks_page:{status}:{page} pagination callbacks."""
    parts = data.split(":", 2)
    if len(parts) < 3:
        answer_callback_query(cb_id, t("msg.invalid_page"))
        return
    status_key = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        answer_callback_query(cb_id, t("msg.invalid_page_num"))
        return

    tasks = _collect_tasks_by_status(chat_id, status_key)
    label = TASK_STATUS_LABELS.get(status_key, status_key)

    if not tasks:
        send_text(
            chat_id,
            t("msg.no_tasks_current"),
            reply_markup=task_status_list_keyboard([], status_key),
        )
        answer_callback_query(cb_id, t("msg.no_tasks_label"))
        return

    send_text(
        chat_id,
        t("msg.task_list_paged", label=label, count=len(tasks), page=page + 1),
        reply_markup=task_status_list_keyboard(tasks, status_key, page=page),
    )
    answer_callback_query(cb_id, t("msg.page_num", num=page + 1))


def _detail_status_emoji(status: str) -> str:
    """Return status emoji for task detail page header."""
    mapping = {
        "pending": "\u23f3",
        "processing": "\u2699\ufe0f",
        "pending_acceptance": "\U0001f4cb",
        "accepted": "\u2705",
        "completed": "\u2705",
        "rejected": "\u274c",
        "failed": "\U0001f4a5",
        "timeout": "\U0001f4a5",
    }
    return mapping.get(str(status or "").strip().lower(), "\U0001f4cb")


def _handle_task_detail_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle task_detail:{task_code} callback to show task detail page."""
    task_code = data.split(":", 1)[1].strip()
    found = find_task(task_code)
    if not found:
        # Try status snapshot
        resolved_id = resolve_task_ref(task_code)
        st = task_status_snapshot(resolved_id) if resolved_id else None
        if st:
            status = str(st.get("status") or "unknown").strip().lower()
            emoji = _detail_status_emoji(status)
            text_preview = str(st.get("text") or "").strip()[:500] or t("msg.no_description")
            iteration = int(st.get("attempt") or 0)
            detail = t("msg.task_detail_snapshot",
                emoji=emoji,
                code=task_code,
                status=status_tag(status),
                created=st.get("created_at", ""),
                iteration=iteration,
                text=text_preview,
                summary=str(st.get("summary") or "").strip()[:500] or t("msg.none_short"),
            )
            send_text(chat_id, detail, reply_markup=task_detail_keyboard(task_code, status))
            answer_callback_query(cb_id, t("msg.task_detail_label"))
            return
        send_text(chat_id, t("msg.task_not_found", ref=task_code), reply_markup=task_mgmt_menu_keyboard())
        answer_callback_query(cb_id, t("msg.task_not_found_short"))
        return

    found = merge_task_with_status(found)
    status = str(found.get("status") or "unknown").strip().lower()
    emoji = _detail_status_emoji(status)
    st = found.get("_status_snapshot") if isinstance(found.get("_status_snapshot"), dict) else {}
    text_preview = str(found.get("text") or "").strip()[:500] or t("msg.no_description")
    summary = str(build_status_summary(found)).strip()[:500]
    iteration = int(st.get("attempt") or found.get("attempt") or 0)
    acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}

    # Read summary from summary file if available
    task_id = str(found.get("task_id") or "")
    summary_file = tasks_root() / "logs" / (task_id + ".summary.txt")
    if summary_file.exists():
        try:
            file_summary = summary_file.read_text(encoding="utf-8").strip()[:500]
            if file_summary:
                summary = file_summary
        except Exception:
            pass

    detail = t("msg.task_detail_full",
        emoji=emoji,
        code=found.get("task_code", task_code),
        status=status_tag(status),
        created=found.get("created_at") or st.get("created_at", ""),
        iteration=iteration,
        text=text_preview,
        summary=summary or t("msg.none_short"),
    )
    if acceptance.get("reason"):
        detail += t("msg.rejection_reason_append", reason=str(acceptance["reason"])[:500])

    # Show git checkpoint for pending_acceptance tasks
    ckpt = str(found.get("_git_checkpoint") or "").strip()
    if ckpt and status == "pending_acceptance":
        detail += t("msg.git_checkpoint_append", ckpt=ckpt[:12])

    # Append pipeline stage execution info if available
    stage_summary = format_stage_execution_summary(found)
    if stage_summary:
        detail += t("msg.separator_line") + stage_summary

    # Determine if this is a pipeline task for keyboard
    is_pipeline = bool((found.get("executor") or {}).get("action") == "pipeline")
    send_text(chat_id, detail, reply_markup=task_detail_keyboard(found.get("task_code", task_code), status, is_pipeline=is_pipeline))
    answer_callback_query(cb_id, t("msg.task_detail_label"))


def _handle_stage_detail_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle stage_detail:{task_code} callback to show per-stage output."""
    task_code = data.split(":", 1)[1].strip()
    task_id = resolve_task_ref(task_code) or task_code

    # Read run log from logs/{task_id}.run.json
    run_log_path = tasks_root() / "logs" / (task_id + ".run.json")
    if not run_log_path.exists():
        send_text(chat_id, t("msg.no_stage_records"), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.no_records"))
        return

    try:
        run_data = load_json(run_log_path)
    except Exception:
        send_text(chat_id, t("msg.cannot_read_stage"), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.read_failed"))
        return

    stage_details = run_data.get("stage_details")
    if not stage_details:
        send_text(chat_id, t("msg.no_stage_records_short"), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.no_records"))
        return

    lines = [t("msg.stage_detail_header", code=task_code), "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"]
    for sd in stage_details:
        stage_name = sd.get("stage", "?")
        role_def = ROLE_DEFINITIONS.get(stage_name, {})
        emoji = role_def.get("emoji", "")
        label = role_def.get("label", stage_name)
        idx = sd.get("stage_index", "?")
        elapsed = sd.get("elapsed_ms")
        noop = sd.get("noop_reason", "")
        rc = sd.get("returncode")
        time_str = " ({:.1f}s)".format(elapsed / 1000.0) if elapsed else ""

        # Model display with fallback for backward compatibility
        model = sd.get("model", "")
        provider = sd.get("provider", "")
        if model and model != "(default)":
            from config import _provider_tag
            tag = _provider_tag(provider)
            model_display = "{} {}".format(model, tag).rstrip()
        else:
            model_display = sd.get("backend", t("msg.unknown"))

        status_icon = t("status.icon_fail") if noop or (rc and rc != 0) else t("status.icon_pass")
        lines.append(t("msg.stage_detail_line", emoji=emoji, idx=idx, label=label, status_icon=status_icon, model_display=model_display, time_str=time_str))
        if noop:
            lines.append("  noop: {}".format(noop[:100]))
        # Show output preview (last_message preferred, fallback stdout)
        output = (sd.get("last_message") or sd.get("stdout") or "").strip()
        if output:
            preview = output[:800]
            if len(output) > 800:
                preview += "\n" + t("msg.truncated")
            lines.append(preview)
        else:
            lines.append("  " + t("msg.no_output"))

    text = "\n".join(lines)
    # Telegram message limit: 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n" + t("msg.truncated")
    send_text(chat_id, text, reply_markup=back_to_menu_keyboard())
    answer_callback_query(cb_id, t("msg.stage_detail_label"))


def _handle_task_doc_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle task_doc:{task_code} callback to display task document."""
    ref = data.split(":", 1)[1].strip()

    # Try as archive entry first
    archived = find_archive_entry(ref)
    if archived:
        result_file = str(archived.get("result_file") or "")
        doc_text = str(archived.get("summary") or "").strip()
        if result_file:
            p = Path(result_file)
            if p.exists():
                try:
                    content = p.read_text(encoding="utf-8")[:3000]
                    doc_text = content
                except Exception:
                    pass
        if not doc_text:
            doc_text = t("msg.no_doc_content")
        send_text(
            chat_id,
            t("msg.task_doc_panel", ref=ref, content=doc_text[:3500]),
            reply_markup=back_to_menu_keyboard(),
        )
        answer_callback_query(cb_id, t("msg.view_doc_label"))
        return

    # Try as active task
    found = find_task(ref)
    if not found:
        resolved_id = resolve_task_ref(ref)
        st = task_status_snapshot(resolved_id) if resolved_id else None
        if st:
            doc_text = str(st.get("summary") or st.get("text") or st.get("error") or "").strip()[:3000]
            if not doc_text:
                doc_text = t("msg.no_doc_content")
            send_text(
                chat_id,
                t("msg.task_doc_panel", ref=ref, content=doc_text[:3500]),
                reply_markup=back_to_menu_keyboard(),
            )
            answer_callback_query(cb_id, t("msg.view_doc_label"))
            return
        send_text(chat_id, t("msg.task_not_found", ref=ref), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.task_not_found_short"))
        return

    found = merge_task_with_status(found)
    acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
    doc_file = str(acceptance.get("doc_file") or "").strip()
    doc_text = ""

    # Try acceptance doc_file
    if doc_file:
        p = Path(doc_file)
        if p.exists():
            try:
                doc_text = p.read_text(encoding="utf-8")[:3000]
            except Exception:
                pass

    # Fallback to result_file
    if not doc_text:
        st = found.get("_status_snapshot") if isinstance(found.get("_status_snapshot"), dict) else {}
        result_file = str(st.get("result_file") or found.get("result_file") or "").strip()
        if result_file:
            p = Path(result_file)
            if p.exists():
                try:
                    doc_text = p.read_text(encoding="utf-8")[:3000]
                except Exception:
                    pass

    # Fallback to summary/error
    if not doc_text:
        doc_text = str(build_status_summary(found)).strip()[:3000]
        if acceptance.get("reason"):
            doc_text += t("msg.rejection_reason_doc", reason=str(acceptance["reason"])[:500])
        error = str(found.get("error") or "").strip()
        if error:
            doc_text += t("msg.error_info_doc", err=error[:500])

    if not doc_text:
        doc_text = t("msg.no_doc_content")

    send_text(
        chat_id,
        t("msg.task_doc_panel", ref=found.get("task_code", ref), content=doc_text[:3500]),
        reply_markup=back_to_menu_keyboard(),
    )
    answer_callback_query(cb_id, t("msg.view_doc_label"))


def _handle_task_summary_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle task_summary:{task_code} callback to show task summary."""
    ref = data.split(":", 1)[1].strip()
    task_id = resolve_task_ref(ref) or ref

    # Try reading summary file
    summary_path = tasks_root() / "logs" / (task_id + ".summary.txt")
    summary_text = ""
    if summary_path.exists():
        try:
            summary_text = summary_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    # Fallback: read from run log
    if not summary_text:
        run_log_path = tasks_root() / "logs" / (task_id + ".run.json")
        if run_log_path.exists():
            try:
                run_data = load_json(run_log_path)
                summary_text = str(run_data.get("summary") or "").strip()
            except Exception:
                pass

    if not summary_text:
        summary_text = t("msg.no_summary_info")

    send_text(
        chat_id,
        t("msg.task_summary_panel", ref=ref, content=summary_text[:3500]),
        reply_markup=back_to_menu_keyboard(),
    )
    answer_callback_query(cb_id, t("msg.view_summary_label"))


def _handle_task_log_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle task_log:{task_code} callback to show full run log."""
    ref = data.split(":", 1)[1].strip()
    task_id = resolve_task_ref(ref) or ref

    run_log_path = tasks_root() / "logs" / (task_id + ".run.json")
    if not run_log_path.exists():
        send_text(chat_id, t("msg.no_log_file"), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.no_log"))
        return

    try:
        log_text = run_log_path.read_text(encoding="utf-8")
    except Exception:
        send_text(chat_id, t("msg.cannot_read_log"), reply_markup=back_to_menu_keyboard())
        answer_callback_query(cb_id, t("msg.read_failed"))
        return

    # Try sending as document if too large
    if len(log_text) > 3500:
        try:
            send_document(chat_id, run_log_path, caption=t("msg.task_log_caption", ref=ref))
        except Exception:
            send_text(chat_id, t("msg.task_log_panel", ref=ref, content=log_text[:3500] + "..."),
                      reply_markup=back_to_menu_keyboard())
    else:
        send_text(
            chat_id,
            t("msg.task_log_panel", ref=ref, content=log_text[:3500]),
            reply_markup=back_to_menu_keyboard(),
        )
    answer_callback_query(cb_id, t("msg.view_log_label"))


def _format_iso_time(iso_str: str) -> str:
    """Convert ISO format time to YYYY-MM-DD HH:MM."""
    s = str(iso_str or "").strip()
    if not s:
        return ""
    # Handle common ISO formats: 2024-01-02T03:04:05Z or 2024-01-02T03:04:05.123456Z
    try:
        # Strip trailing Z and microseconds
        clean = s.replace("Z", "").split(".")[0]
        if "T" in clean:
            date_part, time_part = clean.split("T", 1)
            return "{} {}".format(date_part, time_part[:5])
        return clean[:16]
    except Exception:
        return s[:16]


def _handle_archive_detail_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle archive_detail:{archive_id} callback to show archive detail page."""
    archive_ref = data.split(":", 1)[1].strip()
    print("[archive_detail] looking up: {}".format(archive_ref))
    entry = find_archive_entry(archive_ref)
    if not entry:
        print("[archive_detail] not found: {}".format(archive_ref))
        send_text(chat_id, t("msg.archive_not_found", ref=archive_ref), reply_markup=task_mgmt_menu_keyboard())
        answer_callback_query(cb_id, t("callback.ws_not_found"))
        return

    # Read summary from file if available
    task_id = str(entry.get("task_id") or "")
    summary = str(entry.get("summary") or "").strip()
    if task_id:
        summary_path = tasks_root() / "logs" / (task_id + ".summary.txt")
        if summary_path.exists():
            try:
                file_summary = summary_path.read_text(encoding="utf-8").strip()
                if file_summary:
                    summary = file_summary
            except Exception:
                pass

    completed_time = _format_iso_time(entry.get("completed_at") or entry.get("updated_at", ""))

    detail = t("msg.archive_detail_panel",
        code=entry.get("task_code", "-"),
        archive_id=entry.get("archive_id", ""),
        status=status_tag(entry.get("status", "unknown")),
        action=entry.get("action", "unknown"),
        completed=completed_time,
        text=str(entry.get("text") or "").strip()[:500] or t("msg.no_description"),
        summary=summary[:500] or t("msg.none_short"),
    )

    # Build enhanced keyboard with conditional buttons
    kbd_rows: List[List[Dict]] = []
    aid = entry.get("archive_id", archive_ref)
    kbd_rows.append([
        {"text": t("msg.btn_view_archive_detail"), "callback_data": safe_callback_data("task_doc", aid)},
    ])
    # Add log button if run_log_file exists
    run_log_file = str(entry.get("run_log_file") or "").strip()
    if run_log_file and Path(run_log_file).exists():
        task_code = str(entry.get("task_code") or aid)
        kbd_rows.append([
            {"text": t("msg.btn_view_log"), "callback_data": "task_log:{}".format(task_code)},
        ])
    # Add acceptance doc button if exists
    if task_id:
        from task_accept import acceptance_root
        acc_doc = acceptance_root() / (task_id + ".acceptance.md")
        if acc_doc.exists():
            kbd_rows.append([
                {"text": t("msg.btn_view_accept_doc"), "callback_data": safe_callback_data("task_doc", entry.get("task_code", aid))},
            ])
    kbd_rows.append([
        {"text": t("msg.btn_delete_archive"), "callback_data": safe_callback_data("archive_delete", aid)},
    ])
    kbd_rows.append([
        {"text": t("msg.btn_back_list"), "callback_data": "menu:tasks_archived"},
    ])

    send_text(chat_id, detail, reply_markup={"inline_keyboard": kbd_rows})
    answer_callback_query(cb_id, t("msg.archive_detail_label"))


def _remove_archive_entry(archive_id: str) -> bool:
    """Remove an entry from the archive index by archive_id.

    Supports both exact match and prefix match (for truncated callback_data).
    Returns True if found and removed.
    """
    from task_state import _archive_index_file
    import json as _json
    path = _archive_index_file()
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: List[str] = []
    found = False

    def _matches(aid: str) -> bool:
        if aid == archive_id:
            return True
        # Prefix match for truncated IDs
        if archive_id.startswith("arc-") and aid.startswith(archive_id):
            return True
        return False

    for line in lines:
        row = line.strip()
        if not row:
            continue
        try:
            obj = _json.loads(row)
        except Exception:
            new_lines.append(row)
            continue
        if isinstance(obj, dict) and _matches(str(obj.get("archive_id") or "")):
            if not found:
                # Only remove the first (most recent by reverse order) match
                found = True
                # Also delete the archive file
                archive_file = str(obj.get("archive_file") or "")
                if archive_file:
                    af = Path(archive_file)
                    if af.exists():
                        af.unlink()
                continue
        new_lines.append(row)
    if found:
        path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    return found


def _handle_backend_select_callback(cb_id: str, data: str, chat_id: int, user_id: int) -> None:
    """Handle backend_sel:* callback queries."""
    backend = data.split(":", 1)[1].strip()
    if not is_ops_allowed(chat_id, user_id):
        answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)
        return
    handle_command(chat_id, user_id, "/switch_backend {}".format(backend))
    answer_callback_query(cb_id, t("callback.switched", tag="", model=backend))


def handle_pending_action(chat_id: int, user_id: int, text: str) -> bool:
    """Check if there is a pending action and handle the user's text input.

    Returns True if a pending action was handled, False otherwise.
    """
    pending = get_pending_action(chat_id, user_id)
    if not pending:
        return False

    action = pending.get("action", "")
    context = pending.get("context") or {}
    txt = (text or "").strip()

    if not txt:
        return False

    # -- New Task --
    if action == "new_task":
        task_id = create_task(chat_id, user_id, "/task {}".format(txt))
        task = load_json(task_file("pending", task_id))
        task_code = task.get("task_code", "-")
        send_text(
            chat_id,
            t("msg.task_created_simple", code=task_code, task_id=task_id, text=txt[:200]),
            reply_markup=task_inline_keyboard(task_code),
        )
        return True

    # -- New Task with Workspace Selection --
    if action == "new_task_with_workspace":
        ws_id = context.get("ws_id", "")
        ws_label = context.get("ws_label", ws_id)
        # Fallback: verify workspace still exists; if deleted, use default
        if ws_id:
            from workspace_registry import get_workspace as _get_ws_verify, get_default_workspace as _get_def_ws
            ws_check = _get_ws_verify(ws_id)
            if not ws_check:
                fallback = _get_def_ws()
                if fallback:
                    ws_id = fallback["id"]
                    ws_label = fallback.get("label", ws_id)
                    send_text(
                        chat_id,
                        t("msg.ws_deleted_fallback", label=ws_label),
                    )
                else:
                    send_text(chat_id, t("msg.ws_deleted_no_fallback"), reply_markup=back_to_menu_keyboard())
                    return True
        if ws_id and should_queue_task(ws_id):
            # Workspace has active task, queue this one
            from task_state import register_task_created as _reg_task, load_runtime_state
            from utils import new_task_id as _new_tid
            q_task_id = _new_tid()
            q_info = {
                "task_id": q_task_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "text": txt,
                "action": infer_action(txt),
                "task_code": "",
            }
            pos = enqueue_task(ws_id, q_info)
            send_text(
                chat_id,
                t("msg.task_queued_ws", ws=ws_label, pos=pos, text=txt[:200]),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            # No active task, create immediately with workspace targeting
            task_id = create_task_for_workspace(chat_id, user_id, txt, ws_id, ws_label)
            task = load_json(task_file("pending", task_id))
            task_code = task.get("task_code", "-")
            send_text(
                chat_id,
                t("msg.task_created_ws", code=task_code, task_id=task_id, ws=ws_label, text=txt[:200]),
                reply_markup=task_inline_keyboard(task_code),
            )
        return True

    # -- English Practice: user sends English text --
    if action == "eng_practice_input":
        ws_id = context.get("ws_id", "")
        ws_label = context.get("ws_label", "")
        try:
            eval_result = _evaluate_english_text(txt)
        except Exception:
            eval_result = None
        if not eval_result:
            # AI evaluation failed - fallback: create task with original text
            send_text(chat_id, t("msg.eng_practice_eval_failed"))
            if ws_id:
                task_id = create_task_for_workspace(chat_id, user_id, txt, ws_id, ws_label)
            else:
                task_id = create_task(chat_id, user_id, "/task {}".format(txt))
            task = load_json(task_file("pending", task_id))
            task_code = task.get("task_code", "-")
            if ws_id:
                send_text(chat_id, t("msg.task_created_ws", code=task_code, task_id=task_id, ws=ws_label, text=txt[:200]),
                          reply_markup=task_inline_keyboard(task_code))
            else:
                send_text(chat_id, t("msg.task_created_simple", code=task_code, task_id=task_id, text=txt[:200]),
                          reply_markup=task_inline_keyboard(task_code))
            return True
        # Format issues
        issues_lines = []
        for issue in eval_result.get("issues", []):
            issue_type = issue.get("type", "")
            orig = issue.get("original", "")
            suggestion = issue.get("suggestion", "")
            explanation = issue.get("explanation", "")
            issues_lines.append("  - [{type}] \"{orig}\" → \"{sug}\" ({exp})".format(
                type=issue_type, orig=orig, sug=suggestion, exp=explanation))
        issues_text = "\n".join(issues_lines) if issues_lines else "  (No issues found)"
        result_msg = t("msg.eng_practice_result",
                       original=eval_result.get("original", txt),
                       corrected=eval_result.get("corrected", txt),
                       issues=issues_text,
                       chinese_meaning=eval_result.get("chinese_meaning", ""))
        send_text(chat_id, result_msg, reply_markup=eng_practice_confirm_keyboard())
        set_pending_action(chat_id, user_id, "eng_practice_confirm", {
            "ws_id": ws_id,
            "ws_label": ws_label,
            "original_text": txt,
            "corrected_text": eval_result.get("corrected", txt),
            "chinese_meaning": eval_result.get("chinese_meaning", ""),
        })
        return True

    # -- Screenshot --
    if action == "screenshot":
        try:
            run_screenshot_once(chat_id, txt or "screenshot")
        except Exception as exc:
            send_text(chat_id, t("msg.screenshot_failed", err=str(exc)[:1000]))
        return True

    # -- Archive Search --
    if action == "archive_search":
        handle_command(chat_id, user_id, "/archive {}".format(txt))
        return True

    # -- Pipeline Config --
    if action == "pipeline_config":
        handle_command(chat_id, user_id, "/set_pipeline {}".format(txt))
        return True

    # -- Mgr Restart (needs OTP) --
    if action == "mgr_restart":
        handle_command(chat_id, user_id, "/mgr_restart {}".format(txt))
        return True

    # -- Mgr Reinit (needs OTP) --
    if action == "mgr_reinit":
        handle_command(chat_id, user_id, "/mgr_reinit {}".format(txt))
        return True

    # -- Ops Restart (needs OTP) --
    if action == "ops_restart":
        handle_command(chat_id, user_id, "/ops_restart {}".format(txt))
        return True

    # -- Set Workspace (path + OTP) --
    if action == "set_workspace":
        handle_command(chat_id, user_id, "/ops_set_workspace {}".format(txt))
        return True

    # -- Reset Workspace (needs OTP) --
    if action == "reset_workspace":
        handle_command(chat_id, user_id, "/ops_set_workspace default {}".format(txt))
        return True

    # -- Accept with OTP --
    if action == "accept_otp":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/accept {} {}".format(ref, txt))
        return True

    # -- Reject with OTP + reason --
    if action == "reject_otp":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/reject {} {}".format(ref, txt))
        return True

    # -- Reject with reason only --
    if action == "reject_reason":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/reject {} {}".format(ref, txt))
        return True

    # -- Retry with OTP + supplement --
    if action == "retry_otp":
        ref = context.get("task_ref", "")
        handle_command(chat_id, user_id, "/retry {} {}".format(ref, txt))
        return True

    # -- Archive Show --
    if action == "archive_show":
        handle_command(chat_id, user_id, "/archive_show {}".format(txt))
        return True

    # -- Archive Log --
    if action == "archive_log":
        handle_command(chat_id, user_id, "/archive_log {}".format(txt))
        return True

    # -- Auth Debug --
    if action == "auth_debug":
        handle_command(chat_id, user_id, "/auth_debug {}".format(txt))
        return True

    # -- Pipeline Config Custom --
    if action == "pipeline_config_custom":
        handle_command(chat_id, user_id, "/set_pipeline {}".format(txt))
        return True

    # -- Workspace Add --
    if action == "workspace_add":
        handle_command(chat_id, user_id, "/workspace_add {}".format(txt))
        return True

    # -- Workspace Remove (text input fallback) --
    if action == "workspace_remove":
        handle_command(chat_id, user_id, "/workspace_remove {}".format(txt))
        return True

    # -- Workspace Set Default (text input fallback) --
    if action == "workspace_set_default":
        handle_command(chat_id, user_id, "/workspace_default {}".format(txt))
        return True

    # -- Search Root Add --
    if action == "search_root_add":
        handle_command(chat_id, user_id, "/workspace_search_roots add {}".format(txt))
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


def _infer_provider_from_model(model: str, provider: str = "") -> str:
    p = (provider or "").strip().lower()
    if p in {"anthropic", "openai"}:
        return p
    m = (model or "").strip().lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return ""


def verify_risky_operation(chat_id: int, user_id: int, otp: Optional[str], usage: str) -> Tuple[bool, Optional[str]]:
    if not is_ops_allowed(chat_id, user_id):
        return False, "not authorized for {}".format(usage)
    if not get_auth_state():
        return False, t("msg.2fa_not_init")
    token = (otp or "").strip()
    if not token:
        return False, usage
    otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
    if not verify_otp(token, window=otp_window):
        return False, t("msg.2fa_failed")
    return True, None


def _requires_acceptance_2fa() -> bool:
    """Return True when TASK_STRICT_ACCEPTANCE=1 AND 2FA has been initialized."""
    if os.getenv("TASK_STRICT_ACCEPTANCE", "0") != "1":
        return False
    return bool(get_auth_state())


def _do_summary_command(chat_id: int, user_id: int) -> None:
    """Handle /summary: show workspace selector or generate summary directly."""
    from workspace_registry import ensure_current_workspace_registered, list_workspaces as _list_ws_sum
    ensure_current_workspace_registered()
    workspaces = _list_ws_sum()
    if not workspaces:
        send_text(
            chat_id,
            t("msg.add_workspace_first"),
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if len(workspaces) > 1:
        send_text(
            chat_id,
            t("msg.summary_panel"),
            reply_markup=workspace_select_keyboard(workspaces, "summary_ws"),
        )
    else:
        ws = workspaces[0]
        _generate_and_send_summary(chat_id, Path(ws["path"]))


def _generate_and_send_summary(chat_id: int, workspace_path: Path) -> None:
    """Generate AI-driven project summary and send to user."""
    from project_summary import generate_ai_summary

    if not workspace_path.exists():
        send_text(
            chat_id,
            t("msg.path_not_exist", path=workspace_path),
            reply_markup=back_to_menu_keyboard(),
        )
        return

    send_text(chat_id, t("msg.analyzing_project"))

    report = generate_ai_summary(workspace_path, commit_count=3)

    # Telegram message limit: 4096 chars
    if len(report) > 4096:
        report = report[:4090] + "\n" + t("msg.content_truncated")

    send_text(chat_id, report, reply_markup=back_to_menu_keyboard())


def handle_command(chat_id: int, user_id: int, text: str) -> bool:
    txt = (text or "").strip()
    if txt.startswith("/menu") or txt.startswith("/start"):
        active_workspace = resolve_active_workspace()
        auth_ready = t("msg.enabled") if get_auth_state() else t("msg.not_initialized")
        backend = get_agent_backend()
        model = get_claude_model() or t("msg.not_set")
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

    if txt.startswith("/help"):
        send_text(
            chat_id,
            HELP_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/screenshot"):
        try:
            body = txt[12:].strip() if len(txt) > 11 else ""
            if body and _looks_like_screenshot_task_tail(body):
                task_id = create_task(chat_id, user_id, "/task {}".format(txt))
                task = load_json(task_file("pending", task_id))
                task_code = task.get("task_code", "-")
                send_text(
                    chat_id,
                    t("msg.screenshot_as_task_created",
                        code=task_code,
                        task_id=task_id,
                        text=txt[:200],
                    ),
                    reply_markup=task_inline_keyboard(task_code),
                )
                return True
            run_screenshot_once(chat_id, body or "screenshot")
        except Exception as exc:
            send_text(chat_id, t("msg.screenshot_failed", err=str(exc)[:1000]))
        return True

    if txt.startswith("/ops_whoami"):
        active_workspace = resolve_active_workspace()
        send_text(
            chat_id,
            t("msg.identity_info") + "\n"
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

    if txt.startswith("/info"):
        backend = get_agent_backend()
        model = get_claude_model() or t("msg.not_set")
        provider = get_model_provider() or t("msg.not_set")
        active_workspace = resolve_active_workspace()
        lines = [
            t("msg.system_info"),
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            t("msg.info_backend", backend=backend),
            t("msg.info_model", model=model),
            t("msg.info_provider", provider=provider),
            t("msg.info_workspace", workspace=str(active_workspace)),
            t("msg.info_2fa", status=t("msg.enabled") if get_auth_state() else t("msg.not_initialized")),
        ]
        if backend == "pipeline":
            stages = get_pipeline_stages()
            lines.append(t("msg.info_pipeline", pipeline=format_pipeline_stages(stages)))
        send_text(chat_id, "\n".join(lines), reply_markup=back_to_menu_keyboard())
        return True

    if txt.startswith("/summary"):
        _do_summary_command(chat_id, user_id)
        return True

    # -- /task (no args): interactive workspace selection + task input --
    if txt == "/task":
        from workspace_registry import ensure_current_workspace_registered, list_workspaces as _list_ws_cmd
        ensure_current_workspace_registered()
        workspaces = _list_ws_cmd()
        eng_on = get_skill_english_practice()
        eng_status = t("msg.eng_practice_status_on") if eng_on else t("msg.eng_practice_status_off")
        if len(workspaces) > 1:
            send_text(
                chat_id,
                t("msg.new_task_ws_panel") + eng_status,
                reply_markup=workspace_select_keyboard(workspaces, "ws_task_select"),
            )
        else:
            if eng_on:
                set_pending_action(chat_id, user_id, "eng_practice_input")
                send_text(chat_id, str(PENDING_PROMPTS["eng_practice_input"]),
                          reply_markup=cancel_keyboard())
            else:
                set_pending_action(chat_id, user_id, "new_task")
                send_text(
                    chat_id,
                    str(PENDING_PROMPTS["new_task"]) + eng_status,
                    reply_markup=cancel_keyboard(),
                )
        return True

    if txt.startswith("/auth_init"):
        st = init_authenticator(issuer="aming-claw", account_name="telegram-ops")
        secret_line = (
            "secret(base32)={}".format(st.get("secret_b32", ""))
            if st.get("created")
            else "secret(base32)={}".format(st.get("masked_secret", ""))
        )
        send_text(
            chat_id,
            t("msg.2fa_init_status", status=(t("msg.2fa_initialized") if st.get("created") else t("msg.2fa_existing")))
            + "\n{}\notpauth_uri={}\nperiod={}s, digits={}\nseed_file={}\n".format(
                secret_line,
                st.get("otpauth_uri", ""),
                st.get("period_sec", 60),
                st.get("digits", 6),
                st.get("seed_file", ""),
            )
            + t("msg.2fa_save_reminder"),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/auth_status"):
        st = get_auth_state()
        if not st:
            send_text(
                chat_id,
                t("msg.2fa_not_init"),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        send_text(
            chat_id,
            t("msg.2fa_status_detail",
                secret=st.get("secret_b32", "")[:4] + "***" + st.get("secret_b32", "")[-4:],
                period=st.get("period_sec", 60),
                digits=st.get("digits", 6),
                updated=st.get("updated_at", ""),
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/auth_debug"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /auth_debug")
            return True
        otp = parse_otp(txt)
        if not otp:
            send_text(chat_id, t("msg.usage_auth_debug"))
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

    if txt.startswith("/switch_backend"):
        parts = txt.split(maxsplit=1)
        backend = parts[1].strip().lower() if len(parts) >= 2 else ""
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /switch_backend")
            return True
        if not backend:
            # No args: show backend selection inline keyboard
            send_text(
                chat_id,
                t("msg.switch_backend_panel", backend=get_agent_backend()),
                reply_markup=backend_select_keyboard(),
            )
            return True
        if backend not in KNOWN_BACKENDS:
            send_text(
                chat_id,
                t("msg.unknown_backend", backend=backend, available="|".join(sorted(KNOWN_BACKENDS))),
            )
            return True
        set_agent_backend(backend, changed_by=user_id)
        if backend == "pipeline":
            stages = get_pipeline_stages()
            if stages:
                send_text(
                    chat_id,
                    t("msg.backend_switched_pipeline", pipeline=format_pipeline_stages(stages)),
                )
            else:
                send_text(
                    chat_id,
                    t("msg.backend_switched_pipeline_empty"),
                )
        else:
            send_text(chat_id, t("msg.backend_switched", backend=backend))
        return True

    if txt.startswith("/switch_model"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /switch_model")
            return True
        parts = txt.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) >= 2 else ""
        current_model = get_claude_model() or t("msg.default_label")
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
            send_text(chat_id, t("msg.model_switched", tag=tag, model=arg))
            return True

        # 无参数 → 从 API 拉取并展示 inline keyboard
        send_text(chat_id, t("msg.fetching_models"))
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
            t("msg.current_model_select", model=current_model, provider=current_provider),
            reply_markup=keyboard,
        )
        return True

    if txt.startswith("/set_role_model"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /set_role_model")
            return True
        parts = txt.split()
        if len(parts) < 3:
            send_text(
                chat_id,
                t("msg.usage_set_role_model")
                + "\n  /set_role_model pm gpt-4o openai\n"
                "  /set_role_model qa claude-sonnet-4-6 anthropic\n"
                "  /set_role_model test default",
            )
            return True
        role_name = parts[1].strip().lower()
        if role_name not in ROLE_DEFINITIONS:
            send_text(chat_id, t("msg.unknown_role", role=role_name))
            return True
        model_arg = parts[2].strip()
        if model_arg.lower() in {"default", "none", "clear"}:
            model_id = ""
            provider = ""
        else:
            model_id = model_arg
            raw_provider = parts[3].strip().lower() if len(parts) >= 4 else ""
            if raw_provider and raw_provider not in {"anthropic", "openai"}:
                send_text(chat_id, t("msg.provider_only"))
                return True
            provider = _infer_provider_from_model(model_id, raw_provider)
        try:
            set_role_stage_model(role_name, model_id, provider=provider, changed_by=user_id)
        except ValueError as exc:
            send_text(chat_id, t("msg.save_failed", err=str(exc)))
            return True
        role_def = ROLE_DEFINITIONS.get(role_name, {})
        stages = get_role_pipeline_stages()
        if model_id:
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            summary = t("msg.role_set",
                emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name), tag=tag, model=model_id
            ).strip()
        else:
            summary = t("msg.role_restored",
                emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name)
            ).strip()
        send_text(
            chat_id,
            t("msg.role_pipeline_updated", summary=summary, config=format_role_pipeline_stages(stages)),
            reply_markup=role_pipeline_config_keyboard(stages),
        )
        return True

    if txt.startswith("/set_pipeline"):
        if not is_ops_allowed(chat_id, user_id):
            send_text(chat_id, "not authorized for /set_pipeline")
            return True
        parts = txt.split(maxsplit=1)
        raw = parts[1].strip().lower() if len(parts) >= 2 else ""
        if not raw:
            preset_list = "\n".join("  {} → {}".format(k, format_pipeline_stages(v)) for k, v in PIPELINE_PRESETS.items())
            send_text(
                chat_id,
                t("msg.usage_set_pipeline", presets=preset_list),
            )
            return True
        # Check for preset name
        if raw in PIPELINE_PRESETS:
            stages = PIPELINE_PRESETS[raw]
        else:
            from config import _parse_pipeline_stages
            stages = _parse_pipeline_stages(raw)
        if not stages:
            send_text(chat_id, t("msg.pipeline_parse_error", config=repr(raw)))
            return True
        set_pipeline_stages(stages, changed_by=user_id)
        send_text(
            chat_id,
            t("msg.pipeline_activated", stages=format_pipeline_stages(stages)),
        )
        return True

    if txt.startswith("/show_pipeline"):
        backend = get_agent_backend()
        stages = get_pipeline_stages()
        if backend != "pipeline":
            send_text(
                chat_id,
                t("msg.pipeline_not_active", backend=backend, pipeline=format_pipeline_stages(stages) if stages else t("config.not_configured")),
            )
            return True
        if not stages:
            send_text(
                chat_id,
                t("msg.pipeline_not_configured")
                + "\n/set_pipeline plan:openai code:claude verify:codex",
            )
            return True
        lines = [t("msg.current_pipeline")]
        for i, s in enumerate(stages, 1):
            name = s.get("name", "?")
            role_def = ROLE_DEFINITIONS.get(name)
            model = s.get("model", "")
            provider = s.get("provider", "")
            if role_def:
                emoji = role_def.get("emoji", "")
                label = role_def.get("label", name)
                if model:
                    from config import _provider_tag
                    tag = _provider_tag(provider)
                    lines.append("  {}. {} {} {} {} {}".format(i, emoji, label, t("msg.pipeline_arrow"), model, tag).rstrip())
                else:
                    lines.append("  {}. {} {} {} ({})".format(i, emoji, label, t("msg.pipeline_arrow"), s.get("backend", "?")))
            else:
                if model:
                    from config import _provider_tag
                    tag = _provider_tag(provider)
                    lines.append("  {}. {} {} {} {}".format(i, name, t("msg.pipeline_arrow"), model, tag).rstrip())
                else:
                    lines.append("  {}. {}({})".format(i, name, s.get("backend", "?")))
        lines.append("\n" + t("msg.builtin_presets") + " " + ", ".join(PIPELINE_PRESETS.keys()))
        send_text(chat_id, "\n".join(lines))
        return True

    if txt.startswith("/mgr_status"):
        status = read_manager_status()
        if not status:
            send_text(chat_id, t("msg.mgr_not_running"))
            return True
        services = status.get("services") or {}
        lines = [t("msg.mgr_status", updated=status.get("updated_at", "-"))]
        for name, svc_status in services.items():
            lines.append("  {}: {}".format(name, svc_status))
        lines.append(t("msg.info_backend", backend=get_agent_backend()))
        lines.append("manager pid: {}".format(status.get("pid", "-")))
        send_text(chat_id, "\n".join(lines))
        return True

    if txt.startswith("/mgr_restart"):
        otp = parse_otp(txt)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/mgr_restart <OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        request_id = write_manager_signal("restart", {}, user_id)
        send_text(
            chat_id,
            t("msg.mgr_restart_sent", request_id=request_id, timeout=os.getenv("MANAGER_POLL_SEC", "5")),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/mgr_reinit"):
        otp = parse_otp(txt)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/mgr_reinit <OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        request_id = write_manager_signal("reinit", {}, user_id)
        send_text(
            chat_id,
            t("msg.mgr_reinit_sent", request_id=request_id),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/ops_restart"):
        otp = parse_otp(txt)
        ok, msg = verify_risky_operation(chat_id, user_id, otp, "/ops_restart <OTP>")
        if not ok:
            send_text(chat_id, msg or "operation blocked", reply_markup=back_to_menu_keyboard())
            return True
        send_text(chat_id, t("msg.ops_restart_start"))
        ok, msg = run_restart_all(chat_id, user_id)
        send_text(
            chat_id,
            "restart-all: {}\n{}".format("ok" if ok else "failed", msg),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    if txt.startswith("/ops_set_workspace_pick"):
        idx, otp = parse_pick_workspace_command(txt)
        ok, msg = verify_risky_operation(
            chat_id,
            user_id,
            otp,
            "/ops_set_workspace_pick <index> <OTP>",
        )
        if not ok:
            send_text(chat_id, msg or "operation blocked")
            return True
        if idx is None or idx <= 0:
            send_text(chat_id, t("msg.usage_ops_set_ws_pick"))
            return True
        candidates = read_workspace_candidates(chat_id, user_id)
        if not candidates:
            send_text(chat_id, t("msg.no_candidates"))
            return True
        if idx > len(candidates):
            send_text(chat_id, t("msg.index_out_of_range", max=len(candidates)))
            return True
        target = candidates[idx - 1]
        if is_risky_workspace(target):
            send_text(chat_id, t("msg.reject_risky_dir", path=str(target)))
            return True
        set_workspace_override(target, changed_by=user_id)
        clear_workspace_candidates(chat_id, user_id)
        send_text(chat_id, t("msg.workspace_switched", path=str(target)))
        return True

    if txt.startswith("/ops_set_workspace"):
        raw_path, otp = parse_set_workspace_command(txt)
        ok, msg = verify_risky_operation(
            chat_id,
            user_id,
            otp,
            "/ops_set_workspace <path|default> <OTP>",
        )
        if not ok:
            send_text(chat_id, msg or "operation blocked")
            return True
        if not raw_path:
            send_text(chat_id, t("msg.usage_ops_set_ws"))
            return True
        if raw_path.lower() in {"default", "reset"}:
            clear_workspace_override(changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, t("msg.workspace_reset"))
            return True
        p = Path(raw_path).expanduser()
        if p.exists() and p.is_dir():
            rp = p.resolve()
            if is_risky_workspace(rp):
                send_text(chat_id, t("msg.reject_risky_dir", path=str(rp)))
                return True
            set_workspace_override(rp, changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, t("msg.workspace_switched", path=str(rp)))
            return True

        candidates = find_git_workspace_candidates(raw_path)
        if not candidates:
            send_text(chat_id, t("msg.workspace_not_found", query=raw_path))
            return True
        if len(candidates) == 1:
            target = candidates[0]
            if is_risky_workspace(target):
                send_text(chat_id, t("msg.reject_risky_dir", path=str(target)))
                return True
            set_workspace_override(target, changed_by=user_id)
            clear_workspace_candidates(chat_id, user_id)
            send_text(chat_id, t("msg.workspace_switched", path=str(target)))
            return True

        store_workspace_candidates(chat_id, user_id, raw_path, candidates)
        lines = [
            t("msg.multiple_workspaces")
        ]
        for idx, candidate in enumerate(candidates, 1):
            lines.append("{}. {}".format(idx, str(candidate)))
        send_text(chat_id, "\n".join(lines[:25]))
        return True

    if txt.startswith("/accept"):
        parts = txt.split(maxsplit=2)
        if len(parts) < 2:
            # No args: show pending_acceptance tasks as interactive list
            tasks = _collect_tasks_by_status(chat_id, "pending_acceptance")
            if not tasks:
                send_text(
                    chat_id,
                    t("msg.no_accept_tasks"),
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            send_text(
                chat_id,
                t("msg.accept_list_header", count=len(tasks)),
                reply_markup=pending_tasks_keyboard(tasks, "accept"),
            )
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
                    t("msg.task_already_archived",
                        archive_id=archived.get("archive_id", ""),
                        status=status_tag(archived.get("status", "unknown")),
                    ),
                )
                return True
            send_text(chat_id, t("msg.task_not_found", ref=task_ref))
            return True
        stage = str(found.get("_stage") or "")
        if stage != "results":
            send_text(chat_id, t("msg.task_not_ready", stage=stage))
            return True
        if str(found.get("status") or "") not in {"pending_acceptance", "rejected", "completed", "failed"}:
            send_text(chat_id, t("msg.task_no_accept", status=status_tag(found.get("status", "unknown"))))
            return True

        # ── Run post-acceptance tests before committing ──
        test_result = run_post_acceptance_tests(resolve_active_workspace())
        if not test_result.get("skipped"):
            if not test_result["passed"]:
                error_detail = test_result.get("error") or ""
                output = test_result.get("output") or ""
                msg = t("msg.accept_test_failed") + "\n\n"
                if error_detail:
                    msg += t("msg.error_prefix", err=error_detail) + "\n"
                if output:
                    msg += t("msg.test_output", output=output[:2000])
                send_text(chat_id, msg)
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
        append_task_event(
            str(found.get("task_id") or ""),
            "accepted",
            {
                "status": "accepted",
                "stage": "results",
                "accepted_by": int(user_id),
            },
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
                    git_commit_msg = "\nGit: commit={}, {} files".format(sha, len(files))
                else:
                    git_commit_msg = "\nGit: no changes to commit"
            elif commit_result.get("error"):
                git_commit_msg = "\nGit: commit failed - {}".format(commit_result["error"])
        except Exception as exc:
            git_commit_msg = "\nGit: commit error - {}".format(str(exc)[:200])

        # Build confirmation message and optional restart button
        _accept_msg = t("msg.task_accepted",
            code=found.get("task_code", "-"),
            task_id=found.get("task_id", ""),
            archive_id=archive_meta.get("archive_id", ""),
            git_msg=git_commit_msg,
        )
        _restart_needed = False
        try:
            _restart_needed = commit_result.get("needs_restart", False) if commit_result else False
        except Exception:
            pass
        if _restart_needed:
            _task_ref = found.get("task_code") or found.get("task_id", "")
            _accept_msg += "\n\n" + t("msg.core_module_changed")
            _restart_kb = {
                "inline_keyboard": [[
                    {"text": t("msg.restart_service"), "callback_data": safe_callback_data("restart", _task_ref)},
                    {"text": t("msg.skip_restart"), "callback_data": safe_callback_data("skip_restart", _task_ref)},
                ]]
            }
            send_text(chat_id, _accept_msg, reply_markup=_restart_kb)
        else:
            send_text(chat_id, _accept_msg)
        archive_path = Path(str(archive_meta.get("archive_file") or ""))
        if archive_path.exists():
            send_text(chat_id, t("msg.archive_file_generated", path=str(archive_path)))

        # ── Auto-launch queued tasks for this workspace ──
        _auto_launch_queued_task(found, chat_id)

        return True

    if txt.startswith("/reject"):
        raw_reject = txt[len("/reject"):].strip()
        reject_parts = raw_reject.split(None, 2)
        if not reject_parts:
            # No args: show pending_acceptance tasks as interactive list
            tasks = _collect_tasks_by_status(chat_id, "pending_acceptance")
            if not tasks:
                send_text(
                    chat_id,
                    t("msg.no_reject_tasks"),
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            send_text(
                chat_id,
                t("msg.reject_list_header", count=len(tasks)),
                reply_markup=pending_tasks_keyboard(tasks, "reject"),
            )
            return True
        task_ref = reject_parts[0]
        if _requires_acceptance_2fa():
            otp_token = reject_parts[1] if len(reject_parts) >= 2 else None
            otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
            if not otp_token:
                send_text(
                    chat_id,
                    t("msg.reject_need_otp", ref=task_ref),
                )
                return True
            if not verify_otp(otp_token, window=otp_window):
                send_text(
                    chat_id,
                    t("msg.reject_otp_failed", ref=task_ref),
                )
                return True
            reason = reject_parts[2].strip() if len(reject_parts) >= 3 else ""
            if not reason:
                send_text(
                    chat_id,
                    t("msg.reject_need_reason_otp", ref=task_ref),
                )
                return True
        else:
            reason = " ".join(reject_parts[1:]).strip() if len(reject_parts) >= 2 else ""
            if not reason:
                send_text(chat_id, t("msg.reject_need_reason"))
                return True
        if not task_ref:
            send_text(chat_id, t("msg.usage_reject"))
            return True
        found = find_task(task_ref)
        if not found:
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    t("msg.task_already_archived",
                        archive_id=archived.get("archive_id", ""),
                        status=status_tag(archived.get("status", "unknown")),
                    ),
                )
                return True
            send_text(chat_id, t("msg.task_not_found", ref=task_ref))
            return True
        stage = str(found.get("_stage") or "")
        if stage != "results":
            send_text(chat_id, t("msg.task_not_ready", stage=stage))
            return True
        if str(found.get("status") or "") not in {"pending_acceptance", "rejected", "completed", "failed"}:
            send_text(chat_id, t("msg.task_no_reject", status=status_tag(found.get("status", "unknown"))))
            return True

        found["status"] = "rejected"
        found["updated_at"] = utc_iso()
        acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
        acceptance["state"] = "rejected"
        acceptance["acceptance_required"] = True
        acceptance["archive_allowed"] = False
        acceptance["rejected_at"] = utc_iso()
        acceptance["rejected_by"] = int(user_id)
        acceptance["reason"] = reason
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
        append_task_event(
            str(found.get("task_id") or ""),
            "rejected",
            {
                "status": "rejected",
                "stage": "results",
                "reason": reason,
                "rejected_by": int(user_id),
            },
        )
        # ── Git: rollback to checkpoint on rejection ──
        git_rollback_msg = ""
        checkpoint = str(found.get("_git_checkpoint") or "")
        if checkpoint:
            try:
                rb_result = rollback_to_checkpoint(checkpoint)
                if rb_result.get("success"):
                    git_rollback_msg = "\nGit: rolled back to {} (was: {})".format(
                        rb_result.get("current_commit", ""),
                        rb_result.get("reverted_commit", ""),
                    )
                elif rb_result.get("error"):
                    git_rollback_msg = "\nGit: rollback failed - {}".format(rb_result["error"])
            except Exception as exc:
                git_rollback_msg = "\nGit: rollback error - {}".format(str(exc)[:200])
        else:
            git_rollback_msg = "\nGit: no checkpoint, skipping rollback"

        _reject_code = found.get("task_code", "-")
        _reject_keyboard = {
            "inline_keyboard": [
                [
                    {"text": t("task.view_progress"), "callback_data": "status:{}".format(_reject_code)},
                    {"text": t("task.retry"), "callback_data": "retry:{}".format(_reject_code)},
                ],
                [
                    {"text": t("task.accept"), "callback_data": "accept:{}".format(_reject_code)},
                    {"text": t("task.view_events"), "callback_data": "events:{}".format(_reject_code)},
                ],
            ]
        }
        send_text(
            chat_id,
            t("msg.task_rejected",
                code=_reject_code,
                task_id=found.get("task_id", ""),
                reason=acceptance.get("reason", t("retry.no_reason")),
                git_msg=git_rollback_msg,
            ),
            reply_markup=_reject_keyboard,
        )
        return True

    if txt.startswith("/retry"):
        raw_retry = txt[len("/retry"):].strip()
        retry_parts = raw_retry.split(None, 1)
        if not retry_parts:
            # No args: show rejected tasks as interactive list
            tasks = _collect_tasks_by_status(chat_id, "rejected")
            if not tasks:
                send_text(
                    chat_id,
                    t("msg.no_retry_tasks"),
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            send_text(
                chat_id,
                t("msg.retry_list_header", count=len(tasks)),
                reply_markup=pending_tasks_keyboard(tasks, "retry"),
            )
            return True
        task_ref = retry_parts[0]
        extra_instruction = retry_parts[1] if len(retry_parts) >= 2 else ""

        # 2FA check
        if _requires_acceptance_2fa():
            otp_parts = raw_retry.split(None, 2)
            otp_token = otp_parts[1] if len(otp_parts) >= 2 else None
            extra_instruction = otp_parts[2] if len(otp_parts) >= 3 else ""
            otp_window = int(os.getenv("AUTH_OTP_WINDOW", "2"))
            if not otp_token:
                send_text(
                    chat_id,
                    t("msg.retry_need_otp", ref=task_ref),
                )
                return True
            if not verify_otp(otp_token, window=otp_window):
                send_text(
                    chat_id,
                    t("msg.retry_otp_failed", ref=task_ref),
                )
                return True

        found = find_task(task_ref)
        if not found:
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    t("msg.task_already_archived",
                        archive_id=archived.get("archive_id", ""),
                        status=status_tag(archived.get("status", "unknown")),
                    ),
                )
                return True
            send_text(chat_id, t("msg.task_not_found", ref=task_ref))
            return True

        # AC-6: Workspace queue compatibility
        ws_id = str(found.get("target_workspace_id", "")).strip()
        if not ws_id:
            ws_label = str(found.get("target_workspace", "")).strip()
            if ws_label:
                from workspace_registry import find_workspace_by_label
                ws = find_workspace_by_label(ws_label)
                if ws:
                    ws_id = ws["id"]
        if not ws_id:
            from workspace_registry import get_default_workspace
            ws = get_default_workspace()
            if ws:
                ws_id = ws["id"]

        # Call core retry logic
        success, msg, updated = retry_task(found, user_id, extra_instruction)
        if not success:
            send_text(chat_id, msg)
            return True

        # AC-6: Check workspace queue - if workspace busy, enqueue
        if ws_id and should_queue_task(ws_id):
            # Move the pending file back and enqueue instead
            pending_path = task_file("pending", str(updated["task_id"]))
            if pending_path.exists():
                pending_path.unlink()
            q_info = {
                "task_id": str(updated.get("task_id", "")),
                "task_code": str(updated.get("task_code", "")),
                "chat_id": chat_id,
                "user_id": user_id,
                "text": str(updated.get("_retry_enhanced_text") or updated.get("text", "")),
                "action": str(updated.get("action", "codex")),
            }
            pos = enqueue_task(ws_id, q_info)
            update_task_runtime(updated, status="queued", stage="pending")
            send_text(
                chat_id,
                t("msg.task_queued", msg=msg, pos=pos),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            send_text(
                chat_id,
                msg,
                reply_markup=task_inline_keyboard(
                    updated.get("task_code", "-"),
                ),
            )
        return True

    # -- /cancel: cancel processing/queued tasks --
    if txt.startswith("/cancel"):
        cancel_parts = txt.split(maxsplit=1)
        cancel_arg = cancel_parts[1].strip() if len(cancel_parts) >= 2 else ""
        if cancel_arg:
            # Direct cancel by ref
            found = find_task(cancel_arg)
            if not found:
                send_text(chat_id, t("msg.task_not_found", ref=cancel_arg), reply_markup=back_to_menu_keyboard())
                return True
            task_id = str(found.get("task_id") or "")
            st = task_status_snapshot(task_id)
            current_status = str((st or found).get("status") or found.get("status") or "").strip().lower()
            if current_status not in ("pending", "processing", "queued"):
                send_text(
                    chat_id,
                    t("msg.only_cancel_active", status=current_status),
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            # Remove from workspace queue if queued
            if current_status == "queued":
                ws_id = str(found.get("target_workspace_id", "")).strip()
                if ws_id:
                    remove_from_queue(ws_id, task_id)
            # Remove pending file
            pending_path = task_file("pending", task_id)
            if pending_path.exists():
                pending_path.unlink()
            processing_path = task_file("processing", task_id)
            if processing_path.exists():
                processing_path.unlink()
            update_task_runtime(found, status="cancelled", stage="results")
            mark_task_finished(found, status="cancelled", stage="results", error=t("msg.user_cancelled"))
            send_text(
                chat_id,
                t("msg.task_cancelled", ref=cancel_arg),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        # No args: show cancellable tasks (processing + queued + pending)
        tasks_processing = _collect_tasks_by_status(chat_id, "processing")
        tasks_pending = _collect_tasks_by_status(chat_id, "pending")
        # Also collect queued tasks
        tasks_queued: List[Dict] = []
        active = list_active_tasks(chat_id=chat_id)
        for item in active:
            tid = str(item.get("task_id") or "")
            st = task_status_snapshot(tid) if tid else None
            cs = str((st or item).get("status") or item.get("status") or "").strip().lower()
            if cs == "queued":
                enriched = dict(item)
                if st:
                    enriched["status"] = st.get("status", enriched.get("status"))
                    enriched["task_code"] = st.get("task_code", enriched.get("task_code", "-"))
                tasks_queued.append(enriched)
        all_cancellable = tasks_pending + tasks_processing + tasks_queued
        if not all_cancellable:
            send_text(
                chat_id,
                t("msg.no_cancel_tasks"),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        send_text(
            chat_id,
            t("msg.cancel_list_header", count=len(all_cancellable)),
            reply_markup=pending_tasks_keyboard(all_cancellable, "cmd_cancel"),
        )
        return True

    if txt.startswith("/clear_tasks"):
        active = list_active_tasks(chat_id=chat_id)
        if not active:
            send_text(chat_id, t("msg.no_active_tasks"), reply_markup=back_to_menu_keyboard())
            return True
        removed = clear_active_tasks(chat_id)
        if removed == 0:
            send_text(chat_id, t("msg.all_tasks_running"), reply_markup=back_to_menu_keyboard())
        else:
            send_text(
                chat_id,
                t("msg.tasks_cleared", count=removed),
                reply_markup=back_to_menu_keyboard(),
            )
        return True

    if txt.startswith("/status"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            active = list_active_tasks(chat_id=chat_id)
            # Read full task data (with acceptance dict) for accurate status display.
            merged = []
            for item in active:
                task_id = str(item.get("task_id") or "")
                if not task_id:
                    continue
                found = find_task(task_id)
                if found:
                    enriched = merge_task_with_status(found)
                    # Preserve fields from active entry that may be missing in task file
                    for key in ("task_code", "action"):
                        if not enriched.get(key) and item.get(key):
                            enriched[key] = item[key]
                else:
                    # Task file gone (archived/cleared); fall back to state snapshot
                    enriched = dict(item)
                    st = task_status_snapshot(task_id)
                    if st:
                        enriched["_status_snapshot"] = st
                        enriched["status"] = st.get("status", enriched.get("status"))
                        enriched["_stage"] = st.get("stage", enriched.get("_stage", "unknown"))
                        enriched["updated_at"] = st.get("updated_at", enriched.get("updated_at", ""))
                        enriched["task_code"] = st.get("task_code", enriched.get("task_code", "-"))
                merged.append(enriched)
            merged.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            if not merged:
                send_text(
                    chat_id,
                    t("msg.no_active_tasks_status"),
                    reply_markup=back_to_menu_keyboard(),
                )
                return True
            lines = [t("msg.active_task_list")]
            for item in merged[:20]:
                lines.append(
                    t("msg.task_list_item",
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
                        "Task [{code}] {task_id} Status: {status}({status_tag})\naction={action}\nstage={stage}\nupdated_at={updated}\nstarted_at={started}\nended_at={ended}\nend_marker={end_marker}\nsummary: {summary}".format(
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
                            summary=str(st.get("summary", "")).strip()[:300] or t("msg.no_summary_short"),
                        ),
                    )
                    return True
            archived = find_archive_entry(task_ref)
            if archived:
                send_text(
                    chat_id,
                    "Archive [{code}] Status: {status}({status_tag})\nAccepted (archived)\naction={action}\narchive_id={archive_id}\ntask_id={task_id}\ncompleted_at={completed_at}\nsummary: {summary}".format(
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
            send_text(chat_id, t("msg.task_not_found", ref=task_ref))
            return True
        found = merge_task_with_status(found)
        executor = found.get("executor") or {}
        code = found.get("task_code", "-")
        acceptance = found.get("acceptance") if isinstance(found.get("acceptance"), dict) else {}
        st = found.get("_status_snapshot") if isinstance(found.get("_status_snapshot"), dict) else {}
        send_text(
            chat_id,
            t("msg.status_detail",
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

    if txt.startswith("/events"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_events"))
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
            send_text(chat_id, t("msg.task_not_found", ref=task_ref))
            return True
        send_text(chat_id, build_events_text(str(st.get("task_id") or task_id), str(st.get("task_code") or "-")))
        return True

    if txt.startswith("/archive_show"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_archive_show"))
            return True
        ref = parts[1].strip()
        item = find_archive_entry(ref)
        if not item:
            suggest = search_archive_entries(ref, limit=5)
            if not suggest:
                send_text(chat_id, t("msg.archive_not_found", ref=ref))
                return True
            send_text(chat_id, build_archive_list_text(suggest, t("msg.archive_fuzzy_matches")))
            return True
        send_text(
            chat_id,
            t("msg.archive_detail",
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
                send_text(chat_id, "{} {}: {}".format(caption, "file", str(p)))
        return True

    if txt.startswith("/archive_log"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_archive_log"))
            return True
        ref = parts[1].strip()
        exact = find_archive_entry(ref)
        if exact:
            send_text(
                chat_id,
                t("msg.archive_log_detail",
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
                    send_text(chat_id, "{} {}: {}".format(caption, "file", str(p)))
            return True
        matches = search_archive_entries(ref, limit=30)
        if not matches:
            send_text(chat_id, t("msg.archive_log_not_found", ref=ref))
            return True
        send_text(
            chat_id,
            build_archive_grouped_text(
                matches,
                t("msg.archive_log_search", keyword=ref),
                limit_per_group=4,
            )
            + "\n" + t("msg.archive_log_hint"),
        )
        return True

    if txt.startswith("/archive"):
        query = txt[8:].strip() if len(txt) > 8 else ""
        if not query:
            grouped = grouped_archive_overview(limit_per_group=3)
            if not grouped:
                send_text(chat_id, t("msg.no_archives"), reply_markup=back_to_menu_keyboard())
                return True
            lines = [t("msg.archive_overview")]
            for action, info in grouped.items():
                lines.append(t("msg.type_count", action=action, count=info.get("count", 0)))
                for item in (info.get("items") or []):
                    lines.append(
                        "  [{code}] {archive_id} {status} | {summary}".format(
                            code=item.get("task_code", "-"),
                            archive_id=item.get("archive_id", ""),
                            status=item.get("status", "unknown"),
                            summary=str(item.get("summary", "")).strip()[:60],
                        )
                    )
            lines.append(t("msg.archive_search_hint"))
            send_text(chat_id, "\n".join(lines[:120]), reply_markup=back_to_menu_keyboard())
            return True
        matches = search_archive_entries(query, limit=20)
        if not matches:
            send_text(chat_id, t("msg.archive_not_found", ref=query), reply_markup=back_to_menu_keyboard())
            return True
        if len(matches) > 8:
            send_text(
                chat_id,
                build_archive_grouped_text(
                    matches,
                    t("msg.archive_search_result", keyword=query),
                    limit_per_group=4,
                ),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            send_text(
                chat_id,
                build_archive_list_text(matches, t("msg.archive_search_result", keyword=query)),
                reply_markup=back_to_menu_keyboard(),
            )
        return True

    # ── Workspace registry commands ──────────────────────────────────────────

    if txt.startswith("/workspace_add"):
        parts = txt.split(maxsplit=2)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_workspace_add"))
            return True
        raw_path = parts[1].strip()
        label = parts[2].strip() if len(parts) >= 3 else ""
        p = Path(raw_path).expanduser()
        if p.exists() and p.is_dir():
            # Exact path provided
            if is_risky_workspace(p.resolve()):
                send_text(chat_id, t("msg.reject_risky_dir", path=str(p.resolve())))
                return True
            try:
                from workspace_registry import add_workspace
                ws = add_workspace(p, label=label, created_by=user_id)
                send_text(
                    chat_id,
                    t("msg.workspace_added", id=ws["id"], label=ws["label"], path=ws["path"], default=t("msg.yes") if ws.get("is_default") else t("msg.no")),
                    reply_markup=back_to_menu_keyboard(),
                )
            except ValueError as exc:
                send_text(chat_id, t("msg.add_failed", err=str(exc)))
            return True

        # Path doesn't exist — try fuzzy search if it looks like a keyword
        if _looks_like_path(raw_path):
            send_text(chat_id, t("msg.path_not_exist", path=raw_path))
            return True

        # Fuzzy search for git workspaces matching the keyword
        candidates = find_git_workspace_candidates(raw_path)
        if not candidates:
            send_text(
                chat_id,
                t("msg.workspace_not_found", query=raw_path),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        if len(candidates) == 1:
            target = candidates[0]
            if is_risky_workspace(target):
                send_text(chat_id, t("msg.reject_risky_dir", path=str(target)))
                return True
            try:
                from workspace_registry import add_workspace
                ws = add_workspace(target, label=label or target.name, created_by=user_id)
                send_text(
                    chat_id,
                    t("msg.workspace_added", id=ws["id"], label=ws["label"], path=ws["path"], default=t("msg.yes") if ws.get("is_default") else t("msg.no")),
                    reply_markup=back_to_menu_keyboard(),
                )
            except ValueError as exc:
                send_text(chat_id, t("msg.add_failed", err=str(exc)))
            return True

        # Multiple matches — show interactive selection
        store_workspace_candidates(chat_id, user_id, raw_path, candidates)
        send_text(
            chat_id,
            t("msg.workspace_candidates", count=len(candidates), keyword=raw_path),
            reply_markup=fuzzy_workspace_add_keyboard(candidates),
        )
        return True

    if txt.startswith("/workspace_remove"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_workspace_remove"))
            return True
        ws_id = parts[1].strip()
        from workspace_registry import remove_workspace
        if remove_workspace(ws_id):
            send_text(chat_id, t("msg.workspace_dir_removed", id=ws_id), reply_markup=back_to_menu_keyboard())
        else:
            send_text(chat_id, t("msg.workspace_dir_not_found", id=ws_id))
        return True

    if txt.startswith("/workspace_default"):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            send_text(chat_id, t("msg.usage_workspace_default"))
            return True
        ws_id = parts[1].strip()
        from workspace_registry import set_default_workspace, get_workspace
        if set_default_workspace(ws_id):
            ws = get_workspace(ws_id)
            send_text(
                chat_id,
                t("msg.default_workspace_set", id=ws_id, label=ws.get("label", "") if ws else ""),
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            send_text(chat_id, t("msg.workspace_dir_not_found", id=ws_id))
        return True

    if txt.startswith("/workspace_search_roots"):
        parts = txt.split(maxsplit=2)
        # /workspace_search_roots — show current
        if len(parts) < 2:
            roots = get_workspace_search_roots()
            if roots:
                lines = [t("msg.search_roots_cmd_header")]
                for idx, r in enumerate(roots, 1):
                    lines.append("{}. {}".format(idx, r))
                lines.append(t("msg.search_roots_usage"))
                send_text(chat_id, "\n".join(lines), reply_markup=back_to_menu_keyboard())
            else:
                send_text(
                    chat_id,
                    t("msg.search_roots_not_configured"),
                    reply_markup=back_to_menu_keyboard(),
                )
            return True
        sub = parts[1].strip().lower()
        # /workspace_search_roots add <path>[;path2;...]
        if sub == "add":
            if len(parts) < 3:
                send_text(chat_id, t("msg.usage_search_roots_add"))
                return True
            raw_paths = parts[2].strip()
            added = []
            failed = []
            for segment in raw_paths.split(";"):
                segment = segment.strip()
                if not segment:
                    continue
                ok, msg = add_workspace_search_root(segment, changed_by=user_id)
                if ok:
                    added.append(msg)
                else:
                    failed.append(msg)
            lines = []
            if added:
                lines.append(t("msg.search_root_added"))
                for a in added:
                    lines.append("  {}".format(a))
            if failed:
                lines.append(t("msg.search_root_add_failed"))
                for f in failed:
                    lines.append("  {}".format(f))
            roots = get_workspace_search_roots()
            send_text(
                chat_id,
                "\n".join(lines) if lines else t("msg.no_valid_paths"),
                reply_markup=search_roots_keyboard(roots),
            )
            return True
        # /workspace_search_roots remove <index>
        if sub == "remove":
            if len(parts) < 3:
                send_text(chat_id, t("msg.usage_search_roots_remove"))
                return True
            try:
                idx = int(parts[2].strip())
            except ValueError:
                send_text(chat_id, t("msg.index_must_be_number"))
                return True
            ok, msg = remove_workspace_search_root(idx, changed_by=user_id)
            if ok:
                roots = get_workspace_search_roots()
                send_text(
                    chat_id,
                    t("msg.search_root_removed", msg=msg),
                    reply_markup=search_roots_keyboard(roots),
                )
            else:
                send_text(chat_id, t("msg.search_root_remove_failed", msg=msg))
            return True
        # /workspace_search_roots clear
        if sub == "clear":
            set_workspace_search_roots([], changed_by=user_id)
            send_text(
                chat_id,
                t("msg.search_roots_cleared"),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        send_text(chat_id, t("msg.unknown_subcommand", sub=sub))
        return True

    if txt.startswith("/workspace_list") or txt == "/workspaces":
        from workspace_registry import list_workspaces as _list_ws
        workspaces = _list_ws(include_inactive=True)
        if not workspaces:
            send_text(
                chat_id,
                t("msg.no_workspaces"),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        lines = [t("msg.workspace_list")]
        for ws in workspaces:
            flags = []
            if ws.get("is_default"):
                flags.append(t("msg.default_flag"))
            if not ws.get("active", True):
                flags.append(t("msg.disabled_flag"))
            flag_str = " [{}]".format(",".join(flags)) if flags else ""
            lines.append(
                t("msg.workspace_list_item",
                    label=ws.get("label", ws["id"]),
                    flags=flag_str,
                    id=ws["id"],
                    path=ws["path"],
                    concurrent=ws.get("max_concurrent", 1),
                )
            )
        send_text(chat_id, "\n\n".join(lines), reply_markup=back_to_menu_keyboard())
        return True

    if txt.startswith("/workspace_status") or txt == "/dispatch_status":
        from parallel_dispatcher import get_dispatcher_status
        status = get_dispatcher_status()
        workers = status.get("workers", {})
        if not workers:
            send_text(
                chat_id,
                t("msg.dispatcher_not_running"),
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        lines = [t("msg.dispatcher_status")]
        for ws_id, w in workers.items():
            state = t("msg.running") if w.get("running") else t("msg.stopped")
            busy = t("msg.busy", task=w.get("current_task_id", "")) if w.get("busy") else t("msg.idle")
            lines.append(
                "{label} ({state})\n  ID: {id}\n".format(
                    label=w.get("ws_label", ws_id),
                    state=state,
                    id=ws_id,
                ) + t("msg.worker_stats", busy=busy, queue=w.get("queue_size", 0), done=w.get("tasks_completed", 0), fail=w.get("tasks_failed", 0))
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


def create_task_for_workspace(chat_id: int, user_id: int, text: str, ws_id: str, ws_label: str) -> str:
    """Create a task explicitly targeting a specific workspace by ID."""
    task_id = new_task_id()
    action = infer_action(text)

    task = {
        "task_id": task_id,
        "chat_id": chat_id,
        "requested_by": user_id,
        "action": action,
        "text": text.strip(),
        "status": "pending",
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
        "target_workspace_id": ws_id,
        "target_workspace": ws_label,
    }

    task["task_code"] = register_task_created(task)
    save_json(task_file("pending", task_id), task)
    return task_id


def _evaluate_english_text(user_text: str) -> Optional[Dict]:
    """Call AI (claude -p) to evaluate user's English requirement text.

    Returns a dict with keys: original, corrected, issues, chinese_meaning.
    Returns None on failure.
    """
    import json as _json
    prompt = (
        'You are an English writing tutor. Evaluate the following requirement description '
        'written by a non-native speaker.\n\n'
        'User\'s text: "{}"\n\n'
        'Please respond in the following JSON format ONLY (no markdown, no extra text):\n'
        '{{\n'
        '  "original": "...",\n'
        '  "corrected": "...",\n'
        '  "issues": [{{"type": "grammar|vocabulary|expression", "original": "...", '
        '"suggestion": "...", "explanation": "..."}}],\n'
        '  "chinese_meaning": "..."\n'
        '}}'
    ).format(user_text.replace('"', '\\"'))
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout or "").strip()
        # Try to extract JSON from output (may be wrapped in markdown code block)
        if "```" in output:
            # Extract content between ``` markers
            parts = output.split("```")
            for part in parts[1:]:
                # Skip the language tag line if present
                lines = part.strip().split("\n", 1)
                if len(lines) > 1 and lines[0].strip().lower() in ("json", ""):
                    output = lines[1].strip()
                    break
                elif lines[0].strip().startswith("{"):
                    output = part.strip()
                    break
        data = _json.loads(output)
        if isinstance(data, dict) and "corrected" in data:
            return data
    except Exception:
        pass
    return None


def _auto_launch_queued_task(accepted_task: Dict, chat_id: int) -> None:
    """After a task is accepted, check if there are queued tasks for the same workspace.

    If so, promote the next one to pending and notify the user.
    """
    # Determine workspace ID of the accepted task
    ws_id = str(accepted_task.get("target_workspace_id", "")).strip()
    if not ws_id:
        ws_label = str(accepted_task.get("target_workspace", "")).strip()
        if ws_label:
            from workspace_registry import find_workspace_by_label
            ws = find_workspace_by_label(ws_label)
            if ws:
                ws_id = ws["id"]
    if not ws_id:
        # Try to resolve from workspace_registry by default
        from workspace_registry import get_default_workspace
        ws = get_default_workspace()
        if ws:
            ws_id = ws["id"]
    if not ws_id:
        return

    pending = queue_length(ws_id)
    if pending == 0:
        return

    promoted = promote_next_queued_task(ws_id)
    if not promoted:
        return

    from workspace_registry import get_workspace as _get_ws_auto
    ws = _get_ws_auto(ws_id)
    ws_label_str = ws.get("label", ws_id) if ws else ws_id
    remaining = queue_length(ws_id)

    send_text(
        chat_id,
        t("msg.queue_auto_start",
            ws=ws_label_str,
            code=promoted.get("task_code", "-"),
            task_id=promoted.get("task_id", ""),
            text=(promoted.get("text", "") or "")[:200],
            remaining=remaining,
        ),
        reply_markup=task_inline_keyboard(promoted.get("task_code", "-")),
    )
