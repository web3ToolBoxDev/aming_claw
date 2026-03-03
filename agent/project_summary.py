"""
project_summary.py - Project information collection and formatting.

Provides:
- collect_project_info: Scan workspace directory for project overview
- collect_recent_commits: Get recent git commits
- format_summary_text: Format collected data into human-readable text
- generate_ai_summary: AI-driven project summary based on recent commits
"""
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

agent_dir = os.path.dirname(os.path.abspath(__file__))
if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)

# Directories to skip during file scanning
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "egg-info",
}

# Feature file -> tech stack mapping
TECH_STACK_MAP = {
    "package.json": "Node.js",
    "requirements.txt": "Python",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "docker-compose.yaml": "Docker Compose",
    "Makefile": "Make",
    "CMakeLists.txt": "CMake",
    "pom.xml": "Java/Maven",
    "build.gradle": "Java/Gradle",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    ".csproj": "C#/.NET",
    "tsconfig.json": "TypeScript",
}


def _run_git(workspace: Path, *args: str, timeout: int = 15) -> Tuple[int, str, str]:
    """Run a git command in the given workspace directory."""
    cmd = ["git", "-C", str(workspace)] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "git command timed out"
    except Exception as exc:
        return -1, "", str(exc)


def _is_git_repo(workspace: Path) -> bool:
    """Check if the workspace is inside a git repository."""
    code, _, _ = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return code == 0


def _collect_git_info(workspace: Path) -> Dict:
    """Collect git status information."""
    if not _is_git_repo(workspace):
        return {
            "is_repo": False,
            "branch": "",
            "commit": "",
            "has_uncommitted": False,
            "uncommitted_count": 0,
        }

    # Branch name
    code, branch, _ = _run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        branch = "(unknown)"

    # Latest short commit SHA
    code, commit, _ = _run_git(workspace, "rev-parse", "--short", "HEAD")
    if code != 0:
        commit = ""

    # Uncommitted changes count
    code, status_out, _ = _run_git(workspace, "status", "--porcelain")
    uncommitted_lines = [l for l in status_out.splitlines() if l.strip()] if code == 0 else []

    return {
        "is_repo": True,
        "branch": branch,
        "commit": commit,
        "has_uncommitted": len(uncommitted_lines) > 0,
        "uncommitted_count": len(uncommitted_lines),
    }


def _detect_tech_stack(workspace: Path) -> List[str]:
    """Detect tech stack by checking for characteristic files in the root."""
    detected = set()
    try:
        root_files = set(os.listdir(workspace))
    except OSError:
        return []

    for filename, tech in TECH_STACK_MAP.items():
        if filename in root_files:
            detected.add(tech)

    # Also check for files ending with known suffixes (e.g. *.csproj)
    for f in root_files:
        if f.endswith(".csproj"):
            detected.add("C#/.NET")
        if f.endswith(".sln"):
            detected.add("C#/.NET")

    return sorted(detected)


def _collect_file_stats(workspace: Path) -> Dict:
    """Collect file statistics grouped by extension."""
    counter = Counter()
    total = 0
    for root, dirs, files in os.walk(workspace, topdown=True):
        # Prune skipped directories in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for f in files:
            total += 1
            ext = os.path.splitext(f)[1].lower()
            if ext:
                counter[ext] += 1
            else:
                counter["(no ext)"] += 1

    return {
        "total_files": total,
        "by_extension": dict(counter.most_common()),
    }


def _collect_top_dirs(workspace: Path, max_depth: int = 2) -> List[str]:
    """Collect top-level directory structure up to max_depth."""
    result = []
    base = workspace.resolve()
    try:
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name in SKIP_DIRS or name.startswith("."):
                continue
            result.append(name + "/")
            if max_depth >= 2:
                try:
                    for sub in sorted(entry.iterdir()):
                        if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.name.startswith("."):
                            result.append("  " + sub.name + "/")
                except PermissionError:
                    pass
    except PermissionError:
        pass
    return result


def collect_project_info(workspace: Path) -> Dict:
    """Scan workspace directory, collect project overview.

    Returns a dict with: name, path, git, tech_stack, file_stats, top_dirs.
    """
    workspace = Path(workspace).resolve()
    return {
        "name": workspace.name,
        "path": str(workspace),
        "git": _collect_git_info(workspace),
        "tech_stack": _detect_tech_stack(workspace),
        "file_stats": _collect_file_stats(workspace),
        "top_dirs": _collect_top_dirs(workspace),
    }


def collect_recent_commits(workspace: Path, count: int = 20) -> List[Dict]:
    """Get the most recent N commits from the workspace git repo.

    Returns list of dicts with: sha, author, date, message.
    Returns empty list if not a git repo or no commits.
    """
    workspace = Path(workspace).resolve()
    if not _is_git_repo(workspace):
        return []

    fmt = "%h|%an|%ai|%s"
    code, out, _ = _run_git(
        workspace, "log", "--oneline",
        "--format={}".format(fmt),
        "-n", str(count),
        timeout=15,
    )
    if code != 0 or not out:
        return []

    commits = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        # Shorten date to YYYY-MM-DD
        date_str = parts[2].strip()
        if len(date_str) >= 10:
            date_str = date_str[:10]
        commits.append({
            "sha": parts[0].strip(),
            "author": parts[1].strip(),
            "date": date_str,
            "message": parts[3].strip(),
        })
    return commits


