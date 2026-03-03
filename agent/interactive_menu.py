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
                {"text": "\u270f\ufe0f \u65b0\u5efa\u4efb\u52a1", "callback_data": "menu:new_task"},
                {"text": "\U0001f4cb \u4efb\u52a1\u7ba1\u7406", "callback_data": "menu:sub_task_mgmt"},
                {"text": "\U0001f9e9 \u6280\u80fd\u7ba1\u7406", "callback_data": "menu:sub_skills"},
            ],
            # ---- Sub-menu entries ----
            [
                {"text": "\u2699\ufe0f \u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"},
                {"text": "\U0001f527 \u8fd0\u7ef4\u64cd\u4f5c", "callback_data": "menu:sub_ops"},
            ],
            [
                {"text": "\U0001f512 \u5b89\u5168\u8ba4\u8bc1", "callback_data": "menu:sub_security"},
                {"text": "\U0001f4c1 \u5de5\u4f5c\u533a\u7ba1\u7406", "callback_data": "menu:sub_workspace"},
            ],
            [
                {"text": "\U0001f4ca \u9879\u76ee\u603b\u7ed3", "callback_data": "menu:summary"},
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
                {"text": "\u2139\ufe0f \u7cfb\u7edf\u4fe1\u606f", "callback_data": "menu:info"},
                {"text": "\U0001f504 \u5207\u6362\u540e\u7aef", "callback_data": "menu:switch_backend"},
            ],
            [
                {"text": "\U0001f916 \u5207\u6362\u6a21\u578b", "callback_data": "menu:switch_model"},
                {"text": "\U0001f4cb \u6a21\u578b\u6e05\u5355", "callback_data": "menu:model_list"},
            ],
            [
                {"text": "\u2699\ufe0f \u6d41\u6c34\u7ebf\u914d\u7f6e", "callback_data": "menu:pipeline_config"},
                {"text": "\U0001f4ca \u6d41\u6c34\u7ebf\u72b6\u6001", "callback_data": "menu:pipeline_status"},
            ],
            [
                {"text": "\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf\u914d\u7f6e", "callback_data": "menu:role_pipeline_config"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
            ],
        ]
    }


def archive_menu_keyboard() -> Dict:
    """Sub-menu: archive management."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4c1 \u5f52\u6863\u6982\u89c8", "callback_data": "menu:archive"},
            ],
            [
                {"text": "\U0001f50d \u5f52\u6863\u68c0\u7d22", "callback_data": "menu:archive_search"},
                {"text": "\U0001f4dc \u5f52\u6863\u65e5\u5fd7", "callback_data": "menu:archive_log"},
            ],
            [
                {"text": "\U0001f4c4 \u5f52\u6863\u8be6\u60c5", "callback_data": "menu:archive_show"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4efb\u52a1\u7ba1\u7406", "callback_data": "menu:sub_task_mgmt"},
            ],
        ]
    }


def ops_menu_keyboard() -> Dict:
    """Sub-menu: operations & workspace management."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f527 Mgr\u91cd\u542f", "callback_data": "menu:mgr_restart"},
                {"text": "\u2b06\ufe0f \u81ea\u6211\u66f4\u65b0", "callback_data": "menu:mgr_reinit"},
            ],
            [
                {"text": "\U0001f504 Ops\u5168\u90e8\u91cd\u542f", "callback_data": "menu:ops_restart"},
                {"text": "\U0001f4c8 Mgr\u72b6\u6001", "callback_data": "menu:mgr_status"},
            ],
            [
                {"text": "\U0001f4c2 \u5207\u6362\u5de5\u4f5c\u533a", "callback_data": "menu:set_workspace"},
                {"text": "\u21a9\ufe0f \u91cd\u7f6e\u5de5\u4f5c\u533a", "callback_data": "menu:reset_workspace"},
            ],
            [
                {"text": "\U0001f4c1 \u5de5\u4f5c\u76ee\u5f55\u5217\u8868", "callback_data": "menu:workspace_list"},
                {"text": "\u2795 \u6dfb\u52a0\u5de5\u4f5c\u76ee\u5f55", "callback_data": "menu:workspace_add"},
            ],
            [
                {"text": "\U0001f4ca \u8c03\u5ea6\u5668\u72b6\u6001", "callback_data": "menu:dispatch_status"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
            ],
        ]
    }


