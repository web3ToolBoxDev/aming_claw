"""
workspace_registry.py - Multi-workspace registry for parallel task processing.

Manages a collection of working directories, each with its own identity,
settings, and concurrency limits. Workspaces are persisted in
state/workspace_registry.json.

Schema per workspace entry:
  {
    "id":             "ws-<hex>",
    "path":           "/absolute/path/to/repo",
    "label":          "my-project",
    "project_id":     "my-project",           # normalized kebab-case (optional)
    "active":         true,
    "max_concurrent": 1,
    "is_default":     false,
    "created_at":     "<iso>",
    "updated_at":     "<iso>",
    "created_by":     <user_id|0>,
  }
"""
import os
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from i18n import t
from utils import load_json, normalize_project_id, save_json, tasks_root, utc_iso


def _paths_equal(a: Path, b: Path) -> bool:
    """Cross-platform path comparison (case-insensitive on Windows)."""
    if sys.platform == "win32":
        return str(a).lower() == str(b).lower()
    return a == b


def _registry_file() -> Path:
    return tasks_root() / "state" / "workspace_registry.json"


def _load_registry() -> Dict:
    p = _registry_file()
    if not p.exists():
        return {"version": 1, "workspaces": [], "updated_at": utc_iso()}
    try:
        data = load_json(p)
        if not isinstance(data.get("workspaces"), list):
            data["workspaces"] = []
        return data
    except Exception:
        return {"version": 1, "workspaces": [], "updated_at": utc_iso()}


def _save_registry(data: Dict) -> None:
    data["updated_at"] = utc_iso()
    save_json(_registry_file(), data)


def _new_ws_id() -> str:
    return "ws-" + uuid.uuid4().hex[:8]


# ── Sensitive-path guard ─────────────────────────────────────────────────────

_BLOCKED_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}


def is_blocked_workspace(path: Path) -> bool:
    """Reject paths that contain or *are* sensitive directories."""
    lowered_parts = {p.lower() for p in path.resolve().parts}
    return bool(lowered_parts.intersection(_BLOCKED_DIRS))


# ── CRUD ─────────────────────────────────────────────────────────────────────

def list_workspaces(*, include_inactive: bool = False) -> List[Dict]:
    reg = _load_registry()
    out = []
    for ws in reg["workspaces"]:
        if not include_inactive and not ws.get("active", True):
            continue
        out.append(ws)
    return out


def get_workspace(ws_id: str) -> Optional[Dict]:
    for ws in _load_registry()["workspaces"]:
        if ws["id"] == ws_id:
            return ws
    return None


def find_workspace_by_label(label: str) -> Optional[Dict]:
    label_lower = label.strip().lower()
    for ws in _load_registry()["workspaces"]:
        if ws.get("label", "").strip().lower() == label_lower:
            return ws
    return None


def find_workspace_by_path(path: Path) -> Optional[Dict]:
    resolved = str(path.resolve()).lower().replace("\\", "/")
    for ws in _load_registry()["workspaces"]:
        ws_resolved = str(Path(ws["path"]).resolve()).lower().replace("\\", "/")
        if ws_resolved == resolved:
            return ws
    return None


def find_workspace_by_project_id(project_id: str) -> Optional[Dict]:
    """Find workspace by normalized project_id match."""
    normalized = normalize_project_id(project_id)
    if not normalized:
        return None
    for ws in _load_registry()["workspaces"]:
        ws_pid = ws.get("project_id", "")
        if ws_pid and normalize_project_id(ws_pid) == normalized:
            return ws
    return None


def add_workspace(
    path: Path,
    *,
    label: str = "",
    project_id: str = "",
    max_concurrent: int = 1,
    is_default: bool = False,
    created_by: int = 0,
) -> Dict:
    """Register a new workspace directory. Returns the new workspace entry."""
    resolved = path.resolve()
    if is_blocked_workspace(resolved):
        raise ValueError(t("workspace_reg.path_sensitive", path=resolved))
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(t("workspace_reg.path_not_exist", path=resolved))

    existing = find_workspace_by_path(resolved)
    if existing:
        raise ValueError(t("workspace_reg.path_already_registered", path=resolved, id=existing["id"]))

    ws_id = _new_ws_id()
    if not label:
        label = resolved.name

    entry = {
        "id": ws_id,
        "path": str(resolved),
        "label": label,
        "project_id": normalize_project_id(project_id) if project_id else "",
        "active": True,
        "max_concurrent": max(1, int(max_concurrent)),
        "is_default": bool(is_default),
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
        "created_by": int(created_by),
    }

    reg = _load_registry()

    # If this is default, clear other defaults
    if is_default:
        for ws in reg["workspaces"]:
            ws["is_default"] = False

    reg["workspaces"].append(entry)
    _save_registry(reg)
    return entry


