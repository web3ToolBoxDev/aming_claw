"""
backends.py - AI execution backends: codex, claude, pipeline.

Contains:
- resolve_workspace, is_sensitive_path, task_touches_sensitive_path
- parse_wait_file_task, run_deterministic_wait_file_task
- build_codex_prompt, build_claude_prompt, build_pipeline_stage_prompt
- run_codex, run_claude, get_git_changed_files
- is_ack_only_message, has_execution_evidence, detect_noop_execution, detect_stage_noop
- run_codex_with_retry, run_claude_with_retry, run_stage_with_retry
- process_codex, process_claude, process_pipeline
"""
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from i18n import t
from utils import tasks_root
from workspace import resolve_active_workspace
from task_accept import finalize_codex_task, finalize_pipeline_task


# ── Workspace & safety ────────────────────────────────────────────────────────

def resolve_workspace() -> Path:
    base = resolve_active_workspace()
    # Keep executor in a dedicated search workspace by default.
    search_workspace = os.getenv("CODEX_SEARCH_WORKSPACE", "").strip()
    workspace = Path(search_workspace) if search_workspace else (base / "search-workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


def is_sensitive_path(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    blocked_names = {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".azure",
        ".config",
    }
    if lowered_parts.intersection(blocked_names):
        return True
    lowered_path = str(path).lower().replace("\\", "/")
    blocked_keywords = [
        "/id_rsa",
        "/id_ed25519",
        "/known_hosts",
        "/authorized_keys",
    ]
    return any(keyword in lowered_path for keyword in blocked_keywords)


def task_touches_sensitive_path(text: str) -> bool:
    lower_text = text.lower()
    patterns = [
        r"(^|[^a-z0-9])\.ssh([^a-z0-9]|$)",
        r"~[/\\]\.ssh",
        r"%userprofile%[/\\]\.ssh",
        r"/home/[^\s]+/\.ssh",
        r"c:[/\\]users[/\\][^\s]+[/\\]\.ssh",
        r"/etc/ssh",
        r"(^|[^a-z0-9])\.aws([^a-z0-9]|$)",
        r"(^|[^a-z0-9])\.gnupg([^a-z0-9]|$)",
        r"id_rsa",
        r"id_ed25519",
        r"known_hosts",
        r"authorized_keys",
    ]
    return any(re.search(pattern, lower_text) for pattern in patterns)


# ── Deterministic test task ───────────────────────────────────────────────────

def parse_wait_file_task(text: str) -> Optional[Dict]:
    """
    Match a deterministic test task pattern:
    在工作目录创建文件 <name>，写入当前时间；等待<sec>秒后再追加一行 <line>
    """
    s = (text or "").strip()
    m = re.search(
        r"在工作目录创建文件\s+([^\s，,；;]+)\s*[，,]\s*写入当前时间\s*[；;]\s*等待\s*(\d+)\s*秒后再追加一行\s+(.+)$",
        s,
        re.IGNORECASE,
    )
    if not m:
        return None
    file_name = m.group(1).strip()
    wait_sec = int(m.group(2).strip())
    append_line = m.group(3).strip()
    if not file_name or wait_sec < 0 or wait_sec > int(os.getenv("TASK_MAX_WAIT_SEC", "900")):
        return None
    # Basic filename guard.
    if any(x in file_name for x in ["..", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]):
        return None
    return {
        "file_name": file_name,
        "wait_sec": wait_sec,
        "append_line": append_line,
    }


def run_deterministic_wait_file_task(task: Dict) -> Optional[Dict]:
    enabled = os.getenv("TASK_ENABLE_DETERMINISTIC_WAIT_FILE", "1").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return None
    parsed = parse_wait_file_task(task.get("text", ""))
    if not parsed:
        return None
    workspace = resolve_workspace()
    target = (workspace / parsed["file_name"]).resolve()
    if workspace not in target.parents and target != workspace:
        raise RuntimeError("target file escapes workspace")
    t0 = time.perf_counter()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
    time.sleep(parsed["wait_sec"])
    with target.open("a", encoding="utf-8") as f:
        f.write(parsed["append_line"] + "\n")
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    summary = t("ai_prompt.wait_file_summary", sec=parsed["wait_sec"], file=str(target))
    return {
        "returncode": 0,
        "stdout": summary,
        "stderr": "",
        "last_message": summary,
        "elapsed_ms": elapsed_ms,
        "cmd": ["deterministic_wait_file_task"],
        "timeout_retries": 0,
        "workspace": str(workspace),
        "git_changed_files": [str(target.relative_to(workspace))],
        "attempt_tag": "deterministic",
        "attempt_count": 1,
        "noop_reason": None,
    }


# ── Prompt builders ───────────────────────────────────────────────────────────

def _get_task_text(task: Dict) -> str:
    """Return enhanced text if available (retry with rejection context), else original."""
    return str(task.get("_retry_enhanced_text") or task.get("text") or "")


def _image_attachment_hint(task: Dict) -> str:
    """Build image attachment hint text for prompts."""
    images = task.get("images") or []
    if not images:
        return ""
    return "\n\n[附件: 图片 {} 张, 请参考任务目录 attachments/ 下的文件]".format(len(images))


def encode_image_base64(path: str) -> str:
    """Read an image file and return its base64 encoding."""
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def build_codex_prompt(task: Dict) -> str:
    text = _get_task_text(task)
    prompt = t("ai_prompt.codex_system", task_id=task["task_id"], text=text)
    prompt += _image_attachment_hint(task)
    return prompt


def build_claude_prompt(task: Dict) -> str:
    text = _get_task_text(task)
    prompt = t("ai_prompt.claude_system", task_id=task["task_id"], text=text)
    prompt += _image_attachment_hint(task)
    return prompt


# ── Git helpers ───────────────────────────────────────────────────────────────

def get_git_changed_files(workspace: Path) -> Optional[List[str]]:
    try:
        proc = subprocess.run(
            # Restrict status to current workspace subtree, avoid counting
            # unrelated pre-existing changes from repository root.
            ["git", "-C", str(workspace), "status", "--porcelain", "--", "."],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").splitlines()
    changed: List[str] = []
    for line in out:
        if not line.strip():
            continue
        # porcelain format: XY<space>path or XY<space>old -> new
        if len(line) >= 4:
            changed.append(line[3:].strip())
    return changed


# ── Backend runners ───────────────────────────────────────────────────────────

def run_codex(task: Dict, extra_guard: str = "", attempt_tag: str = "",
              prompt_override: Optional[str] = None, model_override: str = "") -> Dict:
    # Use the actual project directory (not the isolated search-workspace)
    # so Codex can read and modify project source files.
    workspace = resolve_active_workspace()
    if is_sensitive_path(workspace):
        raise RuntimeError("workspace is sensitive and not allowed: {}".format(workspace))
    if task_touches_sensitive_path(task.get("text", "")):
        raise RuntimeError("task rejected: request touches sensitive paths (e.g. .ssh)")
    timeout_sec = int(os.getenv("CODEX_TIMEOUT_SEC", "1200"))
    max_retries = int(os.getenv("CODEX_TIMEOUT_RETRIES", "1"))
    model = model_override.strip() if model_override else os.getenv("CODEX_MODEL", "").strip()
    codex_bin = os.getenv("CODEX_BIN", "").strip()
    if not codex_bin:
        codex_bin = "codex.cmd" if os.name == "nt" else "codex"
    dangerous = os.getenv("CODEX_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}

    suffix = (("." + attempt_tag) if attempt_tag else "") + ".last_message.txt"
    output_last = tasks_root() / "logs" / (task["task_id"] + suffix)
    prompt = prompt_override if prompt_override is not None else build_codex_prompt(task)
    if extra_guard:
        prompt = prompt + "\n\n" + extra_guard.strip() + "\n"
    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
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

    # Snapshot git state before execution (to diff against after)
    before_changed: set = set(get_git_changed_files(workspace) or [])

    last_timeout: Optional[str] = None
    proc = None
    t0 = time.perf_counter()
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_sec, check=False)
            break
        except subprocess.TimeoutExpired:
            last_timeout = "timeout after {}s (attempt {}/{})".format(
                timeout_sec, attempt + 1, max_retries + 1
            )
            if attempt >= max_retries:
                raise
            time.sleep(min(5, 1 + attempt))
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if proc is None:
        raise RuntimeError(last_timeout or "codex did not start")

    last_message = ""
    if output_last.exists():
        try:
            last_message = output_last.read_text(encoding="utf-8")
        except Exception:
            last_message = ""

    # Only report files newly changed by this task (not pre-existing dirty files)
    after_changed = get_git_changed_files(workspace) or []
    new_changed = [f for f in after_changed if f not in before_changed]

    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-12000:],
        "stderr": (proc.stderr or "")[-12000:],
        "last_message": last_message.strip(),
        "elapsed_ms": elapsed_ms,
        "cmd": cmd,
        "timeout_retries": max_retries,
        "workspace": str(workspace),
        "git_changed_files": new_changed,
        "attempt_tag": attempt_tag,
    }


def _extract_text_from_claude_json(raw: str) -> str:
    """Extract assistant text content from Claude CLI JSON output.

    Handles single message object or list of messages.
    Skips tool_use content blocks, extracts only text blocks.
    Returns empty string on any parse error (never raises).
    """
    if not raw or not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""

    # Normalize to list of messages
    if isinstance(data, dict):
        messages = [data]
    elif isinstance(data, list):
        messages = data
    else:
        return ""

    texts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
    return "\n".join(texts)


def _base_claude_cmd(extra_flags: Optional[List[str]] = None) -> List[str]:
    """Return base Claude CLI command list: [claude_bin, "-p", --model X ...]."""
    from config import get_claude_model
    model = get_claude_model() or os.getenv("CLAUDE_MODEL", "").strip()
    claude_bin = os.getenv("CLAUDE_BIN", "").strip()
    if not claude_bin:
        import shutil
        claude_bin = (
            shutil.which("claude.cmd") or shutil.which("claude")
            or ("claude.cmd" if os.name == "nt" else "claude")
        )
    cmd = [claude_bin, "-p"]
    if model:
        cmd += ["--model", model]
    if extra_flags:
        cmd += extra_flags
    return cmd


def run_claude(task: Dict, extra_guard: str = "", attempt_tag: str = "",
               prompt_override: Optional[str] = None,
               output_format: str = "text") -> Dict:
    # Claude CLI needs the actual project directory (not the isolated search-workspace)
    # so it can read and modify project source files.
    workspace = resolve_active_workspace()
    if is_sensitive_path(workspace):
        raise RuntimeError("workspace is sensitive and not allowed: {}".format(workspace))
    if task_touches_sensitive_path(task.get("text", "")):
        raise RuntimeError("task rejected: request touches sensitive paths (e.g. .ssh)")
    timeout_sec = int(os.getenv("CLAUDE_TIMEOUT_SEC", "1200"))
    max_retries = int(os.getenv("CLAUDE_TIMEOUT_RETRIES", "1"))
    dangerous = os.getenv("CLAUDE_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}

    prompt = prompt_override if prompt_override is not None else build_claude_prompt(task)
    if extra_guard:
        prompt = prompt + "\n\n" + extra_guard.strip() + "\n"
    # Pass prompt via stdin to avoid Windows cmd.exe truncating multi-line arguments.
    fmt = output_format if output_format in ("text", "json") else "text"
    extra = ["--output-format", fmt]
    if dangerous:
        extra += ["--dangerously-skip-permissions"]
    cmd = _base_claude_cmd(extra_flags=extra)

    # Append --image flags for task images (Claude CLI vision support)
    for img in (task.get("images") or []):
        img_path = img.get("local_path", "")
        if img_path and Path(img_path).exists():
            cmd += ["--image", img_path]

    # Strip env vars that interfere with Claude CLI:
    # - CLAUDECODE/CLAUDE_CODE_*: prevents "nested session" rejection
    # - ANTHROPIC_API_KEY: forces CLI to use API credits instead of Max subscription OAuth
    _claude_env = {k: v for k, v in os.environ.items()
                   if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
                                "ANTHROPIC_API_KEY")}

    # Snapshot pre-existing uncommitted files so we only report NEW changes after Claude runs.
    before_changed: set = set(get_git_changed_files(workspace) or [])

    last_timeout: Optional[str] = None
    proc = None
    t0 = time.perf_counter()
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
                cwd=str(workspace),
                env=_claude_env,
            )
            break
        except subprocess.TimeoutExpired:
            last_timeout = "timeout after {}s (attempt {}/{})".format(
                timeout_sec, attempt + 1, max_retries + 1
            )
            if attempt >= max_retries:
                raise
            time.sleep(min(5, 1 + attempt))
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if proc is None:
        raise RuntimeError(last_timeout or "claude did not start")

    # Only count files that Claude actually changed (exclude pre-existing dirty files).
    after_changed = get_git_changed_files(workspace) or []
    new_changed = [f for f in after_changed if f not in before_changed]

    raw_stdout = (proc.stdout or "").strip()
    # For JSON output format, extract assistant text from JSON structure.
    # Falls back to raw stdout if JSON parsing fails.
    if fmt == "json" and raw_stdout:
        extracted = _extract_text_from_claude_json(raw_stdout)
        stdout = extracted.strip() if extracted else raw_stdout
    else:
        stdout = raw_stdout
    return {
        "returncode": proc.returncode,
        "stdout": stdout[-12000:],
        "stderr": (proc.stderr or "")[-12000:],
        "last_message": stdout[-12000:],
        "elapsed_ms": elapsed_ms,
        "cmd": cmd,
        "timeout_retries": max_retries,
        "workspace": str(workspace),
        "git_changed_files": new_changed,
        "attempt_tag": attempt_tag,
    }


