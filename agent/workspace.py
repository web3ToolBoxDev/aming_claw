import os
import threading
from pathlib import Path
from typing import Optional

from utils import load_json, save_json, tasks_root, utc_iso


# ── Thread-local workspace context ──────────────────────────────────────────
# Used by parallel_dispatcher to set workspace per worker thread.
# When set, resolve_active_workspace() returns this value instead of
# the global override or env var.

_thread_local = threading.local()


def set_thread_workspace(workspace: Optional[Path]) -> None:
    """Set thread-local workspace override (for parallel worker threads)."""
    _thread_local.workspace = workspace


def get_thread_workspace() -> Optional[Path]:
    """Get thread-local workspace override, or None."""
    return getattr(_thread_local, "workspace", None)


def clear_thread_workspace() -> None:
    """Clear thread-local workspace override."""
    _thread_local.workspace = None


class thread_workspace_context:
    """Context manager to temporarily set the thread-local workspace."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._previous: Optional[Path] = None

    def __enter__(self):
        self._previous = get_thread_workspace()
        set_thread_workspace(self._workspace)
        return self

    def __exit__(self, *exc):
        set_thread_workspace(self._previous)


# ── Global workspace state (file-based) ─────────────────────────────────────

def workspace_state_file() -> Path:
    return tasks_root() / "state" / "workspace_override.json"


def get_workspace_override() -> Optional[Path]:
    path = workspace_state_file()
    if not path.exists():
        return None
    try:
        val = str(load_json(path).get("workspace", "")).strip()
    except Exception:
        return None
    if not val:
        return None
    return Path(val)


def set_workspace_override(workspace: Path, changed_by: int) -> None:
    save_json(
        workspace_state_file(),
        {
            "workspace": str(workspace),
            "changed_by": int(changed_by),
            "updated_at": utc_iso(),
        },
    )


def clear_workspace_override(changed_by: int) -> None:
    save_json(
        workspace_state_file(),
        {
            "workspace": "",
            "changed_by": int(changed_by),
            "updated_at": utc_iso(),
        },
    )


def resolve_workspace_from_env() -> Path:
    configured = os.getenv("CODEX_WORKSPACE", "").strip()
    if configured:
        return Path(configured)
    return Path.cwd()


def resolve_active_workspace() -> Path:
    """Return the active workspace, checking thread-local override first.

    Priority:
      1. Thread-local workspace (set by parallel dispatcher worker)
      2. Global file-based override (workspace_override.json)
      3. CODEX_WORKSPACE env var
      4. Current working directory
    """
    thread_ws = get_thread_workspace()
    if thread_ws:
        return thread_ws
    override = get_workspace_override()
    if override:
        return override
    return resolve_workspace_from_env()
