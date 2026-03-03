"""Interactive button-based menu system for Telegram bot.

Provides a hierarchical click-to-interact UI organized as:
  Main Dashboard -> Quick Actions (new task, task list, screenshot)
                 -> Sub-menus (system, ops, security, workspace)
  Task Management -> Status filters + Archive Management sub-menu

Multi-step flows use a pending-action state machine:
  click button -> bot prompts for input -> user sends text -> action completes.
"""

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import load_json, save_json, send_text, tasks_root, utc_iso
from i18n import t


# ---------------------------------------------------------------------------
# Lazy translation helpers (allow module-level constants to resolve at access)
# ---------------------------------------------------------------------------

class _LazyTranslation:
    """A lazy string proxy that calls ``t(key)`` on each access.

    Supports ``.format(**kwargs)`` so that callers that do
    ``WELCOME_TEXT.format(backend=..., ...)`` keep working transparently.
    Also supports ``in`` checks, iteration, and other common str operations
    so that code like ``"/menu" in HELP_TEXT`` works correctly.
    """
    def __init__(self, key):
        self._key = key

    def _resolve(self):
        return t(self._key)

    def __str__(self):
        return self._resolve()

    def __repr__(self):
        return "_LazyTranslation({!r})".format(self._key)

    def format(self, **kwargs):
        return t(self._key, **kwargs)

    def __contains__(self, item):
        return item in self._resolve()

    def __iter__(self):
        return iter(self._resolve())

    def __len__(self):
        return len(self._resolve())

    def __eq__(self, other):
        if isinstance(other, str):
            return self._resolve() == other
        if isinstance(other, _LazyTranslation):
            return self._resolve() == other._resolve()
        return NotImplemented

    def __hash__(self):
        return hash(self._resolve())

    def __add__(self, other):
        return self._resolve() + str(other)

    def __radd__(self, other):
        return str(other) + self._resolve()

    def __getattr__(self, name):
        # Delegate any other attribute access (splitlines, strip, etc.) to the resolved string
        return getattr(self._resolve(), name)


class _TranslatedDict:
    """A dict-like object that resolves values via ``t(prefix.key)`` on access."""
    def __init__(self, prefix):
        self._prefix = prefix
        self._keys = []

    def _with_keys(self, keys):
        self._keys = list(keys)
        return self

    def __getitem__(self, key):
        return t("{}.{}".format(self._prefix, key))

    def get(self, key, default=None):
        val = t("{}.{}".format(self._prefix, key))
        raw_key = "{}.{}".format(self._prefix, key)
        return val if val != raw_key else (default if default is not None else val)

    def __contains__(self, key):
        return key in self._keys

    def items(self):
        return [(k, self[k]) for k in self._keys]

    def keys(self):
        return list(self._keys)

    def values(self):
        return [self[k] for k in self._keys]


# ---------------------------------------------------------------------------
# Callback data helpers
# ---------------------------------------------------------------------------

def safe_callback_data(prefix: str, identifier: str, max_bytes: int = 64) -> str:
    """Build ``prefix:identifier`` and truncate *identifier* so the result
    fits within *max_bytes* UTF-8 bytes (Telegram hard limit).
    """
    full = "{}:{}".format(prefix, identifier)
    if len(full.encode("utf-8")) <= max_bytes:
        return full
    # Reserve bytes for "prefix:" part
    prefix_part = "{}:".format(prefix)
    avail = max_bytes - len(prefix_part.encode("utf-8"))
    if avail <= 0:
        return prefix_part[:max_bytes]
    # Truncate identifier by bytes without breaking multi-byte chars
    enc = identifier.encode("utf-8")
    truncated = enc[:avail].decode("utf-8", errors="ignore").rstrip("-")
    return "{}{}".format(prefix_part, truncated)


# ---------------------------------------------------------------------------
# Pending-action state (file-backed for persistence across restarts)
# ---------------------------------------------------------------------------

def _pending_state_file() -> Path:
    return tasks_root() / "state" / "pending_actions.json"


def _load_pending_states() -> Dict:
    path = _pending_state_file()
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def _save_pending_states(states: Dict) -> None:
    save_json(_pending_state_file(), states)


def set_pending_action(chat_id: int, user_id: int, action: str, context: Optional[Dict] = None) -> None:
    """Register a pending action for a chat+user pair."""
    states = _load_pending_states()
    key = "{}:{}".format(chat_id, user_id)
    states[key] = {
        "action": action,
        "context": context or {},
        "created_at": utc_iso(),
    }
    _save_pending_states(states)