# ── Noop detection ────────────────────────────────────────────────────────────

def is_ack_only_message(message: str) -> bool:
    msg = (message or "").strip()
    if not msg:
        return True
    if len(msg) > 160:
        return False
    # If message contains structured execution evidence,
    # it is NOT ack-only — it's the expected output contract from task execution.
    if has_execution_evidence(msg):
        return False
    lowered = msg.lower()
    # Chinese ack patterns
    if ("后续" in msg and "执行" in msg) or ("直接告诉我" in msg) or ("请告诉我" in msg):
        return True
    if ("直接执行模式" in msg) or ("进入直接执行模式" in msg):
        return True
    # English ack patterns
    if "i will" in lowered and "execute" in lowered:
        return True
    if "please send" in lowered or "send me the task" in lowered or "provide the task" in lowered:
        return True
    # Asking user to send/provide the task — model hasn't seen or acted on it.
    if "请发送" in msg or "发送任务" in msg or "请提供任务" in msg:
        return True
    patterns = [
        # Chinese ack patterns
        r"^明白[。.!]?$",
        r"^收到[。.!]?$",
        r"^好的[。.!]?$",
        r"^已了解[。.!]?$",
        r"^了解[。.!]?$",
        r"^明白。后续我会直接执行你给的任务[。.!]?$",
        r"^收到。后续我将直接执行任务[。.!]?$",
        r"^收到。后续我会直接执行任务[。.!]?$",
        r"^收到。后续我会直接执行你给的任务[。.!]?$",
        r"^收到。后续我将直接执行你给的任务[。.!]?$",
        r"^已了解。后续我会直接执行你下达的任务[。.!]?$",
        r"^已进入直接执行模式[。.!]?$",
        r"^已切换为直接执行模式[。.!]?$",
        r"^收到。接下来我会直接执行你给的具体任务[，,]不复述[，,]不反问[。.!]?$",
        r"^请直接告诉我(你)?(的)?问题[。.!]?$",
        # English ack patterns
        r"^understood[.!]?$",
        r"^got it[.!]?$",
        r"^received[.!]?$",
        r"^ok[.!]?$",
        r"^okay[.!]?$",
        r"^sure[.!]?$",
        r"^will do[.!]?$",
        r"^ready to execute[.!]?$",
        r"^i('m| am) ready[.!]?$",
        r"^acknowledged[.!]?$",
    ]
    return any(re.match(p, msg, re.IGNORECASE) for p in patterns)


