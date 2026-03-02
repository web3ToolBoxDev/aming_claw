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
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

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
    summary = (
        "已执行步骤: 创建文件并写入时间，等待 {sec} 秒后追加内容。\n"
        "修改文件列表: {file}\n"
        "后续建议: 可用 /status 查看状态，或直接检查文件内容。"
    ).format(sec=parsed["wait_sec"], file=str(target))
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


def build_codex_prompt(task: Dict) -> str:
    text = _get_task_text(task)
    return (
        "你是 Codex 执行器，必须直接执行任务，不要复述角色，不要请求用户再补充。\n"
        "如果任务存在歧义，做最合理假设并继续执行。\n"
        "要求：\n"
        "1) 直接在工作目录修改文件/运行命令；\n"
        "2) 禁止访问任何敏感目录/文件（如 .ssh、.aws、.gnupg、私钥、系统凭据目录）；\n"
        "3) 最终输出包含：已执行步骤、修改文件列表、后续建议；\n"
        "4) 中文回复。\n\n"
        "任务ID: {task_id}\n"
        "任务内容: {text}\n"
    ).format(task_id=task["task_id"], text=text)


def build_claude_prompt(task: Dict) -> str:
    text = _get_task_text(task)
    return (
        "请立即执行以下任务（禁止回复确认语，禁止请求补充信息，直接动手）：\n\n"
        "{text}\n\n"
        "任务ID: {task_id}\n"
        "要求：直接在工作目录修改文件或运行命令；"
        "禁止访问敏感目录（.ssh/.aws/.gnupg/私钥）；"
        "完成后输出：1) 已执行步骤 2) 修改文件列表 3) 后续建议。中文回复。"
    ).format(task_id=task["task_id"], text=text)


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