def remove_workspace(ws_id: str) -> bool:
    """Remove a workspace by id. Returns True if found and removed."""
    reg = _load_registry()
    before = len(reg["workspaces"])
    reg["workspaces"] = [ws for ws in reg["workspaces"] if ws["id"] != ws_id]
    if len(reg["workspaces"]) < before:
        _save_registry(reg)
        return True
    return False


def update_workspace(ws_id: str, **fields) -> Optional[Dict]:
    """Update fields on a workspace entry. Returns updated entry or None."""
    reg = _load_registry()
    for ws in reg["workspaces"]:
        if ws["id"] != ws_id:
            continue
        allowed = {"label", "active", "max_concurrent", "is_default", "path", "project_id"}
        for k, v in fields.items():
            if k in allowed:
                ws[k] = v
        ws["updated_at"] = utc_iso()

        # Enforce single default
        if fields.get("is_default"):
            for other in reg["workspaces"]:
                if other["id"] != ws_id:
                    other["is_default"] = False

        _save_registry(reg)
        return ws
    return None


def set_default_workspace(ws_id: str) -> bool:
    """Set the given workspace as default. Returns False if not found."""
    return update_workspace(ws_id, is_default=True) is not None


def get_default_workspace() -> Optional[Dict]:
    """Return the default workspace, or the first active one, or None."""
    workspaces = list_workspaces()
    for ws in workspaces:
        if ws.get("is_default"):
            return ws
    return workspaces[0] if workspaces else None


def resolve_workspace_for_task(task: Dict) -> Optional[Dict]:
    """Determine which workspace a task should run in.

    Priority:
      1. task["target_workspace_id"] → exact id match
      2. task["target_workspace"] → label match
      3. task["project_id"] → normalized project_id match
      4. @workspace:<label> prefix in task text
      5. default workspace
    """
    # Explicit workspace id
    ws_id = task.get("target_workspace_id", "").strip()
    if ws_id:
        ws = get_workspace(ws_id)
        if ws and ws.get("active", True):
            return ws

    # Explicit workspace label
    ws_label = task.get("target_workspace", "").strip()
    if ws_label:
        ws = find_workspace_by_label(ws_label)
        if ws and ws.get("active", True):
            return ws

    # Project ID match (normalized)
    project_id = task.get("project_id", "").strip()
    if project_id:
        ws = find_workspace_by_project_id(project_id)
        if ws and ws.get("active", True):
            return ws

    # Parse @workspace:<label> prefix from task text
    text = task.get("text", "")
    if text.startswith("@workspace:"):
        parts = text.split(None, 1)
        if parts:
            label_part = parts[0][len("@workspace:"):]
            ws = find_workspace_by_label(label_part)
            if ws and ws.get("active", True):
                return ws

    return get_default_workspace()


# ── Migration helper ─────────────────────────────────────────────────────────

def migrate_project_ids() -> int:
    """Auto-populate project_id on registry entries that lack it.

    For each entry without project_id, normalizes its label to derive one.
    Idempotent — safe to call multiple times.

    Returns number of entries updated.
    """
    reg = _load_registry()
    updated = 0
    for ws in reg["workspaces"]:
        if ws.get("project_id"):
            continue  # Already has project_id
        label = ws.get("label", "")
        if label:
            ws["project_id"] = normalize_project_id(label)
            ws["updated_at"] = utc_iso()
            updated += 1
    if updated:
        _save_registry(reg)
    return updated


def ensure_current_workspace_registered() -> Optional[Dict]:
    """当前活跃工作目录若不在注册表中，自动注册。
    Also runs project_id migration on existing entries."""
    # Migrate legacy entries that lack project_id
    migrate_project_ids()

    from workspace import resolve_active_workspace
    current = resolve_active_workspace()
    if not current.exists():
        return None

    current_resolved = current.resolve()
    existing = list_workspaces()

    # 检查当前路径是否已注册（规范化比较）
    for ws in existing:
        ws_path = Path(ws["path"]).resolve()
        if _paths_equal(current_resolved, ws_path):
            return None  # 已注册，跳过

    # Skip if current path is a subdirectory of an already-registered workspace
    # (e.g., agent/ is inside aming_claw/ — registering it would cause routing conflicts)
    for ws in existing:
        ws_path = Path(ws["path"]).resolve()
        current_str = str(current_resolved).lower().replace("\\", "/")
        ws_str = str(ws_path).lower().replace("\\", "/")
        if current_str.startswith(ws_str + "/"):
            return None  # Subdirectory of existing workspace, skip

    # Also skip if current path has no .git (not a project root)
    if not (current_resolved / ".git").is_dir():
        return None

    # 未注册，执行注册；注册表为空时设为默认
    is_first = len(existing) == 0
    try:
        return add_workspace(
            current,
            label=current.name,
            is_default=is_first,
            created_by=0,
        )
    except ValueError:
        return None
