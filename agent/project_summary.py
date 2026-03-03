"""
project_summary.py - Project information collection and formatting.

Provides:
- collect_project_info: Scan workspace directory for project overview
- collect_recent_commits: Get recent git commits
- format_summary_text: Format collected data into human-readable text
"""
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

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
