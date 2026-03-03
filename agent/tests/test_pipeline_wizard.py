"""Tests for the pipeline configuration wizard flow (overview-style).

Covers:
- Preset selection → enters overview page (not immediate apply)
- Stage model modification → overview page updates
- Confirm apply → config takes effect
- Back/return button navigation
- Edge cases (pending action lost)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bot_commands  # noqa: E402
from interactive_menu import (  # noqa: E402
    set_pending_action,
    get_pending_action,
    peek_pending_action,
    clear_pending_action,
)
from config import PIPELINE_PRESETS  # noqa: E402


def _make_cb(data, chat_id=100, user_id=200, cb_id="cb-1"):
    """Build a minimal Telegram callback_query dict."""
    return {
        "id": cb_id,
        "data": data,
        "message": {"chat": {"id": chat_id}},
        "from": {"id": user_id},
    }


class TestPipelinePresetEntersOverview(unittest.TestCase):
    """Sub-task 3.3: Preset selection should enter overview page, NOT apply immediately."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_preset_does_not_call_set_pipeline_stages(self, _ops, mock_send, _acb):
        """Selecting a preset should NOT call set_pipeline_stages (config not applied yet)."""
        with patch.object(bot_commands, "set_pipeline_stages") as mock_set:
            bot_commands.handle_callback_query(_make_cb("pipeline_preset:plan_code_verify"))
            mock_set.assert_not_called()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_preset_stores_pending_action(self, _ops, mock_send, _acb):
        """Selecting a preset should store a pipeline_configure pending action."""
        bot_commands.handle_callback_query(_make_cb("pipeline_preset:plan_code_verify"))
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "pipeline_configure")
        ctx = pending["context"]
        self.assertEqual(ctx["preset_name"], "plan_code_verify")
        self.assertEqual(len(ctx["stages"]), 3)

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_preset_shows_overview_keyboard(self, _ops, mock_send, _acb):
        """Selecting a preset should display the stage overview keyboard."""
        bot_commands.handle_callback_query(_make_cb("pipeline_preset:plan_code"))
        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs[1].get("reply_markup") if call_kwargs[1] else None
        if reply_markup is None:
            reply_markup = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        self.assertIsNotNone(reply_markup)
        all_data = [btn["callback_data"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        self.assertTrue(any("pipeline_stage_cfg:" in d for d in all_data))
        self.assertIn("pipeline_apply", all_data)

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_role_pipeline_merges_role_config(self, _ops, mock_send, _acb):
        """role_pipeline preset should merge per-role model config into pending stages."""
        from config import set_role_stage_model
        set_role_stage_model("pm", "claude-opus-4-6", provider="anthropic")
        bot_commands.handle_callback_query(_make_cb("pipeline_preset:role_pipeline"))
        pending = peek_pending_action(100, 200)
        ctx = pending["context"]
        pm_stage = next(s for s in ctx["stages"] if s["name"] == "pm")
        self.assertEqual(pm_stage["model"], "claude-opus-4-6")
        self.assertEqual(pm_stage["provider"], "anthropic")


class TestStageModelModification(unittest.TestCase):
    """Sub-task 3.4: stage_model callback updates pending stages and returns to overview."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_stage_model_updates_pending(self, _ops, mock_send, _acb):
        """Setting a stage model should update the pending action stages."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": [
                {"name": "plan", "backend": "claude"},
                {"name": "code", "backend": "claude"},
            ],
        })
        bot_commands.handle_callback_query(
            _make_cb("stage_model:0:anthropic:claude-opus-4-6"))
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        stages = pending["context"]["stages"]
        self.assertEqual(stages[0]["model"], "claude-opus-4-6")
        self.assertEqual(stages[0]["provider"], "anthropic")
        # Other stage unchanged
        self.assertFalse(stages[1].get("model"))

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_stage_model_returns_to_overview(self, _ops, mock_send, _acb):
        """After modifying a stage model, overview page should be shown."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code_verify",
            "stages": [
                {"name": "plan", "backend": "claude"},
                {"name": "code", "backend": "claude"},
                {"name": "verify", "backend": "codex"},
            ],
        })
        bot_commands.handle_callback_query(
            _make_cb("stage_model:2:openai:gpt-4.1"))
        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs[1].get("reply_markup") if call_kwargs[1] else None
        if reply_markup is None:
            reply_markup = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        all_data = [btn["callback_data"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        self.assertIn("pipeline_apply", all_data)
        # Verify stage overview shows updated model
        all_text = [btn["text"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        self.assertTrue(any("gpt-4.1" in t for t in all_text))


class TestPipelineApply(unittest.TestCase):
    """Sub-task 3.5: pipeline_apply callback applies config and clears pending action."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_apply_calls_set_pipeline_stages(self, _ops, mock_send, _acb):
        """Clicking confirm should call set_pipeline_stages with correct stages."""
        stages = [
            {"name": "plan", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "code", "backend": "claude"},
        ]
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": stages,
        })
        with patch.object(bot_commands, "set_pipeline_stages") as mock_set:
            bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
            mock_set.assert_called_once()
            applied_stages = mock_set.call_args[0][0]
            self.assertEqual(len(applied_stages), 2)
            self.assertEqual(applied_stages[0]["model"], "claude-opus-4-6")

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_apply_clears_pending_action(self, _ops, mock_send, _acb):
        """After applying, pending action should be cleared."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": [{"name": "plan", "backend": "claude"}],
        })
        bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
        # get_pending_action returns None since it was consumed
        self.assertIsNone(peek_pending_action(100, 200))

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_apply_role_pipeline_syncs_role_stages(self, _ops, mock_send, _acb):
        """For role_pipeline, apply should also call set_role_pipeline_stages."""
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "dev", "backend": "claude"},
            {"name": "test", "backend": "claude"},
            {"name": "qa", "backend": "claude"},
        ]
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "role_pipeline",
            "stages": stages,
        })
        with patch.object(bot_commands, "set_pipeline_stages"), \
             patch.object(bot_commands, "set_role_pipeline_stages") as mock_role:
            bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
            mock_role.assert_called_once()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_apply_sends_confirmation_message(self, _ops, mock_send, _acb):
        """After applying, a confirmation message with summary should be sent."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": [
                {"name": "plan", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
                {"name": "code", "backend": "claude"},
            ],
        })
        bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
        self.assertTrue(mock_send.called)
        msg_text = mock_send.call_args[0][1]
        self.assertIn("\u2705", msg_text)  # ✅
        self.assertIn("pipeline", msg_text)


