"""Interactive button-based menu system for Telegram bot.

Provides a hierarchical click-to-interact UI organized as:
  Main Dashboard -> Quick Actions (new task, task list, screenshot)
                 -> Sub-menus (system, archive, ops, security)

Multi-step flows use a pending-action state machine:
  click button -> bot prompts for input -> user sends text -> action completes.
"""

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import load_json, save_json, send_text, tasks_root, utc_iso


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
                {"text": "\U0001f4cb \u4efb\u52a1\u5217\u8868", "callback_data": "menu:task_list"},
                {"text": "\U0001f4f7 \u622a\u56fe", "callback_data": "menu:screenshot"},
            ],
            # ---- Sub-menu entries ----
            [
                {"text": "\u2699\ufe0f \u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"},
                {"text": "\U0001f4c2 \u5f52\u6863\u7ba1\u7406", "callback_data": "menu:sub_archive"},
            ],
            [
                {"text": "\U0001f527 \u8fd0\u7ef4\u64cd\u4f5c", "callback_data": "menu:sub_ops"},
                {"text": "\U0001f512 \u5b89\u5168\u8ba4\u8bc1", "callback_data": "menu:sub_security"},
            ],
            [
                {"text": "\U0001f4c1 \u5de5\u4f5c\u533a\u7ba1\u7406", "callback_data": "menu:sub_workspace"},
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
            ],
            [
                {"text": "\u2699\ufe0f \u6d41\u6c34\u7ebf\u914d\u7f6e", "callback_data": "menu:pipeline_config"},
                {"text": "\U0001f4ca \u6d41\u6c34\u7ebf\u72b6\u6001", "callback_data": "menu:pipeline_status"},
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
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
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
                {"text": "\U0001f4ca \u961f\u5217\u72b6\u6001", "callback_data": "menu:workspace_queue_status"},
                {"text": "\U0001f4ca \u8c03\u5ea6\u5668\u72b6\u6001", "callback_data": "menu:dispatch_status"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u4e3b\u83dc\u5355", "callback_data": "menu:main"},
            ],
        ]
    }


def workspace_select_keyboard(workspaces: List[Dict], callback_prefix: str = "ws_select") -> Dict:
    """Build a dynamic keyboard for workspace selection.

    Each workspace gets a button with callback_data: <prefix>:<ws_id>
    """
    rows: List[List[Dict]] = []
    for ws in workspaces:
        label = ws.get("label", ws.get("id", "?"))
        ws_id = ws.get("id", "")
        flags = []
        if ws.get("is_default"):
            flags.append("\u2b50")
        if not ws.get("active", True):
            flags.append("\u26d4")
        display = "{}{}".format(" ".join(flags) + " " if flags else "", label)
        rows.append([{"text": display, "callback_data": "{}:{}".format(callback_prefix, ws_id)}])
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
                {"text": "\u270f\ufe0f \u81ea\u5b9a\u4e49\u914d\u7f6e", "callback_data": "menu:pipeline_config_custom"},
            ],
            [
                {"text": "\u00ab \u8fd4\u56de\u7cfb\u7edf\u8bbe\u7f6e", "callback_data": "menu:sub_system"},
            ],
        ]
    }


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


def confirm_cancel_keyboard(action: str, context_str: str = "") -> Dict:
    """Confirm + cancel keyboard for dangerous operations."""
    cb_data = "confirm:{}".format(action)
    if context_str:
        cb_data = "confirm:{}:{}".format(action, context_str)
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
    "  /task <\u5185\u5bb9> - \u521b\u5efa\u65b0\u4efb\u52a1\n"
    "  /status [\u4ee3\u53f7] - \u67e5\u770b\u4efb\u52a1\u5217\u8868\u6216\u5355\u4e2a\u4efb\u52a1\n"
    "  /events <\u4ee3\u53f7> - \u67e5\u770b\u4efb\u52a1\u4e8b\u4ef6\n"
    "  /accept <\u4ee3\u53f7> [OTP] - \u9a8c\u6536\u4efb\u52a1\n"
    "  /reject <\u4ee3\u53f7> [OTP] [\u539f\u56e0] - \u62d2\u7edd\u4efb\u52a1\n"
    "  /clear_tasks - \u6e05\u7a7a\u4efb\u52a1\u5217\u8868\n\n"
    "\u540e\u7aef & \u6a21\u578b:\n"
    "  /switch_backend <codex|claude|pipeline> - \u5207\u6362\u540e\u7aef\n"
    "  /switch_model [\u6a21\u578bID] - \u5207\u6362AI\u6a21\u578b\n"
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
        "\u793a\u4f8b: plan:claude code:claude verify:codex"
    ),
    "workspace_add": (
        "\u2795 \u6dfb\u52a0\u5de5\u4f5c\u76ee\u5f55\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u8bf7\u8f93\u5165\u5de5\u4f5c\u76ee\u5f55\u8def\u5f84\u548c\u53ef\u9009\u6807\u7b7e\n"
        "\u683c\u5f0f: <\u8def\u5f84> [\u6807\u7b7e]\n\n"
        "\u793a\u4f8b: C:\\Users\\me\\projects\\my-app my-app"
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
    "new_task_with_workspace": (
        "\U0001f4dd \u65b0\u5efa\u4efb\u52a1\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u5df2\u9009\u62e9\u5de5\u4f5c\u533a: {ws_label}\n"
        "\u8bf7\u8f93\u5165\u4efb\u52a1\u8be6\u7ec6\u63cf\u8ff0\uff0c\u53d1\u9001\u6587\u5b57\u5373\u53ef\u521b\u5efa:\n\n"
        "\u793a\u4f8b: \u5728 src/utils.py \u4e2d\u6dfb\u52a0\u65e5\u5fd7\u529f\u80fd"
    ),
}