# ---------------------------------------------------------------------------
# AI-driven summary generation
# ---------------------------------------------------------------------------

# Empty tree SHA used to diff the very first commit
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf899d69f82ced515"

# Max diff chars per commit to avoid prompt overflow
_MAX_DIFF_CHARS = 3000


def _collect_commit_diffs(workspace: Path, commit_count: int = 3) -> List[Dict]:
    """Collect recent commits with diff stat and diff content.

    Returns list of dicts:
        [{hash, message, author, date, diff_stat, diff_content}, ...]
    """
    workspace = Path(workspace).resolve()
    if not _is_git_repo(workspace):
        return []

    commit_count = max(1, min(commit_count, 10))

    # Get commit metadata
    fmt = "%H|%s|%an|%ai"
    code, out, _ = _run_git(
        workspace, "log",
        "--format={}".format(fmt),
        "-n", str(commit_count),
    )
    if code != 0 or not out:
        return []

    results = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        full_hash = parts[0].strip()
        message = parts[1].strip()
        author = parts[2].strip()
        date_str = parts[3].strip()[:10]

        # Try diff against parent; for first commit use empty tree
        parent_ref = "{}~1".format(full_hash)
        code_stat, diff_stat, _ = _run_git(
            workspace, "diff", "{}..{}".format(parent_ref, full_hash), "--stat",
        )
        if code_stat != 0:
            # Likely first commit with no parent
            _, diff_stat, _ = _run_git(
                workspace, "diff", "--stat", "{}..{}".format(_EMPTY_TREE_SHA, full_hash),
            )

        code_diff, diff_content, _ = _run_git(
            workspace, "diff", "{}..{}".format(parent_ref, full_hash),
        )
        if code_diff != 0:
            _, diff_content, _ = _run_git(
                workspace, "diff", "{}..{}".format(_EMPTY_TREE_SHA, full_hash),
            )

        # Truncate diff content
        if len(diff_content) > _MAX_DIFF_CHARS:
            diff_content = diff_content[:_MAX_DIFF_CHARS] + "\n... (diff truncated)"

        results.append({
            "hash": full_hash[:8],
            "full_hash": full_hash,
            "message": message,
            "author": author,
            "date": date_str,
            "diff_stat": diff_stat,
            "diff_content": diff_content,
        })
    return results


def _build_summary_prompt(workspace: Path, commit_diffs: List[Dict]) -> str:
    """Build prompt for AI summary generation."""
    # Collect top-level directory structure for context
    top_dirs = _collect_top_dirs(workspace, max_depth=1)
    tech_stack = _detect_tech_stack(workspace)

    parts = []
    parts.append("你是一位资深开发者，请根据以下项目信息和最近的 Git 提交变动，生成一份简洁的中文项目总结。")
    parts.append("")
    parts.append("要求：")
    parts.append("1. 先用一小段话概括这个项目是做什么的（基于目录结构、技术栈和代码变动推断）")
    parts.append("2. 然后按 commit 逐条说明每次提交实现或修改了什么功能")
    parts.append("3. 用中文，简洁明了，以文字段落为主，不要用表格或代码块")
    parts.append("4. 不要输出任何前缀标题如\"项目总结\"，直接输出内容")
    parts.append("")

    if tech_stack:
        parts.append("【技术栈】{}".format(", ".join(tech_stack)))
    if top_dirs:
        parts.append("【目录结构】{}".format(", ".join(d.strip() for d in top_dirs[:15])))
    parts.append("")

    for i, cd in enumerate(commit_diffs, 1):
        parts.append("===== 提交 {} =====".format(i))
        parts.append("Hash: {}  日期: {}  作者: {}".format(cd["hash"], cd["date"], cd["author"]))
        parts.append("Message: {}".format(cd["message"]))
        if cd["diff_stat"]:
            parts.append("变更文件统计:\n{}".format(cd["diff_stat"]))
        if cd["diff_content"]:
            parts.append("代码变动:\n{}".format(cd["diff_content"]))
        parts.append("")

    return "\n".join(parts)