def has_execution_evidence(text: str) -> bool:
    msg = (text or "").strip()
    if not msg:
        return False
    # Preferred structured evidence from prompt contract (Chinese).
    if ("已执行步骤" in msg and "修改文件" in msg and "后续建议" in msg):
        return True
    # Preferred structured evidence from prompt contract (English).
    low = msg.lower()
    if ("steps executed" in low and "modified files" in low and "follow-up" in low):
        return True
    # Fallback evidence: explicit command/file modification markers (bilingual).
    markers = [
        "执行了",
        "已完成以下",
        "修改了",
        "创建了",
        "executed",
        "completed the following",
        "modified",
        "created",
        "apply_patch",
        "git diff",
        "pytest",
        ".py",
        ".ts",
        ".js",
        ".ps1",
    ]
    return any(m in msg for m in markers)


def detect_noop_execution(run: Dict) -> Optional[str]:
    if int(run.get("returncode", 1)) != 0:
        return None
    last_message = (run.get("last_message") or "").strip()
    stdout = (run.get("stdout") or "").strip()
    changed = run.get("git_changed_files")
    has_changes = isinstance(changed, list) and len(changed) > 0
    # Ack-only last_message is considered noop regardless of unrelated workspace changes.
    if is_ack_only_message(last_message):
        return "codex returned acknowledgement-only response without concrete execution"
    # If there are real file changes, accept the result even if stdout is sparse.
    if has_changes:
        return None
    if is_ack_only_message(stdout):
        return "codex stdout is acknowledgement-only and no concrete execution output"
    strict_acceptance = os.getenv("TASK_STRICT_ACCEPTANCE", "1").strip().lower() not in {"0", "false", "no"}
    if strict_acceptance:
        # For /task, require explicit execution evidence when no file changes are detected.
        if not has_execution_evidence(last_message) and not has_execution_evidence(stdout):
            return "no execution evidence found (no changed files and no structured execution summary)"
    return None


