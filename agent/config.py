"""
agent_config.py - Runtime agent backend configuration.

Stores the active backend (codex / claude / pipeline) in
state/agent_config.json so it persists across restarts and can be
changed without editing .env.

Priority: agent_config.json > AGENT_BACKEND env > "pipeline" default

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
KNOWN_STAGE_BACKENDS = {"codex", "claude", "openai"}

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
    "role_pipeline": [
        {"name": "pm",   "backend": "claude"},
        {"name": "dev",  "backend": "claude"},
        {"name": "test", "backend": "claude"},
        {"name": "qa",   "backend": "claude"},
    ],
}

# ── Role pipeline definitions ────────────────────────────────────────────────

# Standard role definitions for the role pipeline
ROLE_DEFINITIONS: Dict[str, Dict] = {
    "pm":   {"label": "产品经理", "emoji": "\U0001f4cb", "default_backend": "claude"},
    "dev":  {"label": "开发",     "emoji": "\U0001f4bb", "default_backend": "claude"},
    "test": {"label": "测试",     "emoji": "\U0001f9ea", "default_backend": "claude"},
    "qa":   {"label": "QA",       "emoji": "\u2705",     "default_backend": "claude"},
}

ROLE_PIPELINE_ORDER = ["pm", "dev", "test", "qa"]


def _config_path():
    return tasks_root() / "state" / "agent_config.json"


def _parse_pipeline_stages(raw: str) -> List[Dict]:
    """Parse 'plan:openai code:claude verify:codex' into a stage list."""
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
    return os.getenv("AGENT_BACKEND", "pipeline")


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
    # Fall back to env var: TASK_PIPELINE_STAGES="plan:openai code:claude verify:codex"
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
    """Human-readable stage list showing model name when available.

    With model: 'pm(claude-opus-4-6 [C]) → dev(claude-opus-4-6 [C])'
    Without model (uses global): 'plan(全局: claude-opus-4-6 [C])'
    Without model (no global): 'plan(claude) → verify(codex)'
    """
    if not stages:
        return "(empty)"
    parts = []
    for s in stages:
        name = s.get("name", "?")
        backend = s.get("backend", "?")
        model = s.get("model", "")
        provider = s.get("provider", "")
        if model:
            tag = _provider_tag(provider)
            display = "{} {}".format(model, tag).rstrip() if tag else model
            parts.append("{}({})".format(name, display))
        else:
            global_model = get_claude_model()
            if global_model:
                global_provider = get_model_provider()
                tag = _provider_tag(global_provider)
                display = "{} {}".format(global_model, tag).rstrip() if tag else global_model
                parts.append("{}(全局: {})".format(name, display))
            else:
                parts.append("{}({})".format(name, backend))
    return " → ".join(parts)


def _provider_tag(provider: str) -> str:
    """Return short provider tag like [C] for anthropic, [O] for openai."""
    if provider == "anthropic":
        return "[C]"
    if provider == "openai":
        return "[O]"
    return ""


# ── Role pipeline stages ─────────────────────────────────────────────────────

def get_role_pipeline_stages() -> List[Dict]:
    """Return the configured role pipeline stages, or defaults if none."""
    p = _config_path()
    if p.exists():
        try:
            data = load_json(p)
            stages = data.get("role_pipeline_stages")
            if isinstance(stages, list) and stages:
                return stages
        except Exception:
            pass
    # Return default role pipeline stages
    return [
        {"name": role, "backend": ROLE_DEFINITIONS[role]["default_backend"],
         "model": "", "provider": ""}
        for role in ROLE_PIPELINE_ORDER
    ]


def set_role_pipeline_stages(stages: List[Dict],
                             changed_by: Optional[int] = None) -> None:
    """Persist role pipeline stages to runtime config."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = load_json(p)
        except Exception:
            pass
    existing["role_pipeline_stages"] = stages
    existing["updated_at"] = utc_iso()
    if changed_by is not None:
        existing["changed_by"] = changed_by
    save_json(p, existing)


def set_role_stage_model(role_name: str, model: str, provider: str = "",
                         changed_by: Optional[int] = None,
                         validate: bool = True) -> None:
    """Set the model for a specific role in the role pipeline.

    If validate=True (default), checks the model is in the available model list
    and raises ValueError if it is unavailable.
    """
    if validate and model:
        from model_registry import get_available_models, find_model
        m = find_model(model)
        if m and m.get("status") == "unavailable":
            reason = m.get("unavailable_reason", "不可用")
            raise ValueError("模型 {} 当前不可用（{}）".format(model, reason))
    stages = get_role_pipeline_stages()
    for stage in stages:
        if stage.get("name") == role_name:
            stage["model"] = model
            stage["provider"] = provider
            break
    else:
        # Role not found in stages, should not happen with valid input
        return
    set_role_pipeline_stages(stages, changed_by=changed_by)


def format_role_pipeline_stages(stages: List[Dict]) -> str:
    """Human-readable role pipeline display."""
    if not stages:
        return "(未配置)"
    lines = []
    for s in stages:
        name = s.get("name", "?")
        role_def = ROLE_DEFINITIONS.get(name, {})
        emoji = role_def.get("emoji", "")
        label = role_def.get("label", name)
        model = s.get("model", "")
        provider = s.get("provider", "")
        if model:
            tag = _provider_tag(provider)
            lines.append("{} {} \u2192 {} {}".format(emoji, label, model, tag).strip())
        else:
            global_model = get_claude_model()
            if global_model:
                global_provider = get_model_provider()
                tag = _provider_tag(global_provider)
                display = "{} {}".format(global_model, tag).rstrip() if tag else global_model
                lines.append("{} {} \u2192 \u5168\u5c40: {}".format(emoji, label, display))
            else:
                lines.append("{} {} \u2192 (\u4f7f\u7528\u5168\u5c40\u6a21\u578b)".format(emoji, label))
    return "\n".join(lines)


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
