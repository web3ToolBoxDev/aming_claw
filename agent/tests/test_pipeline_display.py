"""Tests for pipeline display: format_stage_execution_summary, _provider_tag, show_pipeline."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from config import _provider_tag, format_pipeline_stages, ROLE_DEFINITIONS  # noqa: E402


class TestProviderTag(unittest.TestCase):
    def test_anthropic(self):
        self.assertEqual(_provider_tag("anthropic"), "[C]")

    def test_openai(self):
        self.assertEqual(_provider_tag("openai"), "[O]")

    def test_empty(self):
        self.assertEqual(_provider_tag(""), "")

    def test_unknown(self):
        self.assertEqual(_provider_tag("azure"), "")


class TestFormatPipelineStagesDisplay(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_pipeline_stages([]), "(empty)")

    @patch("config.get_claude_model", return_value="")
    def test_no_model_fallback_to_backend(self, _):
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "codex"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("plan(claude)", result)
        self.assertIn("code(codex)", result)

    def test_with_model_and_anthropic_provider(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("pm(claude-opus-4-6 [C])", result)

    def test_with_model_and_openai_provider(self):
        stages = [
            {"name": "test", "backend": "openai", "model": "gpt-4o", "provider": "openai"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("test(gpt-4o [O])", result)

    def test_with_model_no_provider(self):
        stages = [
            {"name": "dev", "backend": "claude", "model": "claude-sonnet-4-6"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("dev(claude-sonnet-4-6)", result)
        self.assertNotIn("[C]", result)
        self.assertNotIn("[O]", result)

    @patch("config.get_claude_model", return_value="")
    def test_mixed_stages(self, _):
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "dev", "backend": "claude"},
            {"name": "test", "backend": "openai", "model": "gpt-4o", "provider": "openai"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("pm(claude-opus-4-6 [C])", result)
        self.assertIn("dev(claude)", result)
        self.assertIn("test(gpt-4o [O])", result)
        self.assertIn("\u2192", result)

    @patch("config.get_claude_model", return_value="")
    def test_backward_compat_plan_code_verify(self, _):
        """Non-role pipeline presets should still work with backend fallback."""
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "claude"},
            {"name": "verify", "backend": "codex"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("plan(claude)", result)
        self.assertIn("verify(codex)", result)

    @patch("config.get_model_provider", return_value="anthropic")
    @patch("config.get_claude_model", return_value="claude-opus-4-6")
    def test_global_model_replaces_backend(self, _, __):
        """When global model is set, stages without model show \u5168\u5c40: model_name."""
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "codex"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("plan(\u5168\u5c40: claude-opus-4-6 [C])", result)
        self.assertIn("code(\u5168\u5c40: claude-opus-4-6 [C])", result)
        self.assertNotIn("plan(claude)", result)


class TestFormatStageExecutionSummary(unittest.TestCase):
    """Test format_stage_execution_summary from bot_commands."""

    def _import_func(self):
        from bot_commands import format_stage_execution_summary
        return format_stage_execution_summary

    def test_non_pipeline_returns_empty(self):
        func = self._import_func()
        task = {"executor": {"action": "single"}}
        self.assertEqual(func(task), "")

    def test_no_executor_returns_empty(self):
        func = self._import_func()
        task = {}
        self.assertEqual(func(task), "")

    def test_no_stages_returns_empty(self):
        func = self._import_func()
        task = {"executor": {"action": "pipeline"}}
        self.assertEqual(func(task), "")

    def test_pipeline_with_stages(self):
        func = self._import_func()
        task = {
            "executor": {
                "action": "pipeline",
                "stages": [
                    {"stage": "pm", "stage_index": 1, "backend": "claude",
                     "returncode": 0, "elapsed_ms": 3200, "noop_reason": None},
                    {"stage": "dev", "stage_index": 2, "backend": "claude",
                     "returncode": 0, "elapsed_ms": 45100, "noop_reason": None},
                    {"stage": "test", "stage_index": 3, "backend": "openai",
                     "returncode": 0, "elapsed_ms": 12300, "noop_reason": "output too short"},
                ],
            },
            "stages_model_info": [
                {"stage": "pm", "model": "claude-opus-4-6", "provider": "anthropic"},
                {"stage": "dev", "model": "claude-opus-4-6", "provider": "anthropic"},
                {"stage": "test", "model": "gpt-4o", "provider": "openai"},
            ],
        }
        result = func(task)
        self.assertIn("\u6d41\u6c34\u7ebf\u6267\u884c\u8be6\u60c5", result)
        self.assertIn("\u4ea7\u54c1\u7ecf\u7406", result)  # PM label
        self.assertIn("claude-opus-4-6", result)
        self.assertIn("\u2705", result)
        self.assertIn("\u274c", result)  # noop stage
        self.assertIn("3.2s", result)
        self.assertIn("noop:", result)

    def test_pipeline_no_model_info(self):
        """When stages_model_info is missing, should still work using backend."""
        func = self._import_func()
        task = {
            "executor": {
                "action": "pipeline",
                "stages": [
                    {"stage": "pm", "stage_index": 1, "backend": "claude",
                     "returncode": 0, "elapsed_ms": 1000, "noop_reason": None},
                ],
            },
        }
        result = func(task)
        self.assertIn("claude", result)
        self.assertIn("\u2705", result)

    def test_unexecuted_stage(self):
        """Stage with no elapsed_ms and no returncode should show (\u672a\u6267\u884c)."""
        func = self._import_func()
        task = {
            "executor": {
                "action": "pipeline",
                "stages": [
                    {"stage": "qa", "stage_index": 4, "backend": "claude",
                     "returncode": None, "elapsed_ms": None, "noop_reason": None},
                ],
            },
        }
        result = func(task)
        self.assertIn("\u672a\u6267\u884c", result)

    def test_pipeline_with_stage_level_model(self):
        """T3: stages with model/provider fields should be used directly."""
        func = self._import_func()
        task = {
            "executor": {
                "action": "pipeline",
                "stages": [
                    {"stage": "pm", "stage_index": 1, "backend": "claude",
                     "model": "claude-opus-4-6", "provider": "anthropic",
                     "returncode": 0, "elapsed_ms": 2000, "noop_reason": None},
                ],
            },
        }
        result = func(task)
        self.assertIn("claude-opus-4-6", result)
        self.assertIn("[C]", result)


if __name__ == "__main__":
    unittest.main()