# ── Retry wrappers ────────────────────────────────────────────────────────────

def run_codex_with_retry(task: Dict) -> Dict:
    max_noop_retries = int(os.getenv("TASK_NOOP_RETRIES", "1"))
    run = run_codex(task, attempt_tag="attempt1")
    noop_reason = detect_noop_execution(run)
    if not noop_reason:
        run["noop_reason"] = None
        run["attempt_count"] = 1
        return run

    last_reason = noop_reason
    for retry_idx in range(1, max_noop_retries + 1):
        guard = t("ai_prompt.retry_guard")
        run = run_codex(task, extra_guard=guard, attempt_tag="retry{}".format(retry_idx))
        noop_reason = detect_noop_execution(run)
        if not noop_reason:
            run["noop_reason"] = None
            run["attempt_count"] = retry_idx + 1
            run["noop_retry_last_reason"] = last_reason
            return run
        last_reason = noop_reason

    run["noop_reason"] = last_reason
    run["attempt_count"] = max_noop_retries + 1
    return run


def run_claude_with_retry(task: Dict) -> Dict:
    max_noop_retries = int(os.getenv("TASK_NOOP_RETRIES", "1"))
    run = run_claude(task, attempt_tag="attempt1")
    noop_reason = detect_noop_execution(run)
    if not noop_reason:
        run["noop_reason"] = None
        run["attempt_count"] = 1
        return run

    last_reason = noop_reason
    for retry_idx in range(1, max_noop_retries + 1):
        guard = t("ai_prompt.retry_guard")
        run = run_claude(task, extra_guard=guard, attempt_tag="retry{}".format(retry_idx))
        noop_reason = detect_noop_execution(run)
        if not noop_reason:
            run["noop_reason"] = None
            run["attempt_count"] = retry_idx + 1
            run["noop_retry_last_reason"] = last_reason
            return run
        last_reason = noop_reason

    run["noop_reason"] = last_reason
    run["attempt_count"] = max_noop_retries + 1
    return run


# ── Pipeline ──────────────────────────────────────────────────────────────────

# Stage names treated as "analysis" (text output, no code execution required)
_ANALYSIS_STAGES = {"plan", "planning", "verify", "verification", "test", "testing",
                    "review", "check", "analyse", "analysis", "audit",
                    "pm", "qa"}

_STAGE_ROLE_PROMPT_KEYS = {
    "plan": "ai_prompt.stage_plan",
    "code": "ai_prompt.stage_code",
    "implement": "ai_prompt.stage_implement",
    "verify": "ai_prompt.stage_verify",
    "test": "ai_prompt.stage_test",
    "review": "ai_prompt.stage_review",
    "pm": "ai_prompt.stage_pm",
    "dev": "ai_prompt.stage_dev",
    "qa": "ai_prompt.stage_qa",
}


_CODEX_EXEC_HINT = (
    "\n\n[IMPORTANT: Execute the task directly by modifying files. "
    "Do NOT reply with acknowledgement only.]"
)