class TestPipelineStageOverviewCallback(unittest.TestCase):
    """Sub-task 3.6: menu:pipeline_stage_overview and pipeline_stage_cfg callbacks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_overview_shows_stages(self, _ops, mock_send, _acb):
        """menu:pipeline_stage_overview should read pending action and show overview."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": [
                {"name": "plan", "backend": "claude"},
                {"name": "code", "backend": "claude"},
            ],
        })
        bot_commands.handle_callback_query(_make_cb("menu:pipeline_stage_overview"))
        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs[1].get("reply_markup") if call_kwargs[1] else None
        if reply_markup is None:
            reply_markup = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        all_data = [btn["callback_data"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        self.assertIn("pipeline_stage_cfg:0", all_data)
        self.assertIn("pipeline_stage_cfg:1", all_data)

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_overview_fallback_when_no_pending(self, _ops, mock_send, _acb):
        """If pending action is lost, should fall back to preset selection."""
        bot_commands.handle_callback_query(_make_cb("menu:pipeline_stage_overview"))
        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs[1].get("reply_markup") if call_kwargs[1] else None
        if reply_markup is None:
            reply_markup = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        all_data = [btn["callback_data"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        # Should show preset keyboard (has pipeline_preset: buttons)
        self.assertTrue(any("pipeline_preset:" in d for d in all_data))

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    @patch("bot_commands.get_available_models", return_value=[
        {"id": "claude-opus-4-6", "provider": "anthropic", "status": "available"},
    ])
    def test_stage_cfg_shows_model_selection(self, _models, _ops, mock_send, _acb):
        """pipeline_stage_cfg:{index} should show model selection keyboard."""
        set_pending_action(100, 200, "pipeline_configure", {
            "preset_name": "plan_code",
            "stages": [
                {"name": "plan", "backend": "claude"},
                {"name": "code", "backend": "claude"},
            ],
        })
        bot_commands.handle_callback_query(_make_cb("pipeline_stage_cfg:0"))
        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args
        reply_markup = call_kwargs[1].get("reply_markup") if call_kwargs[1] else None
        if reply_markup is None:
            reply_markup = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        all_data = [btn["callback_data"]
                    for row in reply_markup["inline_keyboard"] for btn in row]
        self.assertTrue(any("stage_model:0:" in d for d in all_data))
        self.assertIn("menu:pipeline_stage_overview", all_data)

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_stage_cfg_fallback_when_no_pending(self, _ops, mock_send, _acb):
        """If pending action is lost during stage cfg, fall back to preset selection."""
        bot_commands.handle_callback_query(_make_cb("pipeline_stage_cfg:0"))
        self.assertTrue(mock_send.called)
        msg_text = mock_send.call_args[0][1]
        self.assertIn("\u8fc7\u671f", msg_text)  # 过期

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    def test_apply_fallback_when_no_pending(self, _ops, mock_send, _acb):
        """If pending action is lost during apply, fall back to preset selection."""
        bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
        self.assertTrue(mock_send.called)
        msg_text = mock_send.call_args[0][1]
        self.assertIn("\u8fc7\u671f", msg_text)  # 过期


class TestFullWizardFlow(unittest.TestCase):
    """End-to-end test: preset → modify → apply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch.object(bot_commands, "answer_callback_query")
    @patch.object(bot_commands, "send_text")
    @patch.object(bot_commands, "is_ops_allowed", return_value=True)
    @patch("bot_commands.get_available_models", return_value=[
        {"id": "claude-opus-4-6", "provider": "anthropic", "status": "available"},
        {"id": "gpt-4.1", "provider": "openai", "status": "available"},
    ])
    def test_full_flow(self, _models, _ops, mock_send, _acb):
        """Complete wizard: select preset → modify stage → apply."""
        # Step 1: Select preset
        bot_commands.handle_callback_query(_make_cb("pipeline_preset:plan_code_verify"))
        pending = peek_pending_action(100, 200)
        self.assertEqual(pending["action"], "pipeline_configure")
        self.assertEqual(len(pending["context"]["stages"]), 3)

        # Step 2: Click on stage 0 to configure model
        bot_commands.handle_callback_query(_make_cb("pipeline_stage_cfg:0"))

        # Step 3: Select a model for stage 0
        bot_commands.handle_callback_query(
            _make_cb("stage_model:0:anthropic:claude-opus-4-6"))
        pending = peek_pending_action(100, 200)
        self.assertEqual(pending["context"]["stages"][0]["model"], "claude-opus-4-6")

        # Step 4: Modify stage 2 model
        bot_commands.handle_callback_query(
            _make_cb("stage_model:2:openai:gpt-4.1"))
        pending = peek_pending_action(100, 200)
        self.assertEqual(pending["context"]["stages"][2]["model"], "gpt-4.1")

        # Step 5: Apply
        with patch.object(bot_commands, "set_pipeline_stages") as mock_set:
            bot_commands.handle_callback_query(_make_cb("pipeline_apply"))
            mock_set.assert_called_once()
            applied = mock_set.call_args[0][0]
            self.assertEqual(applied[0]["model"], "claude-opus-4-6")
            self.assertEqual(applied[2]["model"], "gpt-4.1")
            # Stage 1 should have no model (default)
            self.assertFalse(applied[1].get("model"))

        # Pending action cleared
        self.assertIsNone(peek_pending_action(100, 200))


if __name__ == "__main__":
    unittest.main()