def skills_menu_keyboard() -> Dict:
    """Sub-menu: skills management."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4f7 截图", "callback_data": "menu:screenshot"},
            ],
            [
                {"text": "\u00ab 返回主菜单", "callback_data": "menu:main"},
            ],
        ]
    }


def workspace_menu_keyboard() -> Dict:
    """Sub-menu: workspace management."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4c1 \u5de5\u4f5c\u76ee\u5f55\u5217\u8868", "callback_data": "menu:workspace_list"},
                {"text": "\u2795 \u6dfb\u52a0\u5de5\u4f5c\u76ee\u5f55", "callback_data": "menu:workspace_add"},
            ],
            [
                {"text": "\u2796 \u5220\u9664\u5de5\u4f5c\u76ee\u5f55", "callback_data": "menu:workspace_remove"},
                {"text": "\u2b50 \u8bbe\u7f6e\u9ed8\u8ba4", "callback_data": "menu:workspace_set_default"},
            ],
            [
                {"text": "\U0001f50d \u641c\u7d22\u6839\u76ee\u5f55", "callback_data": "menu:workspace_search_roots"},
            ],
            [
                {"text": "\U0001f4ca \u961f\u5217\u72b6\u6001", "callback_data": "menu:workspace_queue_status"},
                {"text": "\U0001f4ca \u8c03\u5ea6\u5668\u72b6\u6001", "callback_data": "menu:dispatch_status"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
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
    rows.append([{"text": "\u2795 \u6dfb\u52a0\u641c\u7d22\u6839\u76ee\u5f55", "callback_data": "menu:search_root_add"}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u5de5\u4f5c\u533a\u7ba1\u7406", "callback_data": "menu:sub_workspace"}])
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
    rows.append([{"text": "\u00ab \u53d6\u6d88", "callback_data": "menu:cancel"}])
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
    rows.append([{"text": "\u00ab \u53d6\u6d88", "callback_data": "menu:cancel"}])
    return {"inline_keyboard": rows}


def security_menu_keyboard() -> Dict:
    """Sub-menu: security & identity."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f510 \u521d\u59cb\u53162FA", "callback_data": "menu:auth_init"},
                {"text": "\U0001f511 2FA\u72b6\u6001", "callback_data": "menu:auth_status"},
            ],
            [
                {"text": "\U0001f464 \u6211\u662f\u8c01", "callback_data": "menu:whoami"},
                {"text": "\U0001f50d OTP\u8c03\u8bd5", "callback_data": "menu:auth_debug"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
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
                {"text": "\u00ab \u8fd4\u56de\u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"},
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
                {"text": "\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf (pm\u2192dev\u2192test\u2192qa)", "callback_data": "pipeline_preset:role_pipeline"},
            ],
            [
                {"text": "\u270f\ufe0f \u81ea\u5b9a\u4e49\u914d\u7f6e", "callback_data": "menu:pipeline_config_custom"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"},
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
            btn_text = "{} \u914d\u7f6e{}\u6a21\u578b ({} {})".format(emoji, label, model, tag).strip()
        else:
            btn_text = "{} \u914d\u7f6e{}\u6a21\u578b (\u5168\u5c40)".format(emoji, label)
        rows.append([{"text": btn_text, "callback_data": "role_cfg:{}".format(name)}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"}])
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
    provider_labels = {"anthropic": "── Anthropic (Claude) ──", "openai": "── OpenAI (Codex) ──"}
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
    rows.append([{"text": "\u00ab \u8fd4\u56de\u89d2\u8272\u914d\u7f6e", "callback_data": "menu:role_pipeline_config"}])
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
            emoji = STAGE_EMOJI.get(name, "\u2699\ufe0f")  # fallback ⚙️
        # Build display text
        if model:
            tag = "[C]" if provider == "anthropic" else "[O]" if provider == "openai" else ""
            btn_text = "{} {}: {} {}".format(emoji, name, model, tag).strip()
        else:
            btn_text = "{} {}: \uff08\u9ed8\u8ba4\uff09".format(emoji, name)
        rows.append([{"text": btn_text,
                       "callback_data": "pipeline_stage_cfg:{}".format(idx)}])
    rows.append([{"text": "\u2705 \u786e\u8ba4\u5e94\u7528", "callback_data": "pipeline_apply"}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u9009\u62e9\u9884\u8bbe", "callback_data": "menu:pipeline_config"}])
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
    rows.append([{"text": "\u00ab \u8fd4\u56de\u6982\u89c8",
                   "callback_data": "menu:pipeline_stage_overview"}])
    return {"inline_keyboard": rows}


def model_list_keyboard(models: List[Dict], current_default: str = "") -> Dict:
    """Build keyboard for model list page with set-default and refresh buttons."""
    rows: List[List[Dict]] = []
    for m in models:
        if m.get("status") == "available":
            if m["id"] == current_default:
                rows.append([{"text": "\u2714 \u5f53\u524d\u9ed8\u8ba4: {} {}".format(
                    "[C]" if m["provider"] == "anthropic" else "[O]", m["id"]),
                    "callback_data": "model_default:{}:{}".format(m["provider"], m["id"])}])
            else:
                rows.append([{"text": "\u2b50 \u8bbe\u4e3a\u9ed8\u8ba4: {} {}".format(
                    "[C]" if m["provider"] == "anthropic" else "[O]", m["id"]),
                    "callback_data": "model_default:{}:{}".format(m["provider"], m["id"])}])
    rows.append([{"text": "\U0001f504 \u5237\u65b0", "callback_data": "menu:model_list_refresh"}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"}])
    return {"inline_keyboard": rows}


def cancel_keyboard() -> Dict:
    """Cancel button for pending-action flows."""
    return {
        "inline_keyboard": [
            [{"text": "\u2716 \u53d6\u6d88", "callback_data": "menu:cancel"}],
        ]
    }


def back_to_menu_keyboard() -> Dict:
    """Back to main menu button."""
    return {
        "inline_keyboard": [
            [{"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"}],
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
    for t in tasks[:20]:
        code = str(t.get("task_code") or "-")
        if show_desc:
            desc = str(t.get("text") or t.get("summary") or "").strip()
            if len(desc) > 20:
                desc = desc[:20] + "..."
            label = "{} | {}".format(code, desc) if desc else code
        else:
            label = code
        cb = safe_callback_data(action_prefix, code)
        rows.append([{"text": label, "callback_data": cb}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def task_list_action_keyboard() -> Dict:
    """Keyboard shown below task list: clear all + back to menu."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f5d1 \u6e05\u7a7a\u4efb\u52a1\u5217\u8868", "callback_data": "menu:clear_tasks"},
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Keyboard builders - Task Management Sub-menu
# ---------------------------------------------------------------------------

# Status display names for task management menu
TASK_STATUS_LABELS = {
    "pending": "\U0001f550 \u5f85\u5904\u7406\u4efb\u52a1",
    "processing": "\u2699\ufe0f \u6267\u884c\u4e2d\u4efb\u52a1",
    "pending_acceptance": "\U0001f4cb \u5f85\u9a8c\u6536\u4efb\u52a1",
    "rejected": "\u274c \u5df2\u62d2\u7edd\u4efb\u52a1",
    "accepted": "\u2705 \u5df2\u5b8c\u6210\u4efb\u52a1",
    "failed": "\U0001f4a5 \u5931\u8d25/\u8d85\u65f6",
    "archived": "\U0001f4c1 \u5f52\u6863\u4efb\u52a1",
}

TASK_STATUS_EMPTY_LABELS = {
    "pending": "\u5f85\u5904\u7406",
    "processing": "\u6267\u884c\u4e2d",
    "pending_acceptance": "\u5f85\u9a8c\u6536",
    "rejected": "\u5df2\u62d2\u7edd",
    "accepted": "\u5df2\u5b8c\u6210",
    "failed": "\u5931\u8d25/\u8d85\u65f6",
    "archived": "\u5f52\u6863",
}


def task_mgmt_menu_keyboard() -> Dict:
    """Sub-menu: task management with status-based filtering."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f550 \u5f85\u5904\u7406\u4efb\u52a1", "callback_data": "menu:tasks_pending"},
                {"text": "\u2699\ufe0f \u6267\u884c\u4e2d\u4efb\u52a1", "callback_data": "menu:tasks_processing"},
            ],
            [
                {"text": "\U0001f4cb \u5f85\u9a8c\u6536\u4efb\u52a1", "callback_data": "menu:tasks_pending_acceptance"},
                {"text": "\u274c \u5df2\u62d2\u7edd\u4efb\u52a1", "callback_data": "menu:tasks_rejected"},
            ],
            [
                {"text": "\u2705 \u5df2\u5b8c\u6210\u4efb\u52a1", "callback_data": "menu:tasks_accepted"},
                {"text": "\U0001f4a5 \u5931\u8d25/\u8d85\u65f6", "callback_data": "menu:tasks_failed"},
            ],
            [
                {"text": "\U0001f4c1 \u5f52\u6863\u4efb\u52a1", "callback_data": "menu:tasks_archived"},
                {"text": "\U0001f4ca \u5168\u90e8\u6982\u89c8", "callback_data": "menu:tasks_overview"},
            ],
            [
                {"text": "\U0001f4c2 \u5f52\u6863\u7ba1\u7406", "callback_data": "menu:sub_archive"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
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
    for t in page_tasks:
        code = str(t.get("task_code") or "-")
        text = str(t.get("text") or t.get("summary") or "").strip()
        if len(text) > 20:
            text = text[:20] + "..."
        label = "[{}] {}".format(code, text)
        # For archived tasks, use archive_detail callback with fallback
        if status_key == "archived":
            archive_id = str(t.get("archive_id") or "").strip()
            if not archive_id:
                archive_id = str(t.get("task_id") or t.get("task_code") or code).strip()
            cb = safe_callback_data("archive_detail", archive_id)
        else:
            cb = "task_detail:{}".format(code)
        rows.append([{"text": label, "callback_data": cb}])

    # Pagination buttons
    nav_row: List[Dict] = []
    if page > 0:
        nav_row.append({"text": "\u00ab \u4e0a\u4e00\u9875", "callback_data": "tasks_page:{}:{}".format(status_key, page - 1)})
    if end < total:
        nav_row.append({"text": "\u4e0b\u4e00\u9875 \u00bb", "callback_data": "tasks_page:{}:{}".format(status_key, page + 1)})
    if nav_row:
        rows.append(nav_row)

    rows.append([{"text": "\u00ab \u8fd4\u56de\u4efb\u52a1\u7ba1\u7406", "callback_data": "menu:sub_task_mgmt"}])
    return {"inline_keyboard": rows}


def task_detail_keyboard(task_code: str, status: str, is_pipeline: bool = False) -> Dict:
    """Build action keyboard for a task detail page based on its status.

    Different statuses get different action buttons.
    When is_pipeline=True, adds a "查看阶段详情" button.
    """
    rows: List[List[Dict]] = []
    s = str(status or "").strip().lower()
    code = str(task_code or "")

    if s == "pending":
        rows.append([
            {"text": "\U0001f4c4 \u67e5\u770b\u8be6\u60c5", "callback_data": "task_doc:{}".format(code)},
            {"text": "\U0001f5d1 \u53d6\u6d88\u4efb\u52a1", "callback_data": "task_cancel:{}".format(code)},
        ])
        back_cb = "menu:tasks_pending"
    elif s == "processing":
        rows.append([
            {"text": "\U0001f4ca \u67e5\u770b\u8fdb\u5ea6", "callback_data": "status:{}".format(code)},
            {"text": "\U0001f4dc \u67e5\u770b\u4e8b\u4ef6", "callback_data": "events:{}".format(code)},
        ])
        back_cb = "menu:tasks_processing"
    elif s == "pending_acceptance":
        rows.append([
            {"text": "\u2705 \u9a8c\u6536\u901a\u8fc7", "callback_data": "accept:{}".format(code)},
            {"text": "\u274c \u9a8c\u6536\u62d2\u7edd", "callback_data": "reject:{}".format(code)},
        ])
        rows.append([
            {"text": "\U0001f4c4 \u67e5\u770b\u6587\u6863", "callback_data": "task_doc:{}".format(code)},
            {"text": "\U0001f4d1 \u67e5\u770b\u6982\u8981", "callback_data": "task_summary:{}".format(code)},
        ])
        rows.append([
            {"text": "\U0001f4dc \u67e5\u770b\u5b8c\u6574\u65e5\u5fd7", "callback_data": "task_log:{}".format(code)},
        ])
        back_cb = "menu:tasks_pending_acceptance"
    elif s == "rejected":
        rows.append([
            {"text": "\U0001f504 \u91cd\u65b0\u5f00\u53d1", "callback_data": "retry:{}".format(code)},
            {"text": "\u2705 \u6539\u4e3a\u901a\u8fc7", "callback_data": "accept:{}".format(code)},
        ])
        rows.append([
            {"text": "\U0001f4c4 \u67e5\u770b\u6587\u6863", "callback_data": "task_doc:{}".format(code)},
            {"text": "\U0001f5d1 \u5220\u9664\u4efb\u52a1", "callback_data": "task_delete:{}".format(code)},
        ])
        back_cb = "menu:tasks_rejected"
    elif s in ("accepted", "completed"):
        rows.append([
            {"text": "\U0001f4c4 \u67e5\u770b\u6587\u6863", "callback_data": "task_doc:{}".format(code)},
            {"text": "\U0001f4d1 \u67e5\u770b\u6982\u8981", "callback_data": "task_summary:{}".format(code)},
        ])
        rows.append([
            {"text": "\U0001f4dc \u67e5\u770b\u5b8c\u6574\u65e5\u5fd7", "callback_data": "task_log:{}".format(code)},
        ])
        back_cb = "menu:tasks_accepted"
    elif s in ("failed", "timeout"):
        rows.append([
            {"text": "\U0001f504 \u91cd\u8bd5", "callback_data": "retry:{}".format(code)},
            {"text": "\U0001f4c4 \u67e5\u770b\u9519\u8bef", "callback_data": "task_doc:{}".format(code)},
        ])
        rows.append([
            {"text": "\U0001f4dc \u67e5\u770b\u5b8c\u6574\u65e5\u5fd7", "callback_data": "task_log:{}".format(code)},
            {"text": "\U0001f5d1 \u5220\u9664\u4efb\u52a1", "callback_data": "task_delete:{}".format(code)},
        ])
        back_cb = "menu:tasks_failed"
    else:
        rows.append([
            {"text": "\U0001f4c4 \u67e5\u770b\u8be6\u60c5", "callback_data": "task_doc:{}".format(code)},
        ])
        back_cb = "menu:sub_task_mgmt"

    if is_pipeline:
        rows.append([
            {"text": "\U0001f50d \u67e5\u770b\u9636\u6bb5\u8be6\u60c5", "callback_data": "stage_detail:{}".format(code)},
        ])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u5217\u8868", "callback_data": back_cb}])
    return {"inline_keyboard": rows}


def tasks_overview_keyboard(counts: Dict[str, int]) -> Dict:
    """Build overview statistics keyboard. Each line is a clickable button."""
    status_map = [
        ("pending", "\U0001f550 \u5f85\u5904\u7406", "menu:tasks_pending"),
        ("processing", "\u2699\ufe0f \u6267\u884c\u4e2d", "menu:tasks_processing"),
        ("pending_acceptance", "\U0001f4cb \u5f85\u9a8c\u6536", "menu:tasks_pending_acceptance"),
        ("rejected", "\u274c \u5df2\u62d2\u7edd", "menu:tasks_rejected"),
        ("accepted", "\u2705 \u5df2\u5b8c\u6210", "menu:tasks_accepted"),
        ("failed", "\U0001f4a5 \u5931\u8d25/\u8d85\u65f6", "menu:tasks_failed"),
        ("archived", "\U0001f4c1 \u5df2\u5f52\u6863", "menu:tasks_archived"),
    ]
    rows: List[List[Dict]] = []
    for key, label, cb in status_map:
        count = int(counts.get(key, 0))
        rows.append([{"text": "{}: {}".format(label, count), "callback_data": cb}])
    rows.append([{"text": "\u00ab \u8fd4\u56de\u4efb\u52a1\u7ba1\u7406", "callback_data": "menu:sub_task_mgmt"}])
    return {"inline_keyboard": rows}


def archive_detail_keyboard(archive_id: str) -> Dict:
    """Action keyboard for an archived task detail."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4c4 \u67e5\u770b\u5f52\u6863\u8be6\u60c5", "callback_data": safe_callback_data("task_doc", archive_id)},
                {"text": "\U0001f5d1 \u5220\u9664\u5f52\u6863\u8bb0\u5f55", "callback_data": safe_callback_data("archive_delete", archive_id)},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u5217\u8868", "callback_data": "menu:tasks_archived"},
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
                {"text": "\u2705 \u786e\u8ba4\u6267\u884c", "callback_data": cb_data},
                {"text": "\u2716 \u53d6\u6d88", "callback_data": "menu:cancel"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# Welcome / help text
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "Aming Claw \u63a7\u5236\u9762\u677f\n"
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
    "\u540e\u7aef: {backend}  |  2FA: {auth}\n"
    "\u6a21\u578b: {model}\n"
    "\u5de5\u4f5c\u533a: {workspace}\n"
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
    "\u70b9\u51fb\u6309\u94ae\u64cd\u4f5c\uff0c\u6216\u76f4\u63a5\u53d1\u6587\u5b57\u4e0eAI\u5bf9\u8bdd\n"
    "\u53d1\u9001 /help \u67e5\u770b\u6240\u6709\u652f\u6301\u7684\u547d\u4ee4"
)


HELP_TEXT = (
    "\u652f\u6301\u7684\u547d\u4ee4\u5217\u8868\n"
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
    "\u83dc\u5355 & \u4fe1\u606f:\n"
    "  /menu - \u6253\u5f00\u4e3b\u83dc\u5355\n"
    "  /help - \u663e\u793a\u6b64\u547d\u4ee4\u5217\u8868\n"
    "  /info - \u67e5\u770b\u7cfb\u7edf\u4fe1\u606f\n"
    "  /ops_whoami - \u67e5\u770b\u8eab\u4efd\u4fe1\u606f\n\n"
    "\u4efb\u52a1\u7ba1\u7406:\n"
    "  /task [\u5185\u5bb9] - \u521b\u5efa\u65b0\u4efb\u52a1\uff08\u65e0\u53c2\u6570\u65f6\u4ea4\u4e92\u5f0f\u9009\u62e9\u5de5\u4f5c\u533a\uff09\n"
    "  /status [\u4ee3\u53f7] - \u67e5\u770b\u4efb\u52a1\u5217\u8868\u6216\u5355\u4e2a\u4efb\u52a1\n"
    "  /events <\u4ee3\u53f7> - \u67e5\u770b\u4efb\u52a1\u4e8b\u4ef6\n"
    "  /accept [\u4ee3\u53f7] [OTP] - \u9a8c\u6536\u4efb\u52a1\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u5f85\u9a8c\u6536\u5217\u8868\uff09\n"
    "  /reject [\u4ee3\u53f7] [OTP] <\u539f\u56e0> - \u62d2\u7edd\u4efb\u52a1\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u53ef\u62d2\u7edd\u5217\u8868\uff09\n"
    "  /retry [\u4ee3\u53f7] [\u8865\u5145\u8bf4\u660e] - \u91cd\u8bd5\u4efb\u52a1\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u53ef\u91cd\u8bd5\u5217\u8868\uff09\n"
    "  /cancel [\u4ee3\u53f7] - \u53d6\u6d88\u4efb\u52a1\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u53ef\u53d6\u6d88\u5217\u8868\uff09\n"
    "  /clear_tasks - \u6e05\u7a7a\u4efb\u52a1\u5217\u8868\n\n"
    "\u540e\u7aef & \u6a21\u578b:\n"
    "  /switch_backend [\u540e\u7aef] - \u5207\u6362\u540e\u7aef\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u9009\u62e9\u83dc\u5355\uff09\n"
    "  /switch_model [\u6a21\u578bID] - \u5207\u6362AI\u6a21\u578b\uff08\u65e0\u53c2\u6570\u65f6\u5f39\u51fa\u9009\u62e9\u83dc\u5355\uff09\n"
    "  /set_role_model <\u89d2\u8272> <\u6a21\u578b|default> [provider] - \u8bbe\u7f6e\u89d2\u8272\u6d41\u6c34\u7ebf\u6a21\u578b\n"
    "  /set_pipeline <\u914d\u7f6e> - \u8bbe\u7f6e\u6d41\u6c34\u7ebf\n"
    "  /show_pipeline - \u67e5\u770b\u6d41\u6c34\u7ebf\u914d\u7f6e\n\n"
    "\u5f52\u6863:\n"
    "  /archive [\u5173\u952e\u8bcd] - \u5f52\u6863\u6982\u89c8/\u641c\u7d22\n"
    "  /archive_show <ID> - \u5f52\u6863\u8be6\u60c5\n"
    "  /archive_log <\u5173\u952e\u8bcd> - \u5f52\u6863\u65e5\u5fd7\n\n"
    "\u8fd0\u7ef4 (\u9700OTP):\n"
    "  /mgr_restart <OTP> - \u91cd\u542f\u670d\u52a1\n"
    "  /mgr_reinit <OTP> - \u81ea\u6211\u66f4\u65b0\n"
    "  /mgr_status - \u67e5\u770b\u7ba1\u7406\u670d\u52a1\u72b6\u6001\n"
    "  /ops_restart <OTP> - \u91cd\u542fOps\u670d\u52a1\n"
    "  /ops_set_workspace <\u8def\u5f84> <OTP> - \u5207\u6362\u5de5\u4f5c\u533a\n\n"
    "\u5b89\u5168:\n"
    "  /auth_init - \u521d\u59cb\u53162FA\n"
    "  /auth_status - \u67e5\u770b2FA\u72b6\u6001\n"
    "  /auth_debug <OTP> - OTP\u8c03\u8bd5\n\n"
    "\u5de5\u4f5c\u76ee\u5f55:\n"
    "  /workspace_add <\u8def\u5f84> [\u6807\u7b7e] - \u6dfb\u52a0\u5de5\u4f5c\u76ee\u5f55\n"
    "  /workspace_remove <ID> - \u79fb\u9664\u5de5\u4f5c\u76ee\u5f55\n"
    "  /workspace_list - \u67e5\u770b\u5de5\u4f5c\u76ee\u5f55\u5217\u8868\n"
    "  /workspace_default <ID> - \u8bbe\u7f6e\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55\n"
    "  /workspace_search_roots [add|remove|clear] - \u641c\u7d22\u6839\u76ee\u5f55\n"
    "  /dispatch_status - \u67e5\u770b\u5e76\u884c\u8c03\u5ea6\u5668\u72b6\u6001\n"
    "  @workspace:<\u6807\u7b7e> <\u4efb\u52a1> - \u6307\u5b9a\u5de5\u4f5c\u76ee\u5f55\u6267\u884c\n\n"
    "\u5176\u4ed6:\n"
    "  /screenshot [\u8bf4\u660e] - \u622a\u56fe\n"
    "  \u76f4\u63a5\u53d1\u9001\u6587\u5b57 - \u4e0eAI\u5bf9\u8bdd"
)


SUBMENU_TEXTS = {
    "system": (
        "\u2699\ufe0f \u7cfb\u7edf\u8bbe\u7f6e\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5f53\u524d\u540e\u7aef: {backend}\n"
        "AI\u6a21\u578b: {model}\n"
        "\u63d0\u4f9b\u5546: {provider}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u6267\u884c\u7684\u64cd\u4f5c:"
    ),
    "archive": (
        "\U0001f4c2 \u5f52\u6863\u7ba1\u7406\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u67e5\u770b\u3001\u68c0\u7d22\u548c\u7ba1\u7406\u5df2\u5f52\u6863\u7684\u4efb\u52a1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u6267\u884c\u7684\u64cd\u4f5c:"
    ),
    "ops": (
        "\U0001f527 \u8fd0\u7ef4\u64cd\u4f5c\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u670d\u52a1\u91cd\u542f\u3001\u5de5\u4f5c\u533a\u7ba1\u7406\u7b49\u8fd0\u7ef4\u64cd\u4f5c\n"
        "\u9700\u8981OTP\u9a8c\u8bc1\u7684\u64cd\u4f5c\u4f1a\u63d0\u793a\u8f93\u5165\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u6267\u884c\u7684\u64cd\u4f5c:"
    ),
    "security": (
        "\U0001f512 \u5b89\u5168\u8ba4\u8bc1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "2FA\u8bbe\u7f6e\u3001\u8eab\u4efd\u9a8c\u8bc1\u548c\u5b89\u5168\u7ba1\u7406\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u6267\u884c\u7684\u64cd\u4f5c:"
    ),
    "workspace": (
        "\U0001f4c1 \u5de5\u4f5c\u533a\u7ba1\u7406\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u7ba1\u7406\u5de5\u4f5c\u76ee\u5f55\uff1a\u6dfb\u52a0\u3001\u5220\u9664\u3001\u8bbe\u7f6e\u9ed8\u8ba4\n"
        "\u67e5\u770b\u4efb\u52a1\u961f\u5217\u548c\u8c03\u5ea6\u5668\u72b6\u6001\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u6267\u884c\u7684\u64cd\u4f5c:"
    ),
    "skills": (
        "\U0001f9e9 技能管理\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "可用技能列表：\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "选择要执行的操作:"
    ),
    "task_mgmt": (
        "\U0001f4cb \u4efb\u52a1\u7ba1\u7406\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5f53\u524d\u5171 {active_count} \u4e2a\u6d3b\u8dc3\u4efb\u52a1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u9009\u62e9\u8981\u67e5\u770b\u7684\u4efb\u52a1\u72b6\u6001:"
    ),
}


# ---------------------------------------------------------------------------
# Prompt text for each pending action
# ---------------------------------------------------------------------------

PENDING_PROMPTS = {
    "new_task": (
        "\U0001f4dd \u65b0\u5efa\u4efb\u52a1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u4efb\u52a1\u8be6\u7ec6\u63cf\u8ff0\uff0c\u53d1\u9001\u6587\u5b57\u5373\u53ef\u521b\u5efa:\n\n"
        "\u793a\u4f8b: \u5728 src/utils.py \u4e2d\u6dfb\u52a0\u65e5\u5fd7\u529f\u80fd"
    ),
    "screenshot": (
        "\U0001f4f7 \u622a\u56fe\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u622a\u56fe\u8bf4\u660e\uff08\u53ef\u9009\uff09:\n\n"
        "\u76f4\u63a5\u53d1\u9001\u4efb\u610f\u6587\u5b57\u5373\u53ef\u622a\u56fe"
    ),
    "archive_search": (
        "\U0001f50d \u5f52\u6863\u68c0\u7d22\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u68c0\u7d22\u5173\u952e\u8bcd:\n\n"
        "\u652f\u6301\u4efb\u52a1\u63cf\u8ff0\u3001\u4ee3\u53f7\u6216ID\u641c\u7d22"
    ),
    "archive_show": (
        "\U0001f4c4 \u5f52\u6863\u8be6\u60c5\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u5f52\u6863ID\u3001\u4efb\u52a1ID\u6216\u4efb\u52a1\u4ee3\u53f7:"
    ),
    "archive_log": (
        "\U0001f4dc \u5f52\u6863\u65e5\u5fd7\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u5f52\u6863\u5173\u952e\u8bcd\u3001ID\u6216\u4efb\u52a1\u4ee3\u53f7:"
    ),
    "mgr_restart": (
        "\U0001f527 Mgr\u91cd\u542f\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5c06\u91cd\u542f coordinator + executor \u670d\u52a1\n\n"
        "\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801:"
    ),
    "mgr_reinit": (
        "\u2b06\ufe0f \u81ea\u6211\u66f4\u65b0\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5c06\u6267\u884c git pull + \u91cd\u542f\u6240\u6709\u670d\u52a1\n"
        "\u670d\u52a1\u91cd\u542f\u671f\u95f4\u6d88\u606f\u53ef\u80fd\u77ed\u6682\u65e0\u54cd\u5e94\n\n"
        "\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801:"
    ),
    "ops_restart": (
        "\U0001f504 Ops\u5168\u90e8\u91cd\u542f\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5c06\u91cd\u542f\u6240\u6709\u8fd0\u7ef4\u670d\u52a1\n\n"
        "\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801:"
    ),
    "set_workspace": (
        "\U0001f4c2 \u5207\u6362\u5de5\u4f5c\u533a\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u65b0\u7684\u5de5\u4f5c\u533a\u8def\u5f84\uff08\u6216\u5173\u952e\u8bcd\u641c\u7d22\uff09\n"
        "\u683c\u5f0f: <\u8def\u5f84\u6216\u5173\u952e\u8bcd> <6\u4f4dOTP>\n\n"
        "\u793a\u4f8b: my-project 123456"
    ),
    "reset_workspace": (
        "\u21a9\ufe0f \u91cd\u7f6e\u5de5\u4f5c\u533a\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5c06\u91cd\u7f6e\u4e3a\u9ed8\u8ba4\u5de5\u4f5c\u533a\u8def\u5f84\n\n"
        "\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801:"
    ),
    "auth_debug": (
        "\U0001f50d OTP\u8c03\u8bd5\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801\u8fdb\u884c\u8c03\u8bd5:"
    ),
    "pipeline_config_custom": (
        "\u2699\ufe0f \u81ea\u5b9a\u4e49\u6d41\u6c34\u7ebf\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u6d41\u6c34\u7ebf\u914d\u7f6e (stage:backend \u683c\u5f0f)\n\n"
        "\u793a\u4f8b: plan:openai code:claude verify:codex"
    ),
    "workspace_add": (
        "\u2795 \u6dfb\u52a0\u5de5\u4f5c\u76ee\u5f55\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u5de5\u4f5c\u76ee\u5f55\u8def\u5f84\u6216\u5173\u952e\u8bcd\n"
        "\u683c\u5f0f: <\u8def\u5f84|\u5173\u952e\u8bcd> [\u6807\u7b7e]\n\n"
        "\u793a\u4f8b1: C:\\Users\\me\\projects\\my-app my-app\n"
        "\u793a\u4f8b2: toolbox  (\u6a21\u7cca\u641c\u7d22\u5e26.git\u7684\u76ee\u5f55)"
    ),
    "workspace_remove": (
        "\u2796 \u5220\u9664\u5de5\u4f5c\u76ee\u5f55\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u8981\u5220\u9664\u7684\u5de5\u4f5c\u76ee\u5f55ID:\n\n"
        "\u793a\u4f8b: ws-abc12345"
    ),
    "workspace_set_default": (
        "\u2b50 \u8bbe\u7f6e\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u8981\u8bbe\u7f6e\u4e3a\u9ed8\u8ba4\u7684\u5de5\u4f5c\u76ee\u5f55ID:\n\n"
        "\u793a\u4f8b: ws-abc12345"
    ),
    "search_root_add": (
        "\U0001f50d \u6dfb\u52a0\u641c\u7d22\u6839\u76ee\u5f55\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u8981\u6dfb\u52a0\u7684\u641c\u7d22\u6839\u76ee\u5f55\u8def\u5f84\n"
        "\u6a21\u7cca\u641c\u7d22\u5c06\u5728\u8fd9\u4e9b\u76ee\u5f55\u4e0b\u9012\u5f52\u67e5\u627e .git \u9879\u76ee\n\n"
        "\u793a\u4f8b: C:\\Users\\me\\Documents\n"
        "\u591a\u4e2a\u8def\u5f84\u7528\u5206\u53f7\u5206\u9694: D:\\projects;E:\\repos"
    ),
    "new_task_with_workspace": (
        "\U0001f4dd \u65b0\u5efa\u4efb\u52a1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5df2\u9009\u62e9\u5de5\u4f5c\u533a: {ws_label}\n"
        "\u8bf7\u8f93\u5165\u4efb\u52a1\u8be6\u7ec6\u63cf\u8ff0\uff0c\u53d1\u9001\u6587\u5b57\u5373\u53ef\u521b\u5efa:\n\n"
        "\u793a\u4f8b: \u5728 src/utils.py \u4e2d\u6dfb\u52a0\u65e5\u5fd7\u529f\u80fd"
    ),
}