def build_pipeline_stage_prompt(task: Dict, stage_name: str, context: str,
                                is_codex_routed: bool = False) -> str:
    """Build a stage-specific prompt incorporating accumulated context from prior stages.

    When *is_codex_routed* is True and the stage is a code-execution stage,
    a short Codex-specific execution hint is appended to reduce ack-only responses.
    """
    key = _STAGE_ROLE_PROMPT_KEYS.get(stage_name.lower())
    role_intro = t(key) if key else t("ai_prompt.stage_default")
    parts = [role_intro.rstrip()]
    if context.strip():
        parts.append(t("ai_prompt.prior_stages") + "\n" + context.strip()[:6000])
    parts.append(
        "{}: {}\n{}: {}".format(
            t("ai_prompt.task_id_label"), task["task_id"],
            t("ai_prompt.task_content_label"), _get_task_text(task))
    )
    prompt = "\n\n".join(parts)
    # Append image hint for PM stage (first stage that analyzes requirements)
    if stage_name.lower() in ("pm", "plan"):
        prompt += _image_attachment_hint(task)
    # Append Codex execution hint for code-execution stages routed to OpenAI/Codex
    if is_codex_routed and stage_name.lower() not in _ANALYSIS_STAGES:
        prompt += _CODEX_EXEC_HINT
    return prompt


def detect_stage_noop(run: Dict, stage: Dict) -> Optional[str]:
    """Stage-aware noop detection.
    Analysis stages (plan/verify/test) only need real text content.
    Code stages use the full execution evidence check.
    """
    stage_name = (stage.get("name") or "").lower().strip()
    if stage_name in _ANALYSIS_STAGES:
        msg = (run.get("last_message") or run.get("stdout") or "").strip()
        if not msg or is_ack_only_message(msg):
            return "stage '{}' returned empty or acknowledgement-only output".format(stage_name)
        if len(msg) < 50:
            return "stage '{}' output too short ({} chars)".format(stage_name, len(msg))
        return None
    return detect_noop_execution(run)


def _infer_provider(model: str, provider: str) -> str:
    """Infer provider from model id when provider is not explicitly set."""
    p = (provider or "").strip().lower()
    if p in {"anthropic", "openai"}:
        return p
    m = (model or "").strip().lower()
    if not m:
        return ""
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return ""


def run_stage_with_retry(task: Dict, stage: Dict, prompt: str, stage_idx: int) -> Dict:
    """Run one pipeline stage with noop retry logic.

    Supports per-stage model/provider override via stage dict keys:
      stage["model"]    - model id to use for this stage
      stage["provider"] - provider for this stage ("anthropic"|"openai"|"")
    """
    backend = stage.get("backend", "codex")
    stage_name = stage.get("name", "stage{}".format(stage_idx))
    stage_model = (stage.get("model") or "").strip()
    stage_provider = (stage.get("provider") or "").strip()
    max_noop_retries = int(os.getenv("TASK_NOOP_RETRIES", "1"))
    # QA is the final pipeline stage — a noop wastes all prior stages' work.
    # Give QA at least 2 retries (3 total attempts).
    if stage_name == "qa":
        max_noop_retries = max(2, max_noop_retries)
    base_tag = "s{}_{}".format(stage_idx, stage_name)

    from config import get_claude_model, get_model_provider
    global_model = (get_claude_model() or "").strip()
    global_provider = _infer_provider(global_model, get_model_provider())

    # Determine actual model/provider for this stage
    if backend == "openai":
        default_openai_model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o"
        if stage_model:
            actual_model = stage_model
        elif global_provider == "openai" and global_model:
            actual_model = global_model
        else:
            actual_model = default_openai_model
        actual_provider = "openai"
    elif stage_model:
        actual_model = stage_model
        actual_provider = _infer_provider(stage_model, stage_provider)
    else:
        actual_model = global_model or "(default)"
        actual_provider = global_provider or "(default)"

    logger.info("[Pipeline] Stage '%s' using model=%s, provider=%s",
                stage_name, actual_model, actual_provider)

    # Analysis stages (verify/test/qa/plan) should use Claude CLI, not Codex exec.
    # Codex CLI 'exec' mode is designed for code execution and returns ACK-only
    # for analysis prompts. Claude CLI handles analysis tasks properly.
    is_analysis = stage_name in _ANALYSIS_STAGES

    def _run(p: str, tag: str) -> Dict:
        # For analysis stages: prefer Claude CLI which handles analysis well.
        # Codex CLI 'exec' mode only acknowledges analysis prompts.
        # Use JSON output format for analysis stages to capture assistant text
        # even when the model ends with a tool call (no trailing text block).
        if is_analysis:
            return run_claude(task, attempt_tag=tag, prompt_override=p,
                              output_format="json")

        # Code execution stages: route based on provider/backend.
        # openai provider -> Codex CLI (uses subscription, no API key needed)
        # anthropic provider -> Claude CLI (uses Max subscription)
        if backend == "openai":
            return run_codex(task, attempt_tag=tag, prompt_override=p,
                             model_override=actual_model)
        if backend == "claude":
            # Apply per-stage model/provider override if configured.
            if stage_model:
                from config import set_claude_model
                orig_model = global_model
                orig_provider = get_model_provider()
                eff_provider = _infer_provider(stage_model, stage_provider)
                try:
                    set_claude_model(stage_model, provider=eff_provider)
                    if eff_provider == "openai":
                        # OpenAI model -> use Codex CLI with specific model
                        return run_codex(task, attempt_tag=tag, prompt_override=p,
                                         model_override=stage_model)
                    return run_claude(task, attempt_tag=tag, prompt_override=p)
                finally:
                    set_claude_model(orig_model, provider=orig_provider)
            if global_provider == "openai":
                return run_codex(task, attempt_tag=tag, prompt_override=p,
                                 model_override=global_model)
            return run_claude(task, attempt_tag=tag, prompt_override=p)
        return run_codex(task, attempt_tag=tag, prompt_override=p)

    run = _run(prompt, base_tag + "_a1")
    noop_reason = detect_stage_noop(run, stage)
    if not noop_reason:
        run["noop_reason"] = None
        run["attempt_count"] = 1
        return run

    last_reason = noop_reason
    for retry_idx in range(1, max_noop_retries + 1):
        guard = t("ai_prompt.stage_retry_guard")
        run = _run(prompt + "\n\n" + guard, base_tag + "_r{}".format(retry_idx))
        noop_reason = detect_stage_noop(run, stage)
        if not noop_reason:
            run["noop_reason"] = None
            run["attempt_count"] = retry_idx + 1
            run["noop_retry_last_reason"] = last_reason
            return run
        last_reason = noop_reason

    # ── Fallback: if routed to OpenAI/Codex and still noop, try Claude CLI ──
    uses_codex = actual_provider == "openai" or (
        backend == "openai") or (
        backend == "codex") or (
        backend == "claude" and stage_model and _infer_provider(stage_model, stage_provider) == "openai") or (
        backend == "claude" and not stage_model and global_provider == "openai")

    if uses_codex and not is_analysis:
        fallback_model = "claude-sonnet-4-6"
        logger.warning(
            "[STAGE_FALLBACK] Stage '%s' Codex/OpenAI noop after %d retries — "
            "falling back to Claude CLI (model=%s)",
            stage_name, max_noop_retries, fallback_model)
        fallback_run = run_claude(task, attempt_tag=base_tag + "_fallback_claude",
                                  prompt_override=prompt)
        fallback_noop = detect_stage_noop(fallback_run, stage)
        if not fallback_noop:
            fallback_run["noop_reason"] = None
            fallback_run["attempt_count"] = max_noop_retries + 2
            fallback_run["noop_retry_last_reason"] = last_reason
            fallback_run["fallback_used"] = True
            fallback_run["original_model"] = actual_model
            fallback_run["fallback_model"] = fallback_model
            logger.info("[STAGE_FALLBACK] Stage '%s' fallback to Claude succeeded", stage_name)
            return fallback_run
        logger.error("[STAGE_FALLBACK] Stage '%s' fallback to Claude also failed: %s",
                     stage_name, fallback_noop)
        fallback_run["noop_reason"] = fallback_noop
        fallback_run["attempt_count"] = max_noop_retries + 2
        fallback_run["fallback_used"] = True
        fallback_run["original_model"] = actual_model
        fallback_run["fallback_model"] = fallback_model
        return fallback_run

    run["noop_reason"] = last_reason
    run["attempt_count"] = max_noop_retries + 1
    return run


