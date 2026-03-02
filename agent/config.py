"""
agent_config.py - Runtime agent backend configuration.

Stores the active backend (codex / claude / pipeline) in
state/agent_config.json so it persists across restarts and can be
changed without editing .env.

Priority: agent_config.json > AGENT_BACKEND env > "codex" default

Pipeline mode: when agent_backend="pipeline", pipeline_stages defines
the ordered list of AI stages to run for each task, e.g.:
  [{"name": "plan", "backend": "claude"},
   {"name": "code", "backend": "claude"},
   {"name": "verify", "backend": "codex"}]
"""
import os
from typing import Dict, List, Optional, Tuple

from utils import load_json, save_json, tasks_root, utc_iso

# Valid top-level backends (pipeline = multi-stage mode)
KNOWN_BACKENDS = {"codex", "claude", "pipeline"}

# Valid backends for individual pipeline stages
KNOWN_STAGE_BACKENDS = {"codex", "claude"}

# Built-in named pipeline presets (name → stage list)
PIPELINE_PRESETS: Dict[str, List[Dict]] = {
    "plan_code_verify": [
        {"name": "plan",   "backend": "claude"},
        {"name": "code",   "backend": "claude"},
        {"name": "verify", "backend": "codex"},
    ],
    "plan_code": [
        {"name": "plan", "backend": "claude"},
        {"name": "code", "backend": "claude"},
    ],
    "code_verify": [
        {"name": "code",   "backend": "claude"},
        {"name": "verify", "backend": "codex"},
    ],
    "claude_codex": [
        {"name": "code",   "backend": "claude"},
        {"name": "verify", "backend": "codex"},
    ],
}


def _config_path():
    return tasks_root() / "state" / "agent_config.json"


def _parse_pipeline_stages(raw: str) -> List[Dict]:
    """Parse 'plan:claude code:claude verify:codex' into a stage list."""
    stages = []
    for item in raw.split():
        item = item.strip().lower()
        if not item:
            continue
        if ":" in item:
            name, backend = item.split(":", 1)
        else:
            name, backend = item, "codex"
        backend = backend.strip()
        if backend not in KNOWN_STAGE_BACKENDS:
            backend = "codex"
        stages.append({"name": name.strip(), "backend": backend})
    return stages


# ── Backend ───────────────────────────────────────────────────────────────────

def get_agent_backend() -> str:
    """Return the active backend, reading runtime config first."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            backend = str(data.get("agent_backend", "")).strip()
            if backend in KNOWN_BACKENDS:
                return backend
        except Exception:
            pass
    return os.getenv("AGENT_BACKEND", "codex")


def set_agent_backend(backend: str, changed_by: Optional[int] = None) -> None:
    """Persist the active backend to runtime config."""
    if backend not in KNOWN_BACKENDS:
        raise ValueError(
            "Unknown backend: {!r}. Must be one of: {}".format(
                backend, ", ".join(sorted(KNOWN_BACKENDS))
            )
        )
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = load_json(p)
        except Exception:
            pass
    existing["agent_backend"] = backend
    existing["updated_at"] = utc_iso()
    if changed_by is not None:
        existing["changed_by"] = changed_by
    save_json(p, existing)


# ── AI model (provider + model id) ────────────────────────────────────────────

# Fallback list used when APIs are unreachable
KNOWN_CLAUDE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


def get_claude_model() -> str:
    """Return the active model id (runtime config > env > empty)."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            m = str(data.get("claude_model", "")).strip()
            if m:
                return m
        except Exception:
            pass
    return os.getenv("CLAUDE_MODEL", "").strip()


def get_model_provider() -> str:
    """Return the provider for the active model: 'anthropic' | 'openai' | ''."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            return str(data.get("model_provider", "")).strip()
        except Exception:
            pass
    return ""


def set_claude_model(model: str, provider: str = "",
                     changed_by: Optional[int] = None) -> None:
    """Persist the active model (and provider) to runtime config."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = load_json(p)
        except Exception:
            pass
    existing["claude_model"] = model
    if provider:
        existing["model_provider"] = provider
    existing["updated_at"] = utc_iso()
    if changed_by is not None:
        existing["changed_by"] = changed_by
    save_json(p, existing)


# ── Pipeline stages ───────────────────────────────────────────────────────────

def get_pipeline_stages() -> List[Dict]:
    """Return the configured pipeline stages, or [] if none."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            stages = data.get("pipeline_stages")
            if isinstance(stages, list) and stages:
                return stages
        except Exception:
            pass
    # Fall back to env var: TASK_PIPELINE_STAGES="plan:claude code:claude verify:codex"
    raw = os.getenv("TASK_PIPELINE_STAGES", "").strip()
    if raw:
        return _parse_pipeline_stages(raw)
    return []


def set_pipeline_stages(stages: List[Dict], changed_by: Optional[int] = None) -> None:
    """Persist pipeline stages and switch agent_backend to 'pipeline'."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = load_json(p)
        except Exception:
            pass
    existing["agent_backend"] = "pipeline"
    existing["pipeline_stages"] = stages
    existing["updated_at"] = utc_iso()
    if changed_by is not None:
        existing["changed_by"] = changed_by
    save_json(p, existing)


def format_pipeline_stages(stages: List[Dict]) -> str:
    """Human-readable stage list, e.g. 'plan(claude) → code(claude) → verify(codex)'"""
    if not stages:
        return "(empty)"
    return " → ".join("{}({})".format(s.get("name", "?"), s.get("backend", "?")) for s in stages)


# ── Workspace search roots ────────────────────────────────────────────────────

def get_workspace_search_roots() -> List[str]:
    """Return persisted workspace search root paths, or [] if none."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            roots = data.get("workspace_search_roots")
            if isinstance(roots, list):
                return [str(r) for r in roots if str(r).strip()]
        except Exception:
            pass
    return []


def set_workspace_search_roots(roots: List[str],
                               changed_by: Optional[int] = None) -> None:
    """Persist workspace search root paths to runtime config."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = load_json(p)
        except Exception:
            pass
    existing["workspace_search_roots"] = [r.strip() for r in roots if r.strip()]
    existing["updated_at"] = utc_iso()
    if changed_by is not None:
        existing["changed_by"] = changed_by
    save_json(p, existing)


def add_workspace_search_root(root: str,
                              changed_by: Optional[int] = None) -> Tuple[bool, str]:
    """Add a single search root path. Returns (success, message)."""
    from pathlib import Path as _P
    root = root.strip().strip('"').strip("'")
    if not root:
        return False, "路径不能为空"
    p = _P(root).expanduser()
    if not p.exists():
        return False, "路径不存在: {}".format(root)
    if not p.is_dir():
        return False, "路径不是目录: {}".format(root)
    resolved = str(p.resolve())
    current = get_workspace_search_roots()
    # Dedup (case-insensitive on Windows)
    if any(resolved.lower() == c.lower() for c in current):
        return False, "已存在: {}".format(resolved)
    current.append(resolved)
    set_workspace_search_roots(current, changed_by=changed_by)
    return True, resolved


def remove_workspace_search_root(index: int,
                                 changed_by: Optional[int] = None) -> Tuple[bool, str]:
    """Remove a search root by 1-based index. Returns (success, message)."""
    current = get_workspace_search_roots()
    if index < 1 or index > len(current):
        return False, "无效索引: {}（共{}项）".format(index, len(current))
    removed = current.pop(index - 1)
    set_workspace_search_roots(current, changed_by=changed_by)
    return True, removed