def get_pending_action(chat_id: int, user_id: int) -> Optional[Dict]:
    """Retrieve and clear the pending action for a chat+user pair."""
    states = _load_pending_states()
    key = "{}:{}".format(chat_id, user_id)
    entry = states.pop(key, None)
    if entry:
        _save_pending_states(states)
    return entry


def peek_pending_action(chat_id: int, user_id: int) -> Optional[Dict]:
    """Peek at the pending action without clearing it."""
    states = _load_pending_states()
    key = "{}:{}".format(chat_id, user_id)
    return states.get(key)


def clear_pending_action(chat_id: int, user_id: int) -> None:
    """Clear any pending action."""
    states = _load_pending_states()
    key = "{}:{}".format(chat_id, user_id)
    if key in states:
        del states[key]
        _save_pending_states(states)


# ---------------------------------------------------------------------------
# Keyboard builders - Main Dashboard
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> Dict:
    """Main dashboard: quick actions on top, grouped sub-menus below."""
    return {
        "inline_keyboard": [
            # ---- Quick Actions (most used) ----
            [
                {"text": t("menu.new_task"), "callback_data": "menu:new_task"},
                {"text": t("menu.task_mgmt"), "callback_data": "menu:sub_task_mgmt"},
                {"text": t("menu.skills_mgmt"), "callback_data": "menu:sub_skills"},
            ],
            # ---- Sub-menu entries ----
            [
                {"text": t("menu.system_settings"), "callback_data": "menu:sub_system"},
                {"text": t("menu.ops"), "callback_data": "menu:sub_ops"},
            ],
            [
                {"text": t("menu.security"), "callback_data": "menu:sub_security"},
                {"text": t("menu.workspace_mgmt"), "callback_data": "menu:sub_workspace"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Keyboard builders - Sub-menus
# ---------------------------------------------------------------------------

def system_menu_keyboard() -> Dict:
    """Sub-menu: system configuration."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.info"), "callback_data": "menu:info"},
                {"text": t("menu.switch_backend"), "callback_data": "menu:switch_backend"},
            ],
            [
                {"text": t("menu.switch_model"), "callback_data": "menu:switch_model"},
                {"text": t("menu.model_list"), "callback_data": "menu:model_list"},
            ],
            [
                {"text": t("menu.pipeline_config"), "callback_data": "menu:pipeline_config"},
                {"text": t("menu.pipeline_status"), "callback_data": "menu:pipeline_status"},
            ],
            [
                {"text": t("menu.role_pipeline_config"), "callback_data": "menu:role_pipeline_config"},
            ],
            [
                {"text": t("menu.switch_language"), "callback_data": "menu:switch_language"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


def language_select_keyboard() -> Dict:
    """Language selection keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": t("language.zh"), "callback_data": "menu:lang_zh"},
                {"text": t("language.en"), "callback_data": "menu:lang_en"},
            ],
            [
                {"text": t("menu.back_system"), "callback_data": "menu:sub_system"},
            ],
        ]
    }


def archive_menu_keyboard() -> Dict:
    """Sub-menu: archive management."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.archive_overview"), "callback_data": "menu:archive"},
            ],
            [
                {"text": t("menu.archive_search"), "callback_data": "menu:archive_search"},
                {"text": t("menu.archive_log"), "callback_data": "menu:archive_log"},
            ],
            [
                {"text": t("menu.archive_detail"), "callback_data": "menu:archive_show"},
            ],
            [
                {"text": t("menu.back_task_mgmt"), "callback_data": "menu:sub_task_mgmt"},
            ],
        ]
    }


def ops_menu_keyboard() -> Dict:
    """Sub-menu: operations & workspace management."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.mgr_restart"), "callback_data": "menu:mgr_restart"},
                {"text": t("menu.self_update"), "callback_data": "menu:mgr_reinit"},
            ],
            [
                {"text": t("menu.ops_restart_all"), "callback_data": "menu:ops_restart"},
                {"text": t("menu.mgr_status"), "callback_data": "menu:mgr_status"},
            ],
            [
                {"text": t("menu.switch_workspace"), "callback_data": "menu:set_workspace"},
                {"text": t("menu.reset_workspace"), "callback_data": "menu:reset_workspace"},
            ],
            [
                {"text": t("menu.workspace_list"), "callback_data": "menu:workspace_list"},
                {"text": t("menu.workspace_add"), "callback_data": "menu:workspace_add"},
            ],
            [
                {"text": t("menu.dispatch_status"), "callback_data": "menu:dispatch_status"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


def skills_menu_keyboard() -> Dict:
    """Sub-menu: skills management."""
    from config import get_skill_english_practice
    eng_on = get_skill_english_practice()
    eng_label = t("menu.skill_eng_practice_on") if eng_on else t("menu.skill_eng_practice_off")
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.screenshot"), "callback_data": "menu:screenshot"},
                {"text": t("menu.project_summary"), "callback_data": "menu:summary"},
            ],
            [
                {"text": eng_label, "callback_data": "menu:skill_eng_practice_toggle"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


def workspace_menu_keyboard() -> Dict:
    """Sub-menu: workspace management."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.workspace_list"), "callback_data": "menu:workspace_list"},
                {"text": t("menu.workspace_add"), "callback_data": "menu:workspace_add"},
            ],
            [
                {"text": t("menu.workspace_remove"), "callback_data": "menu:workspace_remove"},
                {"text": t("menu.workspace_set_default"), "callback_data": "menu:workspace_set_default"},
            ],
            [
                {"text": t("menu.search_roots"), "callback_data": "menu:workspace_search_roots"},
            ],
            [
                {"text": t("menu.queue_status"), "callback_data": "menu:workspace_queue_status"},
                {"text": t("menu.dispatch_status"), "callback_data": "menu:dispatch_status"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


def search_roots_keyboard(roots: list) -> Dict:
    """Sub-view: manage workspace search roots.

    Shows current roots with remove buttons, plus an add button.
    """
    rows: list = []
    for idx, root in enumerate(roots, 1):
        display = root
        if len(display) > 50:
            display = "...{}".format(display[-47:])
        rows.append([
            {"text": "\u2796 {}".format(display),
             "callback_data": "sr_remove:{}".format(idx)},
        ])
    rows.append([{"text": t("menu.add_search_root"), "callback_data": "menu:search_root_add"}])
    rows.append([{"text": t("menu.back_workspace"), "callback_data": "menu:sub_workspace"}])
    return {"inline_keyboard": rows}


def workspace_select_keyboard(workspaces: List[Dict], callback_prefix: str = "ws_select") -> Dict:
    """Build a dynamic keyboard for workspace selection.

    Each workspace gets a button showing 'label (path)' format.
    Default workspace is marked with ⭐, inactive with ⛔.
    callback_data: <prefix>:<ws_id>
    """
    rows: List[List[Dict]] = []
    for ws in workspaces:
        label = ws.get("label", ws.get("id", "?"))
        ws_id = ws.get("id", "")
        ws_path = ws.get("path", "")
        flags = []
        if ws.get("is_default"):
            flags.append("\u2b50")
        if not ws.get("active", True):
            flags.append("\u26d4")
        flag_str = " ".join(flags) + " " if flags else ""
        # Show label (path) format; truncate path if too long for Telegram button
        if ws_path:
            display = "{}{} ({})".format(flag_str, label, ws_path)
            if len(display) > 60:
                max_path = 60 - len(flag_str) - len(label) - 6
                if max_path > 10:
                    display = "{}{} ({}...)".format(flag_str, label, ws_path[:max_path])
                else:
                    display = "{}{}".format(flag_str, label)
        else:
            display = "{}{}".format(flag_str, label)
        rows.append([{"text": display, "callback_data": "{}:{}".format(callback_prefix, ws_id)}])
    rows.append([{"text": t("menu.cancel"), "callback_data": "menu:cancel"}])
    return {"inline_keyboard": rows}


def fuzzy_workspace_add_keyboard(candidates: list, callback_prefix: str = "ws_fuzzy_add") -> Dict:
    """Build a dynamic keyboard for fuzzy workspace search results.

    Each candidate gets a button with callback_data: <prefix>:<1-based-index>
    candidates is a list of Path objects.
    """
    rows: List[List[Dict]] = []
    for idx, path in enumerate(candidates, 1):
        display = "{} ({})".format(path.name, str(path.parent))
        # Truncate display to avoid Telegram text limits
        if len(display) > 60:
            display = "{}...{}".format(display[:30], display[-27:])
        rows.append([{"text": display, "callback_data": "{}:{}".format(callback_prefix, idx)}])
    rows.append([{"text": t("menu.cancel"), "callback_data": "menu:cancel"}])
    return {"inline_keyboard": rows}


def security_menu_keyboard() -> Dict:
    """Sub-menu: security & identity."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.init_2fa"), "callback_data": "menu:auth_init"},
                {"text": t("menu.auth_status"), "callback_data": "menu:auth_status"},
            ],
            [
                {"text": t("menu.whoami"), "callback_data": "menu:whoami"},
                {"text": t("menu.otp_debug"), "callback_data": "menu:auth_debug"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Keyboard builders - Selection & Actions
# ---------------------------------------------------------------------------

def backend_select_keyboard() -> Dict:
    """Inline keyboard for backend selection."""
    return {
        "inline_keyboard": [
            [
                {"text": "Codex", "callback_data": "backend_sel:codex"},
                {"text": "Claude", "callback_data": "backend_sel:claude"},
                {"text": "Pipeline", "callback_data": "backend_sel:pipeline"},
            ],
            [
                {"text": t("menu.back_system"), "callback_data": "menu:sub_system"},
            ],
        ]
    }


def pipeline_preset_keyboard() -> Dict:
    """Inline keyboard for pipeline preset selection."""
    return {
        "inline_keyboard": [
            [
                {"text": "plan + code + verify", "callback_data": "pipeline_preset:plan_code_verify"},
            ],
            [
                {"text": "plan + code", "callback_data": "pipeline_preset:plan_code"},
            ],
            [
                {"text": "code + verify", "callback_data": "pipeline_preset:code_verify"},
            ],
            [
                {"text": "claude + codex", "callback_data": "pipeline_preset:claude_codex"},
            ],
            [
                {"text": t("menu.role_pipeline"), "callback_data": "pipeline_preset:role_pipeline"},
            ],
            [
                {"text": t("menu.custom_config"), "callback_data": "menu:pipeline_config_custom"},
            ],
            [
                {"text": t("menu.back_system"), "callback_data": "menu:sub_system"},
            ],
        ]
    }


def role_pipeline_config_keyboard(stages: List[Dict]) -> Dict:
    """Keyboard showing role pipeline config with per-role model selection buttons."""
    from config import ROLE_DEFINITIONS
    rows: List[List[Dict]] = []
    for stage in stages:
        name = stage.get("name", "?")
        role_def = ROLE_DEFINITIONS.get(name, {})
        emoji = role_def.get("emoji", "")
        label = role_def.get("label", name)
        model = stage.get("model", "")
        provider = stage.get("provider", "")
        if model:
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            btn_text = "{} {} ({} {})".format(emoji, t("task.configure_model", label=label), model, tag).strip()
        else:
            btn_text = "{} {}".format(emoji, t("task.configure_model_global", label=label))
        rows.append([{"text": btn_text, "callback_data": "role_cfg:{}".format(name)}])
    rows.append([{"text": t("menu.back_system"), "callback_data": "menu:sub_system"}])
    return {"inline_keyboard": rows}


def role_model_select_keyboard(role_name: str, models: List[Dict]) -> Dict:
    """Build a dynamic keyboard for selecting a model for a specific role.

    Groups models by provider with section headers. Unavailable models
    are shown with a warning prefix so users know the key isn't configured.
    """
    from model_registry import make_label
    rows: List[List[Dict]] = []
    # Group by provider, preserving order within each group
    grouped: Dict[str, List[Dict]] = {}
    for m in models:
        grouped.setdefault(m.get("provider", "unknown"), []).append(m)
    provider_labels = {"anthropic": "\u2500\u2500 Anthropic (Claude) \u2500\u2500", "openai": "\u2500\u2500 OpenAI (Codex) \u2500\u2500"}
    for provider in ["anthropic", "openai"]:
        provider_models = grouped.get(provider)
        if not provider_models:
            continue
        # Section header (non-clickable label shown as a disabled-looking button)
        rows.append([{"text": provider_labels.get(provider, provider),
                       "callback_data": "noop:section"}])
        for m in provider_models:
            available = m.get("status", "available") == "available"
            label = make_label(m)
            if not available:
                reason = m.get("unavailable_reason", "")
                label = "\u26a0 {} ({})".format(label, reason[:20]) if reason else "\u26a0 " + label
            cb_data = "role_model:{}:{}:{}".format(role_name, m["provider"], m["id"])
            rows.append([{"text": label, "callback_data": cb_data}])
    rows.append([{"text": t("menu.back_role_config"), "callback_data": "menu:role_pipeline_config"}])
    return {"inline_keyboard": rows}


def pipeline_stage_overview_keyboard(stages: List[Dict]) -> Dict:
    """Inline keyboard for the pipeline stage overview page.

    Each stage is shown as a button with its emoji, name, and current model.
    Bottom rows contain "confirm apply" and "back to preset selection" buttons.

    Args:
        stages: list of dicts with keys name/backend/model/provider.
    """
    from config import ROLE_DEFINITIONS, STAGE_EMOJI
    rows: List[List[Dict]] = []
    for idx, stage in enumerate(stages):
        name = stage.get("name", "?")
        model = stage.get("model", "")
        provider = stage.get("provider", "")
        # Resolve emoji: try role definitions first, then stage emoji map
        role_def = ROLE_DEFINITIONS.get(name)
        if role_def:
            emoji = role_def.get("emoji", "")
        else:
            emoji = STAGE_EMOJI.get(name, "\u2699\ufe0f")  # fallback
        # Build display text
        if model:
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            btn_text = "{} {}: {} {}".format(emoji, name, model, tag).strip()
        else:
            btn_text = "{} {}: {}".format(emoji, name, t("task.stage_default"))
        rows.append([{"text": btn_text,
                       "callback_data": "pipeline_stage_cfg:{}".format(idx)}])
    rows.append([{"text": t("menu.confirm_apply"), "callback_data": "pipeline_apply"}])
    rows.append([{"text": t("menu.back_preset"), "callback_data": "menu:pipeline_config"}])
    return {"inline_keyboard": rows}


def pipeline_stage_model_keyboard(stage_index: int, stage_name: str,
                                  models: List[Dict]) -> Dict:
    """Inline keyboard for selecting a model for a specific pipeline stage.

    Reuses the same provider-grouped layout as ``role_model_select_keyboard``
    but with ``stage_model:{index}:{provider}:{model_id}`` callback data.

    Args:
        stage_index: zero-based stage index in the pending stages list.
        stage_name: human-readable stage name for display purposes.
        models: unified model list from ``get_available_models()``.
    """
    from model_registry import make_label
    rows: List[List[Dict]] = []
    grouped: Dict[str, List[Dict]] = {}
    for m in models:
        grouped.setdefault(m.get("provider", "unknown"), []).append(m)
    provider_labels = {"anthropic": "\u2500\u2500 Anthropic (Claude) \u2500\u2500",
                       "openai": "\u2500\u2500 OpenAI \u2500\u2500"}
    for provider in ["anthropic", "openai"]:
        provider_models = grouped.get(provider)
        if not provider_models:
            continue
        rows.append([{"text": provider_labels.get(provider, provider),
                       "callback_data": "noop:section"}])
        for m in provider_models:
            available = m.get("status", "available") == "available"
            label = make_label(m)
            if not available:
                reason = m.get("unavailable_reason", "")
                label = "\u26d4 {} ({})".format(label, reason[:20]) if reason else "\u26d4 " + label
            cb_data = "stage_model:{}:{}:{}".format(stage_index, m["provider"], m["id"])
            rows.append([{"text": label, "callback_data": cb_data}])
    rows.append([{"text": t("menu.back_overview"),
                   "callback_data": "menu:pipeline_stage_overview"}])
    return {"inline_keyboard": rows}


def model_list_keyboard(models: List[Dict], current_default: str = "") -> Dict:
    """Build keyboard for model list page with set-default and refresh buttons."""
    rows: List[List[Dict]] = []
    for m in models:
        if m.get("status") == "available":
            if m["id"] == current_default:
                rows.append([{"text": "{}: {} {}".format(
                    t("task.current_default"),
                    "[C]" if m["provider"] == "anthropic" else "[O]", m["id"]),
                    "callback_data": "model_default:{}:{}".format(m["provider"], m["id"])}])
            else:
                rows.append([{"text": "{}: {} {}".format(
                    t("task.set_as_default"),
                    "[C]" if m["provider"] == "anthropic" else "[O]", m["id"]),
                    "callback_data": "model_default:{}:{}".format(m["provider"], m["id"])}])
    rows.append([{"text": t("menu.refresh"), "callback_data": "menu:model_list_refresh"}])
    rows.append([{"text": t("menu.back_system"), "callback_data": "menu:sub_system"}])
    return {"inline_keyboard": rows}


def cancel_keyboard() -> Dict:
    """Cancel button for pending-action flows."""
    return {
        "inline_keyboard": [
            [{"text": t("menu.cancel"), "callback_data": "menu:cancel"}],
        ]
    }


def back_to_menu_keyboard() -> Dict:
    """Back to main menu button."""
    return {
        "inline_keyboard": [
            [{"text": t("menu.back_main"), "callback_data": "menu:main"}],
        ]
    }


def pending_tasks_keyboard(tasks: List[Dict], action_prefix: str, show_desc: bool = True) -> Dict:
    """Build an inline keyboard for selecting a task from a list.

    Args:
        tasks: Task dicts, each containing task_code + text/summary.
        action_prefix: Callback prefix (accept/reject/retry/cancel).
        show_desc: Whether to include short description in button text.
    Returns:
        Telegram InlineKeyboardMarkup dict.
    """
    rows: List[List[Dict]] = []
    for tk in tasks[:20]:
        code = str(tk.get("task_code") or "-")
        if show_desc:
            desc = str(tk.get("text") or tk.get("summary") or "").strip()
            if len(desc) > 20:
                desc = desc[:20] + "..."
            label = "{} | {}".format(code, desc) if desc else code
        else:
            label = code
        cb = safe_callback_data(action_prefix, code)
        rows.append([{"text": label, "callback_data": cb}])
    rows.append([{"text": t("menu.back_main"), "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def task_list_action_keyboard() -> Dict:
    """Keyboard shown below task list: clear all + back to menu."""
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.clear_tasks"), "callback_data": "menu:clear_tasks"},
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Keyboard builders - Task Management Sub-menu
# ---------------------------------------------------------------------------

# Status display names for task management menu (lazy, resolves via t())
TASK_STATUS_LABELS = _TranslatedDict("task_status_label")._with_keys(
    ["pending", "processing", "pending_acceptance", "rejected", "accepted", "failed", "archived"])

TASK_STATUS_EMPTY_LABELS = _TranslatedDict("task_status_empty")._with_keys(
    ["pending", "processing", "pending_acceptance", "rejected", "accepted", "failed", "archived"])


def task_mgmt_menu_keyboard() -> Dict:
    """Sub-menu: task management with status-based filtering."""
    return {
        "inline_keyboard": [
            [
                {"text": TASK_STATUS_LABELS["pending"], "callback_data": "menu:tasks_pending"},
                {"text": TASK_STATUS_LABELS["processing"], "callback_data": "menu:tasks_processing"},
            ],
            [
                {"text": TASK_STATUS_LABELS["pending_acceptance"], "callback_data": "menu:tasks_pending_acceptance"},
                {"text": TASK_STATUS_LABELS["rejected"], "callback_data": "menu:tasks_rejected"},
            ],
            [
                {"text": TASK_STATUS_LABELS["accepted"], "callback_data": "menu:tasks_accepted"},
                {"text": TASK_STATUS_LABELS["failed"], "callback_data": "menu:tasks_failed"},
            ],
            [
                {"text": TASK_STATUS_LABELS["archived"], "callback_data": "menu:tasks_archived"},
                {"text": t("menu.refresh"), "callback_data": "menu:tasks_overview"},
            ],
            [
                {"text": t("menu.archive_mgmt"), "callback_data": "menu:sub_archive"},
            ],
            [
                {"text": t("menu.back_main"), "callback_data": "menu:main"},
            ],
        ]
    }


def task_status_list_keyboard(
    tasks: List[Dict],
    status_key: str,
    page: int = 0,
    page_size: int = 5,
) -> Dict:
    """Build paginated task list keyboard for a given status filter.

    Each task becomes a button: [T00XX] description...
    Includes pagination and back button.
    """
    total = len(tasks)
    start = page * page_size
    end = min(start + page_size, total)
    page_tasks = tasks[start:end]

    rows: List[List[Dict]] = []
    for tk in page_tasks:
        code = str(tk.get("task_code") or "-")
        text = str(tk.get("text") or tk.get("summary") or "").strip()
        if len(text) > 20:
            text = text[:20] + "..."
        label = "[{}] {}".format(code, text)
        # For archived tasks, use archive_detail callback with fallback
        if status_key == "archived":
            archive_id = str(tk.get("archive_id") or "").strip()
            if not archive_id:
                archive_id = str(tk.get("task_id") or tk.get("task_code") or code).strip()
            cb = safe_callback_data("archive_detail", archive_id)
        else:
            cb = "task_detail:{}".format(code)
        rows.append([{"text": label, "callback_data": cb}])

    # Pagination buttons
    nav_row: List[Dict] = []
    if page > 0:
        nav_row.append({"text": t("menu.prev_page"), "callback_data": "tasks_page:{}:{}".format(status_key, page - 1)})
    if end < total:
        nav_row.append({"text": t("menu.next_page"), "callback_data": "tasks_page:{}:{}".format(status_key, page + 1)})
    if nav_row:
        rows.append(nav_row)

    rows.append([{"text": t("menu.back_task_mgmt"), "callback_data": "menu:sub_task_mgmt"}])
    return {"inline_keyboard": rows}


def task_detail_keyboard(task_code: str, status: str, is_pipeline: bool = False) -> Dict:
    """Build action keyboard for a task detail page based on its status.

    Different statuses get different action buttons.
    When is_pipeline=True, adds a "view stage detail" button.
    """
    rows: List[List[Dict]] = []
    s = str(status or "").strip().lower()
    code = str(task_code or "")

    if s == "pending":
        rows.append([
            {"text": t("task.view_detail"), "callback_data": "task_doc:{}".format(code)},
            {"text": t("task.cancel_task"), "callback_data": "task_cancel:{}".format(code)},
        ])
        back_cb = "menu:tasks_pending"
    elif s == "processing":
        rows.append([
            {"text": t("task.view_progress"), "callback_data": "status:{}".format(code)},
            {"text": t("task.view_events"), "callback_data": "events:{}".format(code)},
        ])
        back_cb = "menu:tasks_processing"
    elif s == "pending_acceptance":
        rows.append([
            {"text": t("task.accept"), "callback_data": "accept:{}".format(code)},
            {"text": t("task.reject"), "callback_data": "reject:{}".format(code)},
        ])
        rows.append([
            {"text": t("task.view_doc"), "callback_data": "task_doc:{}".format(code)},
            {"text": t("task.view_summary"), "callback_data": "task_summary:{}".format(code)},
        ])
        rows.append([
            {"text": t("task.view_full_log"), "callback_data": "task_log:{}".format(code)},
        ])
        back_cb = "menu:tasks_pending_acceptance"
    elif s == "rejected":
        rows.append([
            {"text": t("task.retry"), "callback_data": "retry:{}".format(code)},
            {"text": t("task.change_to_accept"), "callback_data": "accept:{}".format(code)},
        ])
        rows.append([
            {"text": t("task.view_doc"), "callback_data": "task_doc:{}".format(code)},
            {"text": t("task.delete_task"), "callback_data": "task_delete:{}".format(code)},
        ])
        back_cb = "menu:tasks_rejected"
    elif s in ("accepted", "completed"):
        rows.append([
            {"text": t("task.view_doc"), "callback_data": "task_doc:{}".format(code)},
            {"text": t("task.view_summary"), "callback_data": "task_summary:{}".format(code)},
        ])
        rows.append([
            {"text": t("task.view_full_log"), "callback_data": "task_log:{}".format(code)},
        ])
        back_cb = "menu:tasks_accepted"
    elif s in ("failed", "timeout"):
        rows.append([
            {"text": t("task.retry_short"), "callback_data": "retry:{}".format(code)},
            {"text": t("task.view_error"), "callback_data": "task_doc:{}".format(code)},
        ])
        rows.append([
            {"text": t("task.view_full_log"), "callback_data": "task_log:{}".format(code)},
            {"text": t("task.delete_task"), "callback_data": "task_delete:{}".format(code)},
        ])
        back_cb = "menu:tasks_failed"
    else:
        rows.append([
            {"text": t("task.view_detail"), "callback_data": "task_doc:{}".format(code)},
        ])
        back_cb = "menu:sub_task_mgmt"

    if is_pipeline:
        rows.append([
            {"text": t("task.view_stage_detail"), "callback_data": "stage_detail:{}".format(code)},
        ])
    rows.append([{"text": t("menu.back_list"), "callback_data": back_cb}])
    return {"inline_keyboard": rows}


def tasks_overview_keyboard(counts: Dict[str, int]) -> Dict:
    """Build overview statistics keyboard. Each line is a clickable button."""
    status_keys = [
        ("pending", "menu:tasks_pending"),
        ("processing", "menu:tasks_processing"),
        ("pending_acceptance", "menu:tasks_pending_acceptance"),
        ("rejected", "menu:tasks_rejected"),
        ("accepted", "menu:tasks_accepted"),
        ("failed", "menu:tasks_failed"),
        ("archived", "menu:tasks_archived"),
    ]
    rows: List[List[Dict]] = []
    for key, cb in status_keys:
        count = int(counts.get(key, 0))
        label = t("overview_label.{}".format(key))
        rows.append([{"text": "{}: {}".format(label, count), "callback_data": cb}])
    rows.append([{"text": t("menu.back_task_mgmt"), "callback_data": "menu:sub_task_mgmt"}])
    return {"inline_keyboard": rows}


def archive_detail_keyboard(archive_id: str) -> Dict:
    """Action keyboard for an archived task detail."""
    return {
        "inline_keyboard": [
            [
                {"text": t("task.view_archive_detail"), "callback_data": safe_callback_data("task_doc", archive_id)},
                {"text": t("task.delete_archive"), "callback_data": safe_callback_data("archive_delete", archive_id)},
            ],
            [
                {"text": t("menu.back_list"), "callback_data": "menu:tasks_archived"},
            ],
        ]
    }


def confirm_cancel_keyboard(action: str, context_str: str = "") -> Dict:
    """Confirm + cancel keyboard for dangerous operations."""
    if context_str:
        cb_data = safe_callback_data("confirm:{}".format(action), context_str)
    else:
        cb_data = "confirm:{}".format(action)
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.confirm_execute"), "callback_data": cb_data},
                {"text": t("menu.cancel"), "callback_data": "menu:cancel"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Welcome / help text (lazy: resolved via t() at access time)
# ---------------------------------------------------------------------------

WELCOME_TEXT = _LazyTranslation("welcome.title")

HELP_TEXT = _LazyTranslation("help.text")

SUBMENU_TEXTS = {
    "system": _LazyTranslation("submenu.system"),
    "archive": _LazyTranslation("submenu.archive"),
    "ops": _LazyTranslation("submenu.ops"),
    "security": _LazyTranslation("submenu.security"),
    "workspace": _LazyTranslation("submenu.workspace"),
    "skills": _LazyTranslation("submenu.skills"),
    "task_mgmt": _LazyTranslation("submenu.task_mgmt"),
}


# ---------------------------------------------------------------------------
# Prompt text for each pending action (lazy: resolved via t() at access time)
# ---------------------------------------------------------------------------

PENDING_PROMPTS = {
    "new_task": _LazyTranslation("prompt.new_task"),
    "screenshot": _LazyTranslation("prompt.screenshot"),
    "archive_search": _LazyTranslation("prompt.archive_search"),
    "archive_show": _LazyTranslation("prompt.archive_show"),
    "archive_log": _LazyTranslation("prompt.archive_log"),
    "mgr_restart": _LazyTranslation("prompt.mgr_restart"),
    "mgr_reinit": _LazyTranslation("prompt.mgr_reinit"),
    "ops_restart": _LazyTranslation("prompt.ops_restart"),
    "set_workspace": _LazyTranslation("prompt.set_workspace"),
    "reset_workspace": _LazyTranslation("prompt.reset_workspace"),
    "auth_debug": _LazyTranslation("prompt.auth_debug"),
    "pipeline_config_custom": _LazyTranslation("prompt.pipeline_config_custom"),
    "workspace_add": _LazyTranslation("prompt.workspace_add"),
    "workspace_remove": _LazyTranslation("prompt.workspace_remove"),
    "workspace_set_default": _LazyTranslation("prompt.workspace_set_default"),
    "search_root_add": _LazyTranslation("prompt.search_root_add"),
    "new_task_with_workspace": _LazyTranslation("prompt.new_task_with_workspace"),
    "eng_practice_input": _LazyTranslation("prompt.eng_practice_input"),
    "eng_practice_confirm": _LazyTranslation("prompt.eng_practice_confirm"),
}


def eng_practice_confirm_keyboard() -> Dict:
    """Confirmation keyboard after AI English evaluation."""
    return {
        "inline_keyboard": [
            [{"text": t("menu.eng_confirm_correct"), "callback_data": "menu:eng_confirm_correct"}],
            [{"text": t("menu.eng_confirm_chinese"), "callback_data": "menu:eng_confirm_chinese"}],
            [{"text": t("menu.eng_confirm_retry"), "callback_data": "menu:eng_confirm_retry"}],
        ]
    }