# ── Direct API execution (Anthropic / OpenAI) ─────────────────────────────────

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
        "{} API error (HTTP {}): {}".format(provider, resp.status_code, detail or "unknown error")
    )


def run_via_api(
    task: Dict,
    prompt_override: Optional[str] = None,
    model_override: str = "",
    provider_override: str = "",
) -> Dict:
    """Call Anthropic or OpenAI chat API directly (no CLI required).
    Provider is determined from the stored model_provider config.
    Returns a run-dict compatible with finalize_codex_task.
    """
    import requests as _req
    from config import get_claude_model, get_model_provider

    model = (model_override or get_claude_model()).strip()
    provider = (provider_override or get_model_provider()).strip().lower()
    prompt = prompt_override if prompt_override is not None else build_claude_prompt(task)
    t0 = time.perf_counter()

    try:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not set")
            resp = _req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key,
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 4096},
                timeout=120,
            )
            if resp.status_code >= 400:
                _raise_api_error("OpenAI", resp)
            content = resp.json()["choices"][0]["message"]["content"]
        else:
            # Default: Anthropic Messages API
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            payload = {
                "model": model or "claude-sonnet-4-6",
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key,
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=120,
            )
            if resp.status_code >= 400:
                _raise_api_error("Anthropic", resp)
            content = resp.json()["content"][0]["text"]

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        workspace = resolve_workspace()
        return {
            "stdout": content,
            "stderr": "",
            "last_message": content,
            "returncode": 0,
            "elapsed_ms": elapsed_ms,
            "cmd": ["api", provider, model],
            "timeout_retries": 0,
            "workspace": str(workspace),
            "git_changed_files": get_git_changed_files(workspace),
            "attempt_tag": "api",
            "noop_reason": None,
            "attempt_count": 1,
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "stdout": "",
            "stderr": str(exc),
            "last_message": "",
            "returncode": 1,
            "elapsed_ms": elapsed_ms,
            "cmd": ["api", provider, model],
            "timeout_retries": 0,
            "workspace": "",
            "git_changed_files": None,
            "attempt_tag": "api",
            "noop_reason": None,
            "attempt_count": 1,
        }


# ── Process functions ─────────────────────────────────────────────────────────

def process_codex(task: Dict, processing: Path) -> Dict:
    deterministic = run_deterministic_wait_file_task(task)
    run = deterministic if deterministic is not None else run_codex_with_retry(task)
    noop_reason = run.get("noop_reason")
    status = "completed" if run["returncode"] == 0 and not noop_reason else "failed"
    error = noop_reason if status == "failed" and noop_reason else None
    return finalize_codex_task(task, processing, run, status, error=error)


