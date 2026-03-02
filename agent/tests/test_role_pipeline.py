"""Tests for role pipeline - config, prompts, UI keyboards, and context passing."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from config import (  # noqa: E402
    PIPELINE_PRESETS,
    ROLE_DEFINITIONS,
    ROLE_PIPELINE_ORDER,
    format_pipeline_stages,
    format_role_pipeline_stages,
    get_role_pipeline_stages,
    set_role_pipeline_stages,
    set_role_stage_model,
)
from backends import (  # noqa: E402
    _STAGE_ROLE_PROMPTS,
    _ANALYSIS_STAGES,
    _is_role_pipeline,
    _build_role_context,
    build_pipeline_stage_prompt,
    detect_stage_noop,
)
from interactive_menu import (  # noqa: E402
    role_pipeline_config_keyboard,
    role_model_select_keyboard,
    pipeline_preset_keyboard,
    system_menu_keyboard,
)


class TestRoleDefinitions(unittest.TestCase):
    def test_all_roles_defined(self):
        for role in ROLE_PIPELINE_ORDER:
            self.assertIn(role, ROLE_DEFINITIONS)

    def test_role_has_required_fields(self):
        for role, defn in ROLE_DEFINITIONS.items():
            self.assertIn("label", defn)
            self.assertIn("emoji", defn)
            self.assertIn("default_backend", defn)

    def test_role_order(self):
        self.assertEqual(ROLE_PIPELINE_ORDER, ["pm", "dev", "test", "qa"])


class TestRolePipelinePreset(unittest.TestCase):
    def test_preset_exists(self):
        self.assertIn("role_pipeline", PIPELINE_PRESETS)

    def test_preset_stages(self):
        stages = PIPELINE_PRESETS["role_pipeline"]
        self.assertEqual(len(stages), 4)
        names = [s["name"] for s in stages]
        self.assertEqual(names, ["pm", "dev", "test", "qa"])

    def test_preset_backends(self):
        for s in PIPELINE_PRESETS["role_pipeline"]:
            self.assertEqual(s["backend"], "claude")


class TestRolePipelineConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_stages(self):
        stages = get_role_pipeline_stages()
        self.assertEqual(len(stages), 4)
        names = [s["name"] for s in stages]
        self.assertEqual(names, ["pm", "dev", "test", "qa"])
        for s in stages:
            self.assertEqual(s["model"], "")
            self.assertEqual(s["provider"], "")

    def test_set_and_get(self):
        custom = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
            {"name": "test", "backend": "claude", "model": "gpt-4o", "provider": "openai"},
            {"name": "qa", "backend": "claude", "model": "", "provider": ""},
        ]
        set_role_pipeline_stages(custom, changed_by=42)
        loaded = get_role_pipeline_stages()
        self.assertEqual(len(loaded), 4)
        self.assertEqual(loaded[0]["model"], "claude-opus-4-6")
        self.assertEqual(loaded[0]["provider"], "anthropic")
        self.assertEqual(loaded[2]["model"], "gpt-4o")
        self.assertEqual(loaded[2]["provider"], "openai")

    def test_set_role_stage_model(self):
        # Start with defaults (skip validation since no real API)
        set_role_stage_model("pm", "claude-opus-4-6", provider="anthropic",
                             changed_by=1, validate=False)
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        self.assertEqual(pm["model"], "claude-opus-4-6")
        self.assertEqual(pm["provider"], "anthropic")
        # Other roles unchanged
        dev = next(s for s in stages if s["name"] == "dev")
        self.assertEqual(dev["model"], "")

    def test_set_role_stage_model_multiple(self):
        set_role_stage_model("pm", "claude-opus-4-6", provider="anthropic", validate=False)
        set_role_stage_model("qa", "gpt-4o", provider="openai", validate=False)
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        qa = next(s for s in stages if s["name"] == "qa")
        self.assertEqual(pm["model"], "claude-opus-4-6")
        self.assertEqual(qa["model"], "gpt-4o")
        self.assertEqual(qa["provider"], "openai")

    def test_set_nonexistent_role(self):
        # Should not crash
        set_role_stage_model("nonexistent", "model-x", validate=False)
        stages = get_role_pipeline_stages()
        # No change expected
        self.assertEqual(len(stages), 4)


class TestFormatRolePipelineStages(unittest.TestCase):
    def test_empty(self):
        result = format_role_pipeline_stages([])
        self.assertEqual(result, "(未配置)")

    def test_with_model(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
        ]
        result = format_role_pipeline_stages(stages)
        self.assertIn("产品经理", result)
        self.assertIn("claude-opus-4-6", result)
        self.assertIn("[C]", result)
        self.assertIn("全局模型", result)

    def test_openai_tag(self):
        stages = [
            {"name": "qa", "backend": "claude", "model": "gpt-4o", "provider": "openai"},
        ]
        result = format_role_pipeline_stages(stages)
        self.assertIn("[O]", result)
        self.assertIn("gpt-4o", result)


class TestFormatPipelineStagesWithModel(unittest.TestCase):
    def test_stage_with_model(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6"},
            {"name": "dev", "backend": "claude"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("pm(claude/claude-opus-4-6)", result)
        self.assertIn("dev(claude)", result)


class TestRolePrompts(unittest.TestCase):
    def test_pm_prompt_exists(self):
        self.assertIn("pm", _STAGE_ROLE_PROMPTS)
        self.assertIn("产品经理", _STAGE_ROLE_PROMPTS["pm"])
        self.assertIn("需求文档", _STAGE_ROLE_PROMPTS["pm"])

    def test_dev_prompt_exists(self):
        self.assertIn("dev", _STAGE_ROLE_PROMPTS)
        self.assertIn("开发", _STAGE_ROLE_PROMPTS["dev"])

    def test_qa_prompt_exists(self):
        self.assertIn("qa", _STAGE_ROLE_PROMPTS)
        self.assertIn("验收", _STAGE_ROLE_PROMPTS["qa"])

    def test_test_prompt_exists(self):
        # "test" was already in the original _STAGE_ROLE_PROMPTS
        self.assertIn("test", _STAGE_ROLE_PROMPTS)

    def test_analysis_stages_include_roles(self):
        self.assertIn("pm", _ANALYSIS_STAGES)
        self.assertIn("qa", _ANALYSIS_STAGES)


class TestIsRolePipeline(unittest.TestCase):
    def test_role_pipeline(self):
        stages = [
            {"name": "pm", "backend": "claude"},
            {"name": "dev", "backend": "claude"},
            {"name": "test", "backend": "claude"},
            {"name": "qa", "backend": "claude"},
        ]
        self.assertTrue(_is_role_pipeline(stages))

    def test_non_role_pipeline(self):
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "claude"},
        ]
        self.assertFalse(_is_role_pipeline(stages))

    def test_partial_role_pipeline(self):
        stages = [
            {"name": "pm", "backend": "claude"},
            {"name": "dev", "backend": "claude"},
        ]
        self.assertFalse(_is_role_pipeline(stages))

    def test_wrong_order(self):
        stages = [
            {"name": "dev", "backend": "claude"},
            {"name": "pm", "backend": "claude"},
            {"name": "test", "backend": "claude"},
            {"name": "qa", "backend": "claude"},
        ]
        self.assertFalse(_is_role_pipeline(stages))


class TestBuildRoleContext(unittest.TestCase):
    def test_pm_gets_no_context(self):
        ctx = _build_role_context("pm", {})
        self.assertEqual(ctx, "")

    def test_dev_gets_pm_context(self):
        outputs = {"pm": "需求文档内容"}
        ctx = _build_role_context("dev", outputs)
        self.assertIn("需求文档", ctx)
        self.assertIn("产品经理", ctx)

    def test_test_gets_pm_and_dev_context(self):
        outputs = {"pm": "需求", "dev": "代码变更"}
        ctx = _build_role_context("test", outputs)
        self.assertIn("需求", ctx)
        self.assertIn("代码变更", ctx)

    def test_qa_gets_all_context(self):
        outputs = {"pm": "需求", "dev": "代码", "test": "测试结果"}
        ctx = _build_role_context("qa", outputs)
        self.assertIn("需求", ctx)
        self.assertIn("代码", ctx)
        self.assertIn("测试结果", ctx)

    def test_qa_with_missing_stages(self):
        outputs = {"pm": "需求"}
        ctx = _build_role_context("qa", outputs)
        self.assertIn("需求", ctx)
        self.assertNotIn("代码", ctx)

    def test_unknown_stage_gets_nothing(self):
        outputs = {"pm": "需求", "dev": "代码"}
        ctx = _build_role_context("unknown", outputs)
        self.assertEqual(ctx, "")


class TestBuildPipelineStagePromptForRoles(unittest.TestCase):
    def test_pm_prompt(self):
        task = {"task_id": "test-123", "text": "实现登录功能"}
        prompt = build_pipeline_stage_prompt(task, "pm", "")
        self.assertIn("产品经理", prompt)
        self.assertIn("test-123", prompt)
        self.assertIn("实现登录功能", prompt)

    def test_dev_prompt_with_context(self):
        task = {"task_id": "test-123", "text": "实现登录功能"}
        context = "PM产出了需求文档"
        prompt = build_pipeline_stage_prompt(task, "dev", context)
        self.assertIn("开发", prompt)
        self.assertIn("前序阶段输出", prompt)
        self.assertIn("PM产出了需求文档", prompt)

    def test_qa_prompt(self):
        task = {"task_id": "test-123", "text": "实现登录功能"}
        prompt = build_pipeline_stage_prompt(task, "qa", "")
        self.assertIn("验收", prompt)


class TestDetectStageNoopForRoles(unittest.TestCase):
    def test_pm_noop_empty(self):
        run = {"last_message": "", "stdout": "", "returncode": 0}
        stage = {"name": "pm"}
        reason = detect_stage_noop(run, stage)
        self.assertIsNotNone(reason)

    def test_pm_noop_short(self):
        run = {"last_message": "ok", "stdout": "ok", "returncode": 0}
        stage = {"name": "pm"}
        reason = detect_stage_noop(run, stage)
        self.assertIsNotNone(reason)

    def test_pm_valid_output(self):
        long_output = "需求文档内容 " * 20  # > 50 chars
        run = {"last_message": long_output, "stdout": long_output, "returncode": 0}
        stage = {"name": "pm"}
        reason = detect_stage_noop(run, stage)
        self.assertIsNone(reason)

    def test_qa_noop_ack_only(self):
        run = {"last_message": "收到。", "stdout": "收到。", "returncode": 0}
        stage = {"name": "qa"}
        reason = detect_stage_noop(run, stage)
        self.assertIsNotNone(reason)


class TestRolePipelineConfigKeyboard(unittest.TestCase):
    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_basic_keyboard(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "", "provider": ""},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
            {"name": "test", "backend": "claude", "model": "", "provider": ""},
            {"name": "qa", "backend": "claude", "model": "", "provider": ""},
        ]
        kb = role_pipeline_config_keyboard(stages)
        self._assert_valid_keyboard(kb)
        # 4 role buttons + 1 back button = 5 rows
        self.assertEqual(len(kb["inline_keyboard"]), 5)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("role_cfg:pm", all_data)
        self.assertIn("role_cfg:dev", all_data)
        self.assertIn("role_cfg:test", all_data)
        self.assertIn("role_cfg:qa", all_data)

    def test_keyboard_with_models(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
        ]
        kb = role_pipeline_config_keyboard(stages)
        self._assert_valid_keyboard(kb)
        # Check PM button shows model
        pm_btn = kb["inline_keyboard"][0][0]
        self.assertIn("claude-opus-4-6", pm_btn["text"])


class TestRoleModelSelectKeyboard(unittest.TestCase):
    def _assert_valid_keyboard(self, kb):
        self.assertIn("inline_keyboard", kb)
        for row in kb["inline_keyboard"]:
            for btn in row:
                self.assertIn("text", btn)
                self.assertIn("callback_data", btn)

    def test_with_models(self):
        models = [
            {"id": "claude-opus-4-6", "provider": "anthropic"},
            {"id": "gpt-4o", "provider": "openai"},
        ]
        kb = role_model_select_keyboard("pm", models)
        self._assert_valid_keyboard(kb)
        # 2 models + 1 back button = 3 rows
        self.assertEqual(len(kb["inline_keyboard"]), 3)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("role_model:pm:anthropic:claude-opus-4-6", all_data)
        self.assertIn("role_model:pm:openai:gpt-4o", all_data)

    def test_empty_models(self):
        kb = role_model_select_keyboard("dev", [])
        self._assert_valid_keyboard(kb)
        # Only back button
        self.assertEqual(len(kb["inline_keyboard"]), 1)


class TestSystemMenuHasRolePipelineEntry(unittest.TestCase):
    def test_system_menu_has_role_pipeline(self):
        kb = system_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("role_pipeline_config" in d for d in all_data))


class TestPipelinePresetHasRolePipeline(unittest.TestCase):
    def test_preset_keyboard_has_role_pipeline(self):
        kb = pipeline_preset_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("role_pipeline" in d for d in all_data))


if __name__ == "__main__":
    unittest.main()