def run_claude(task: Dict, extra_guard: str = "", attempt_tag: str = "", prompt_override: Optional[str] = None) -> Dict:
    # Claude CLI needs the actual project directory (not the isolated search-workspace)
    # so it can read and modify project source files.
    workspace = resolve_active_workspace()
    if is_sensitive_path(workspace):
        raise RuntimeError("workspace is sensitive and not allowed: {}".format(workspace))
    if task_touches_sensitive_path(task.get("text", "")):
        raise RuntimeError("task rejected: request touches sensitive paths (e.g. .ssh)")
    timeout_sec = int(os.getenv("CLAUDE_TIMEOUT_SEC", "1200"))
    max_retries = int(os.getenv("CLAUDE_TIMEOUT_RETRIES", "1"))
    from config import get_claude_model
    model = get_claude_model() or os.getenv("CLAUDE_MODEL", "").strip()
    claude_bin = os.getenv("CLAUDE_BIN", "").strip()
    if not claude_bin:
        import shutil
        claude_bin = (
            shutil.which("claude.cmd") or shutil.which("claude")
            or ("claude.cmd" if os.name == "nt" else "claude")
        )
    dangerous = os.getenv("CLAUDE_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}

    prompt = prompt_override if prompt_override is not None else build_claude_prompt(task)
    if extra_guard:
        prompt = prompt + "\n\n" + extra_guard.strip() + "\n"
    # Pass prompt via stdin to avoid Windows cmd.exe truncating multi-line arguments.
    cmd = [claude_bin, "-p", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    if dangerous:
        cmd += ["--dangerously-skip-permissions"]

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

    stdout = (proc.stdout or "").strip()
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
    # If message contains structured execution evidence (已执行步骤 + 修改文件 + 后续建议),
    # it is NOT ack-only — it's the expected output contract from task execution.
    if has_execution_evidence(msg):
        return False
    lowered = msg.lower()
    if ("后续" in msg and "执行" in msg) or ("直接告诉我" in msg) or ("请告诉我" in msg):
        return True
    if ("直接执行模式" in msg) or ("进入直接执行模式" in msg):
        return True
    if "i will" in lowered and "execute" in lowered:
        return True
    # Asking user to send/provide the task — model hasn't seen or acted on it.
    if "请发送" in msg or "发送任务" in msg or "请提供任务" in msg:
        return True
    patterns = [
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
    ]
    return any(re.match(p, msg) for p in patterns)


def has_execution_evidence(text: str) -> bool:
    msg = (text or "").strip()
    if not msg:
        return False
    # Preferred structured evidence from prompt contract.
    if ("已执行步骤" in msg and "修改文件" in msg and "后续建议" in msg):
        return True
    # Fallback evidence: explicit command/file modification markers.
    markers = [
        "执行了",
        "已完成以下",
        "修改了",
        "创建了",
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
        guard = (
            "上一次输出被判定为无效（仅确认语或无执行证据）。\n"
            "禁止回复\"收到/明白/后续执行\"等确认语。\n"
            "你必须立即执行任务，并在最终回复中包含：\n"
            "1) 已执行步骤（包含实际命令）\n"
            "2) 修改文件列表（若无文件改动，明确说明原因）\n"
            "3) 后续建议\n"
            "若仍不执行，将判定任务失败。"
        )
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
        guard = (
            "上一次输出被判定为无效（仅确认语或无执行证据）。\n"
            "禁止回复\"收到/明白/后续执行\"等确认语。\n"
            "你必须立即执行任务，并在最终回复中包含：\n"
            "1) 已执行步骤（包含实际命令）\n"
            "2) 修改文件列表（若无文件改动，明确说明原因）\n"
            "3) 后续建议\n"
            "若仍不执行，将判定任务失败。"
        )
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

_STAGE_ROLE_PROMPTS = {
    "plan": (
        "你是任务规划专家，必须直接输出可测试的验收标准，不要请求用户补充。\n"
        "如果任务存在歧义，做最合理假设并继续。\n"
        "【输出要求】\n"
        "1) 逐条列出验收标准（每条必须可独立验证，使用编号）；\n"
        "2) 列出测试用例（至少3条，含步骤/预期输出）；\n"
        "3) 指出实现的关键约束和边界条件；\n"
        "4) 中文回复，使用清晰的编号格式。\n"
    ),
    "code": (
        "你是代码实现专家，必须直接编写并执行代码，不要请求用户补充。\n"
        "如果有验收标准请严格遵守；如无则做最合理实现。\n"
        "【输出要求】\n"
        "1) 直接在工作目录修改文件/运行命令；\n"
        "2) 禁止访问敏感目录（.ssh、.aws、私钥等）；\n"
        "3) 最终输出：已执行步骤、修改文件列表、后续建议；\n"
        "4) 中文回复。\n"
    ),
    "implement": (
        "你是代码实现专家，必须直接编写并执行代码，不要请求用户补充。\n"
        "如果有验收标准请严格遵守；如无则做最合理实现。\n"
        "【输出要求】\n"
        "1) 直接在工作目录修改文件/运行命令；\n"
        "2) 禁止访问敏感目录（.ssh、.aws、私钥等）；\n"
        "3) 最终输出：已执行步骤、修改文件列表、后续建议；\n"
        "4) 中文回复。\n"
    ),
    "verify": (
        "你是质量验收专家，必须对照验收标准逐项检查，不要请求用户补充。\n"
        "如果没有明确验收标准，根据常见工程质量标准自行评估。\n"
        "【输出要求】\n"
        "1) 逐条列出验收标准及检查结果（✓通过 / ✗失败 / ⚠部分通过）；\n"
        "2) 运行相关测试/检查命令，记录输出；\n"
        "3) 输出总体验收结论：通过 / 部分通过 / 失败；\n"
        "4) 如有问题，列出具体问题和修复建议；\n"
        "5) 中文回复。\n"
    ),
    "test": (
        "你是测试专家，必须执行具体的测试操作，不要请求用户补充。\n"
        "【输出要求】\n"
        "1) 运行所有相关测试，记录每个测试的结果（通过/失败）；\n"
        "2) 输出测试覆盖率（如可获取）；\n"
        "3) 汇总测试结果和发现的问题；\n"
        "4) 中文回复。\n"
    ),
    "review": (
        "你是代码审查专家，必须对代码质量和实现进行专业评估，不要请求用户补充。\n"
        "【输出要求】\n"
        "1) 评估代码质量（可读性、维护性、性能）；\n"
        "2) 指出潜在问题和改进点；\n"
        "3) 给出总体评分和结论；\n"
        "4) 中文回复。\n"
    ),
    # ── Role pipeline prompts ──────────────────────────────────────────────
    "pm": (
        "你是产品经理（PM），负责解析用户原始需求，拆分为结构化子任务，定义验收标准。\n"
        "禁止回复确认语，禁止请求用户补充信息，直接输出需求文档。\n"
        "如果任务存在歧义，做最合理假设并继续。\n\n"
        "【输出要求 - 需求文档】\n"
        "1) 需求概述：一句话描述核心目标；\n"
        "2) 子任务列表：逐条拆分，每条包含：\n"
        "   - 编号和标题\n"
        "   - 具体描述（做什么、改哪里）\n"
        "   - 验收条件（可独立验证的具体标准）\n"
        "3) 全局约束和注意事项；\n"
        "4) 预期影响范围（涉及的文件/模块）；\n"
        "5) 中文回复，使用清晰的编号格式。\n"
    ),
    "dev": (
        "你是开发工程师（Dev），根据产品经理产出的需求文档逐项实现代码变更。\n"
        "禁止回复确认语，禁止请求用户补充信息，直接编写和执行代码。\n"
        "严格按照需求文档中的子任务和验收标准进行实现。\n\n"
        "【输出要求】\n"
        "1) 直接在工作目录修改文件/运行命令；\n"
        "2) 禁止访问敏感目录（.ssh、.aws、私钥等）；\n"
        "3) 逐项实现需求文档中的子任务；\n"
        "4) 最终输出：\n"
        "   - 已执行步骤（含实际命令）\n"
        "   - 修改文件列表及变更说明\n"
        "   - 每个子任务的实现状态\n"
        "5) 中文回复。\n"
    ),
    "qa": (
        "你是QA验收专家，负责对照需求文档中的验收标准，审计代码变更和测试结果。\n"
        "禁止回复确认语，禁止请求用户补充信息，直接输出验收报告。\n\n"
        "【输出要求 - 验收报告】\n"
        "1) 逐项对照验收标准检查：\n"
        "   - 编号对应需求文档中的子任务\n"
        "   - 每项标注：✓通过 / ✗未通过 / ⚠部分通过\n"
        "   - 附上判断依据和证据\n"
        "2) 代码变更审查结果（质量、安全、规范）；\n"
        "3) 测试结果审查（覆盖率、遗漏场景）；\n"
        "4) 总体结论：通过 / 有条件通过 / 不通过；\n"
        "5) 如不通过，列出具体问题和修复建议；\n"
        "6) 中文回复。\n"
    ),
}

_DEFAULT_STAGE_PROMPT = (
    "你是AI执行器，必须直接执行任务，不要请求用户补充。\n"
    "最终输出包含：已执行步骤、修改文件列表、后续建议。\n"
)


def build_pipeline_stage_prompt(task: Dict, stage_name: str, context: str) -> str:
    """Build a stage-specific prompt incorporating accumulated context from prior stages."""
    role_intro = _STAGE_ROLE_PROMPTS.get(stage_name.lower(), _DEFAULT_STAGE_PROMPT)
    parts = [role_intro.rstrip()]
    if context.strip():
        parts.append("【前序阶段输出】\n" + context.strip()[:6000])
    parts.append(
        "【任务ID】: {}\n【任务内容】: {}".format(task["task_id"], _get_task_text(task))
    )
    return "\n\n".join(parts)


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

    def _run(p: str, tag: str) -> Dict:
        # Route to the correct CLI based on the effective provider.
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
        guard = (
            "上一次输出被判定为无效（仅确认语或无实质内容）。\n"
            "禁止回复\"收到/明白/后续执行\"等确认语。\n"
            "你必须立即完成当前阶段任务并输出具体内容。\n"
            "若仍不执行，将判定任务失败。"
        )
        run = _run(prompt + "\n\n" + guard, base_tag + "_r{}".format(retry_idx))
        noop_reason = detect_stage_noop(run, stage)
        if not noop_reason:
            run["noop_reason"] = None
            run["attempt_count"] = retry_idx + 1
            run["noop_retry_last_reason"] = last_reason
            return run
        last_reason = noop_reason

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


def _build_role_context(stage_name: str, stage_outputs: Dict[str, str]) -> str:
    """Build role-aware context for a pipeline stage.

    PM output is base context for all subsequent stages.
    Dev output is added for test and qa.
    Test output is added for qa.
    """
    from config import ROLE_DEFINITIONS
    parts = []
    role_label_map = {k: v.get("label", k) for k, v in ROLE_DEFINITIONS.items()}

    if stage_name == "dev":
        if "pm" in stage_outputs:
            parts.append("【{} 产出 - 需求文档】\n{}".format(
                role_label_map.get("pm", "PM"), stage_outputs["pm"]))
    elif stage_name == "test":
        if "pm" in stage_outputs:
            parts.append("【{} 产出 - 需求文档】\n{}".format(
                role_label_map.get("pm", "PM"), stage_outputs["pm"]))
        if "dev" in stage_outputs:
            parts.append("【{} 产出 - 代码变更】\n{}".format(
                role_label_map.get("dev", "Dev"), stage_outputs["dev"]))
    elif stage_name == "qa":
        if "pm" in stage_outputs:
            parts.append("【{} 产出 - 需求文档】\n{}".format(
                role_label_map.get("pm", "PM"), stage_outputs["pm"]))
        if "dev" in stage_outputs:
            parts.append("【{} 产出 - 代码变更】\n{}".format(
                role_label_map.get("dev", "Dev"), stage_outputs["dev"]))
        if "test" in stage_outputs:
            parts.append("【{} 产出 - 测试结果】\n{}".format(
                role_label_map.get("test", "Test"), stage_outputs["test"]))

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
            logger.info("[Pipeline] 已加载管线多服务商配置")
        return config
    except ValueError as exc:
        logger.error("[Pipeline] 管线配置加载失败: %s", exc)
        return {}
    except Exception as exc:
        logger.debug("[Pipeline] 未加载管线配置文件 (非错误): %s", exc)
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

        # Build context based on pipeline type
        if is_role:
            role_context = _build_role_context(stage_name, stage_outputs)
            prompt = build_pipeline_stage_prompt(task, stage_name, role_context)
        else:
            prompt = build_pipeline_stage_prompt(task, stage_name, context)

        run = run_stage_with_retry(task, stage, prompt, stage_idx=i + 1)

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
            context += "【{} 阶段 ({})】\n{}\n\n".format(stage_name, backend, output[:4000])

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