def process_claude(task: Dict, processing: Path) -> Dict:
    from config import get_model_provider
    provider = get_model_provider()
    # Anthropic models: use Claude CLI (has tool-use, Max subscription).
    # OpenAI models: use Codex CLI (has tool-use, Codex subscription).
    if provider == "openai":
        run = run_codex_with_retry(task)
    else:
        # "anthropic" or unset: use Claude CLI with model from config
        run = run_claude_with_retry(task)
    noop_reason = run.get("noop_reason")
    status = "completed" if run["returncode"] == 0 and not noop_reason else "failed"
    error = noop_reason if status == "failed" and noop_reason else None
    return finalize_codex_task(task, processing, run, status, error=error)


def _summarize_stage_for_qa(stage_name: str, output: str, max_chars: int = 1500) -> str:
    """Extract compact summary from a stage output for QA context.

    Instead of passing the full raw output (up to 6000 chars per stage),
    extract the most relevant sections to keep the QA prompt concise
    enough for Codex CLI exec mode.
    """
    if not output or not output.strip():
        return t("ai_prompt.no_output")
    lines = output.strip().splitlines()

    if stage_name == "pm":
        # Extract acceptance criteria / verification items
        result_lines = []
        in_section = False
        for line in lines:
            # Look for acceptance criteria sections
            if any(kw in line for kw in ["验收条件", "验收标准", "子任务", "需求", "具体改动"]):
                in_section = True
            if in_section:
                result_lines.append(line)
            # Also grab section headers
            elif line.strip().startswith(("##", "###", "T", "- ")):
                result_lines.append(line)
        if result_lines:
            text = "\n".join(result_lines)[:max_chars]
            return text
        # Fallback: last portion (usually has summary)
        return "\n".join(lines[-30:])[:max_chars]

    if stage_name == "dev":
        # Extract: file change list, completion status
        result_lines = []
        for i, line in enumerate(lines):
            if any(kw in line for kw in ["修改文件", "变更说明", "已执行步骤",
                                          "子任务", "实现状态", "文件", "| ---"]):
                # Grab this line and following context
                result_lines.extend(lines[i:i + 20])
                break
        if not result_lines:
            # Fallback: last lines (usually summary)
            result_lines = lines[-20:]
        return "\n".join(result_lines)[:max_chars]

    if stage_name == "test":
        # Extract: pass/fail results, key numbers
        result_lines = []
        for i, line in enumerate(lines):
            low = line.lower()
            if any(kw in low for kw in ["passed", "failed", "error", "ok",
                                         "通过", "失败", "结论", "结果",
                                         "ran ", "test"]):
                start = max(0, i - 1)
                result_lines.extend(lines[start:start + 5])
        if result_lines:
            return "\n".join(result_lines)[:max_chars]
        return "\n".join(lines[-10:])[:max_chars]

    # Generic fallback
    return "\n".join(lines[:20])[:max_chars]


def _build_role_context(stage_name: str, stage_outputs: Dict[str, str]) -> str:
    """Build role-aware context for a pipeline stage.

    PM output is base context for all subsequent stages.
    Dev output is added for test and qa.
    Test output is added for qa.
    For QA: uses summarized versions to keep prompt concise for Codex CLI.
    """
    parts = []
    pm_label = t("role.pm")
    dev_label = t("role.dev")
    test_label = t("role.test")

    if stage_name == "dev":
        if "pm" in stage_outputs:
            parts.append("{}\n{}".format(
                t("ai_prompt.role_pm_requirements", label=pm_label), stage_outputs["pm"]))
    elif stage_name == "test":
        if "pm" in stage_outputs:
            parts.append("{}\n{}".format(
                t("ai_prompt.role_pm_requirements", label=pm_label), stage_outputs["pm"]))
        if "dev" in stage_outputs:
            parts.append("{}\n{}".format(
                t("ai_prompt.role_dev_code", label=dev_label), stage_outputs["dev"]))
    elif stage_name == "qa":
        # QA gets summarized context to avoid Codex CLI prompt truncation
        if "pm" in stage_outputs:
            pm_summary = _summarize_stage_for_qa("pm", stage_outputs["pm"])
            parts.append("{}\n{}".format(
                t("ai_prompt.role_pm_acceptance", label=pm_label), pm_summary))
        if "dev" in stage_outputs:
            dev_summary = _summarize_stage_for_qa("dev", stage_outputs["dev"])
            parts.append("{}\n{}".format(
                t("ai_prompt.role_dev_summary", label=dev_label), dev_summary))
        if "test" in stage_outputs:
            test_summary = _summarize_stage_for_qa("test", stage_outputs["test"])
            parts.append("{}\n{}".format(
                t("ai_prompt.role_test_result", label=test_label), test_summary))

    return "\n\n".join(parts)


def _is_role_pipeline(stages: List[Dict]) -> bool:
    """Check if stages form a role pipeline (pm → dev → test → qa)."""
    from config import ROLE_PIPELINE_ORDER
    names = [s.get("name", "") for s in stages]
    return names == ROLE_PIPELINE_ORDER


def _load_pipeline_provider_config() -> Dict:
    """Load pipeline multi-provider config (YAML + env overrides).

    Returns the effective config dict, or empty dict if no config is available.
    Logs errors but does not raise to avoid breaking pipeline execution.
    """
    try:
        from pipeline_config import get_effective_pipeline_config
        config = get_effective_pipeline_config()
        if config:
            logger.info("[Pipeline] %s", t("log.pipeline_config_loaded"))
        return config
    except ValueError as exc:
        logger.error("[Pipeline] %s", t("log.pipeline_config_failed", err=exc))
        return {}
    except Exception as exc:
        logger.debug("[Pipeline] %s", t("log.pipeline_config_debug", err=exc))
        return {}