def _call_ai_api(prompt: str, timeout: int = 30) -> Optional[str]:
    """Call the configured AI backend and return the response text.

    Returns None on any failure (timeout, missing key, API error).
    """
    import requests as _req
    from config import get_claude_model, get_model_provider

    model = get_claude_model().strip()
    provider = get_model_provider().strip().lower()

    try:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                return None
            resp = _req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key,
                         "Content-Type": "application/json"},
                json={"model": model or "gpt-4o",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 4096},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                return None
            return resp.json()["choices"][0]["message"]["content"]
        else:
            # Default: Anthropic Messages API
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                return None
            payload = {
                "model": model or "claude-sonnet-4-6",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key,
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code >= 400:
                return None
            return resp.json()["content"][0]["text"]
    except Exception:
        return None


def _build_fallback_summary(workspace: Path, commit_diffs: List[Dict]) -> str:
    """Build a plain-text fallback summary when AI is unavailable."""
    lines = []
    name = workspace.name
    tech = _detect_tech_stack(workspace)
    lines.append("\U0001f4ca 项目总结: {}".format(name))
    lines.append("\u2501" * 24)
    if tech:
        lines.append("\U0001f527 技术栈: {}".format(", ".join(tech)))
    lines.append("")
    lines.append("\U0001f4dd 最近 {} 条提交:".format(len(commit_diffs)))
    for cd in commit_diffs:
        lines.append("")
        lines.append("[{}] {} - {}".format(cd["hash"], cd["date"], cd["message"]))
        if cd["diff_stat"]:
            for sl in cd["diff_stat"].splitlines()[-3:]:
                lines.append("  {}".format(sl.strip()))
    lines.append("")
    lines.append("(AI 分析不可用，仅展示提交记录)")
    return "\n".join(lines)


def generate_ai_summary(workspace_path, commit_count: int = 3) -> str:
    """Generate an AI-driven project summary based on recent commits.

    Args:
        workspace_path: Path to the workspace/git repo.
        commit_count: Number of recent commits to analyse (1-10).

    Returns:
        A Chinese text summary string.
    """
    workspace = Path(workspace_path).resolve()
    commit_count = max(1, min(commit_count, 10))

    if not _is_git_repo(workspace):
        return "当前工作区非 Git 仓库，无法生成提交分析。"

    commit_diffs = _collect_commit_diffs(workspace, commit_count)
    if not commit_diffs:
        return "当前仓库无提交记录。"

    # Build prompt and call AI
    prompt = _build_summary_prompt(workspace, commit_diffs)
    ai_response = _call_ai_api(prompt)

    if ai_response:
        # Format with header
        lines = []
        lines.append("\U0001f4ca 项目总结")
        lines.append("\u2501" * 24)
        lines.append("")
        lines.append(ai_response.strip())
        return "\n".join(lines)

    # Fallback: plain commit list
    return _build_fallback_summary(workspace, commit_diffs)


def format_summary_text(info: Dict, commits: List[Dict]) -> str:
    """Format project info and commits into a human-readable plain text report."""
    lines = []
    name = info.get("name", "unknown")
    lines.append("\U0001f4ca \u9879\u76ee\u603b\u7ed3: {}".format(name))
    lines.append("\u2501" * 24)

    # Path
    lines.append("\U0001f4c1 \u8def\u5f84: {}".format(info.get("path", "")))

    # Git info
    git = info.get("git", {})
    if git.get("is_repo"):
        branch = git.get("branch", "")
        commit = git.get("commit", "")
        lines.append("\U0001f33f \u5206\u652f: {} ({})".format(branch, commit))
        if git.get("has_uncommitted"):
            lines.append("\u26a0\ufe0f \u672a\u63d0\u4ea4\u53d8\u66f4: {} \u4e2a\u6587\u4ef6".format(git.get("uncommitted_count", 0)))
        else:
            lines.append("\u2705 \u5de5\u4f5c\u533a\u5e72\u51c0")
    else:
        lines.append("\U0001f4c2 \u975e Git \u4ed3\u5e93")

    # Tech stack
    tech = info.get("tech_stack", [])
    if tech:
        lines.append("")
        lines.append("\U0001f527 \u6280\u672f\u6808: {}".format(", ".join(tech)))

    # File stats
    file_stats = info.get("file_stats", {})
    total = file_stats.get("total_files", 0)
    by_ext = file_stats.get("by_extension", {})
    lines.append("")
    lines.append("\U0001f4c4 \u6587\u4ef6\u7edf\u8ba1 (\u5171 {} \u4e2a):".format(total))
    # Show top 10 extensions
    sorted_ext = sorted(by_ext.items(), key=lambda x: x[1], reverse=True)
    shown = 0
    other_count = 0
    for ext, cnt in sorted_ext:
        if shown < 10:
            lines.append("  {:<8s} \u2014 {}".format(ext, cnt))
            shown += 1
        else:
            other_count += cnt
    if other_count > 0:
        lines.append("  {:<8s} \u2014 {}".format("\u5176\u4ed6", other_count))

    # Directory structure
    top_dirs = info.get("top_dirs", [])
    if top_dirs:
        lines.append("")
        lines.append("\U0001f4c2 \u76ee\u5f55\u7ed3\u6784:")
        for d in top_dirs:
            lines.append("  {}".format(d))

    # Recent commits
    if commits:
        lines.append("")
        lines.append("\U0001f4dd \u6700\u8fd1\u63d0\u4ea4 (\u5171 {} \u6761):".format(len(commits)))
        for c in commits:
            lines.append("  {} | {} | {}".format(
                c.get("sha", ""),
                c.get("date", ""),
                c.get("message", ""),
            ))

    return "\n".join(lines)