def process_pipeline(task: Dict, processing: Path) -> Dict:
    """Execute a multi-stage pipeline: each stage feeds its output as context to the next.

    For role pipelines (pm → dev → test → qa), uses structured context passing
    where each role receives targeted context from prior roles.

    Provider/model resolution priority (highest → lowest):
      1. Environment variable (PIPELINE_ROLE_{ROLE}_PROVIDER/MODEL)
      2. YAML config file (pipeline_config.yaml)
      3. Runtime config (agent_config.json role_pipeline_stages)
      4. Global default provider/model
    """
    from config import get_pipeline_stages, get_role_pipeline_stages  # imported here to avoid circular

    stages = get_pipeline_stages()

    # If stages form a role pipeline, merge in per-role model/provider config
    pipeline_routing: Optional[List[Dict]] = None
    if stages and _is_role_pipeline(stages):
        # Try loading pipeline config from YAML + env overrides first
        pipeline_cfg = _load_pipeline_provider_config()
        if pipeline_cfg:
            from pipeline_config import apply_config_to_stages, log_role_routing
            stages = apply_config_to_stages(stages, pipeline_cfg)
            pipeline_routing = log_role_routing(stages, pipeline_cfg)
            logger.info("[Pipeline] 角色路由表: %s",
                        "; ".join("{role}→{provider}/{model}({source})".format(**r)
                                  for r in pipeline_routing))
        else:
            # Fall back to runtime config (agent_config.json)
            role_stages = get_role_pipeline_stages()
            role_config = {s["name"]: s for s in role_stages if "name" in s}
            for stage in stages:
                name = stage.get("name", "")
                if name in role_config:
                    rc = role_config[name]
                    if rc.get("model"):
                        stage["model"] = rc["model"]
                        stage["provider"] = rc.get("provider", "")
    if not stages:
        # No pipeline configured — fall back to single backend
        backend = task.get("_pipeline_fallback_backend", "codex")
        return process_codex(task, processing) if backend != "claude" else process_claude(task, processing)

    is_role = _is_role_pipeline(stages)
    stage_results: List[Dict] = []
    stages_model_info: List[Dict] = []
    context = ""  # accumulates prior stage outputs (generic pipeline)
    stage_outputs: Dict[str, str] = {}  # role name → output (role pipeline)

    for i, stage in enumerate(stages):
        stage_name = stage.get("name", "stage{}".format(i + 1))
        backend = stage.get("backend", "codex")
        stage_model = (stage.get("model") or "").strip()
        stage_provider = (stage.get("provider") or "").strip()

        # Record model info for this stage
        if stage_model:
            model_id = stage_model
            provider_id = stage_provider
        else:
            from config import get_claude_model as _gcm, get_model_provider as _gmp
            model_id = _gcm() or "(default)"
            provider_id = _gmp() or "(default)"
        stages_model_info.append({
            "stage": stage_name,
            "model": model_id,
            "provider": provider_id,
        })

        # Determine if this stage will route to Codex/OpenAI
        eff_provider = _infer_provider(stage_model, stage_provider) if stage_model else ""
        from config import get_model_provider as _gmp2
        global_prov = _gmp2() or ""
        is_codex_routed = (
            backend == "openai" or backend == "codex" or
            (backend == "claude" and stage_model and eff_provider == "openai") or
            (backend == "claude" and not stage_model and global_prov == "openai")
        )

        # Build context based on pipeline type
        if is_role:
            role_context = _build_role_context(stage_name, stage_outputs)
            prompt = build_pipeline_stage_prompt(task, stage_name, role_context,
                                                 is_codex_routed=is_codex_routed)
        else:
            prompt = build_pipeline_stage_prompt(task, stage_name, context,
                                                 is_codex_routed=is_codex_routed)

        run = run_stage_with_retry(task, stage, prompt, stage_idx=i + 1)

        # Record fallback info in stages_model_info if fallback occurred
        if run.get("fallback_used"):
            stages_model_info[-1]["fallback_used"] = True
            stages_model_info[-1]["original_model"] = run.get("original_model", model_id)
            stages_model_info[-1]["fallback_model"] = run.get("fallback_model", "")

        stage_results.append({
            "stage": stage_name,
            "backend": backend,
            "stage_index": i + 1,
            "run": run,
            "model": model_id,
            "provider": provider_id,
        })

        # Accumulate output for next stage
        output = (run.get("last_message") or run.get("stdout") or "").strip()
        if output:
            if is_role:
                stage_outputs[stage_name] = output[:6000]
            context += "{}\n{}\n\n".format(
                t("ai_prompt.stage_section", name=stage_name, backend=backend), output[:4000])

        # Abort pipeline on stage failure
        if run.get("returncode", 1) != 0 or run.get("noop_reason"):
            break

    last_run = stage_results[-1]["run"] if stage_results else {"returncode": 1}
    noop_reason = last_run.get("noop_reason")
    status = "completed" if last_run.get("returncode", 1) == 0 and not noop_reason else "failed"
    error = noop_reason if status == "failed" and noop_reason else None
    result = finalize_pipeline_task(task, processing, stage_results, status, error=error,
                                    stages_model_info=stages_model_info)
    if isinstance(result, dict) and pipeline_routing:
        result["pipeline_routing"] = pipeline_routing
    return result
