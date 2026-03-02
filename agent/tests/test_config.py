"""Tests for config.py - backend/model/pipeline configuration."""
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
    KNOWN_BACKENDS,
    KNOWN_STAGE_BACKENDS,
    PIPELINE_PRESETS,
    _parse_pipeline_stages,
    add_workspace_search_root,
    format_pipeline_stages,
    get_agent_backend,
    get_claude_model,
    get_model_provider,
    get_pipeline_stages,
    get_workspace_search_roots,
    remove_workspace_search_root,
    set_agent_backend,
    set_claude_model,
    set_pipeline_stages,
    set_role_stage_model,
    set_workspace_search_roots,
)


class TestParsePipelineStages(unittest.TestCase):
    def test_basic_parse(self):
        stages = _parse_pipeline_stages("plan:claude code:claude verify:codex")
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[0], {"name": "plan", "backend": "claude"})
        self.assertEqual(stages[1], {"name": "code", "backend": "claude"})
        self.assertEqual(stages[2], {"name": "verify", "backend": "codex"})

    def test_default_backend(self):
        stages = _parse_pipeline_stages("plan code verify")
        for s in stages:
            self.assertEqual(s["backend"], "codex")

    def test_invalid_backend_falls_back(self):
        stages = _parse_pipeline_stages("plan:invalid")
        self.assertEqual(stages[0]["backend"], "codex")

    def test_empty_string(self):
        self.assertEqual(_parse_pipeline_stages(""), [])
        self.assertEqual(_parse_pipeline_stages("   "), [])

    def test_case_insensitive(self):
        stages = _parse_pipeline_stages("PLAN:CLAUDE CODE:OPENAI VERIFY:CODEX")
        self.assertEqual(stages[0]["backend"], "claude")
        self.assertEqual(stages[1]["backend"], "openai")
        self.assertEqual(stages[2]["backend"], "codex")


class TestFormatPipelineStages(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_pipeline_stages([]), "(empty)")

    @patch("config.get_claude_model", return_value="")
    def test_single_stage_no_global(self, _):
        """No model set + no global model → fallback to backend name."""
        result = format_pipeline_stages([{"name": "code", "backend": "claude"}])
        self.assertEqual(result, "code(claude)")

    @patch("config.get_claude_model", return_value="")
    def test_multi_stage_no_global(self, _):
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "claude"},
            {"name": "verify", "backend": "codex"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("\u2192", result)
        self.assertIn("plan(claude)", result)
        self.assertIn("verify(codex)", result)

    @patch("config.get_model_provider", return_value="anthropic")
    @patch("config.get_claude_model", return_value="claude-opus-4-6")
    def test_global_model_display(self, _, __):
        """No model set + global model exists → show \u5168\u5c40: model."""
        result = format_pipeline_stages([{"name": "code", "backend": "claude"}])
        self.assertIn("\u5168\u5c40:", result)
        self.assertIn("claude-opus-4-6", result)
        self.assertIn("[C]", result)


class TestKnownBackends(unittest.TestCase):
    def test_known_set(self):
        self.assertIn("codex", KNOWN_BACKENDS)
        self.assertIn("claude", KNOWN_BACKENDS)
        self.assertIn("pipeline", KNOWN_BACKENDS)

    def test_stage_backends(self):
        self.assertIn("codex", KNOWN_STAGE_BACKENDS)
        self.assertIn("claude", KNOWN_STAGE_BACKENDS)
        self.assertIn("openai", KNOWN_STAGE_BACKENDS)
        self.assertNotIn("pipeline", KNOWN_STAGE_BACKENDS)


class TestPipelinePresets(unittest.TestCase):
    def test_presets_exist(self):
        self.assertIn("plan_code_verify", PIPELINE_PRESETS)
        self.assertIn("plan_code", PIPELINE_PRESETS)

    def test_presets_have_valid_backends(self):
        for name, stages in PIPELINE_PRESETS.items():
            for s in stages:
                self.assertIn(s["backend"], KNOWN_STAGE_BACKENDS,
                              msg=f"preset {name} has invalid backend")


class TestGetSetBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("AGENT_BACKEND", None)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_backend(self):
        # Without config file, falls back to env or "pipeline"
        self.assertEqual(get_agent_backend(), "pipeline")

    def test_env_override(self):
        os.environ["AGENT_BACKEND"] = "claude"
        self.assertEqual(get_agent_backend(), "claude")
        os.environ.pop("AGENT_BACKEND", None)

    def test_set_and_get(self):
        set_agent_backend("claude")
        self.assertEqual(get_agent_backend(), "claude")

    def test_set_invalid_raises(self):
        with self.assertRaises(ValueError):
            set_agent_backend("invalid_backend")

    def test_set_pipeline(self):
        set_agent_backend("pipeline")
        self.assertEqual(get_agent_backend(), "pipeline")


class TestGetSetModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("CLAUDE_MODEL", None)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_empty(self):
        self.assertEqual(get_claude_model(), "")

    def test_set_and_get(self):
        set_claude_model("claude-sonnet-4-6", provider="anthropic")
        self.assertEqual(get_claude_model(), "claude-sonnet-4-6")
        self.assertEqual(get_model_provider(), "anthropic")


class TestGetSetPipelineStages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("TASK_PIPELINE_STAGES", None)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_empty(self):
        self.assertEqual(get_pipeline_stages(), [])

    def test_env_fallback(self):
        os.environ["TASK_PIPELINE_STAGES"] = "plan:claude code:codex"
        stages = get_pipeline_stages()
        self.assertEqual(len(stages), 2)
        os.environ.pop("TASK_PIPELINE_STAGES", None)

    def test_set_and_get(self):
        stages = [{"name": "plan", "backend": "claude"}, {"name": "code", "backend": "codex"}]
        set_pipeline_stages(stages)
        loaded = get_pipeline_stages()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["name"], "plan")
        # Should also set backend to pipeline
        self.assertEqual(get_agent_backend(), "pipeline")


class TestWorkspaceSearchRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_empty(self):
        self.assertEqual(get_workspace_search_roots(), [])

    def test_set_and_get(self):
        set_workspace_search_roots(["/tmp/a", "/tmp/b"])
        roots = get_workspace_search_roots()
        self.assertEqual(len(roots), 2)
        self.assertIn("/tmp/a", roots)
        self.assertIn("/tmp/b", roots)

    def test_set_filters_empty(self):
        set_workspace_search_roots(["", "/tmp/a", "  ", "/tmp/b"])
        roots = get_workspace_search_roots()
        self.assertEqual(len(roots), 2)

    def test_add_root(self):
        # Create real dirs for add to validate
        d1 = Path(self.tmp.name) / "projects"
        d1.mkdir()
        ok, msg = add_workspace_search_root(str(d1))
        self.assertTrue(ok)
        roots = get_workspace_search_roots()
        self.assertEqual(len(roots), 1)

    def test_add_duplicate_rejected(self):
        d1 = Path(self.tmp.name) / "projects"
        d1.mkdir()
        add_workspace_search_root(str(d1))
        ok, msg = add_workspace_search_root(str(d1))
        self.assertFalse(ok)
        self.assertIn("\u5df2\u5b58\u5728", msg)

    def test_add_nonexistent_rejected(self):
        ok, msg = add_workspace_search_root("/nonexistent/path/xyz")
        self.assertFalse(ok)
        self.assertIn("\u4e0d\u5b58\u5728", msg)

    def test_add_empty_rejected(self):
        ok, msg = add_workspace_search_root("")
        self.assertFalse(ok)

    def test_remove_root(self):
        set_workspace_search_roots(["/tmp/a", "/tmp/b", "/tmp/c"])
        ok, removed = remove_workspace_search_root(2)
        self.assertTrue(ok)
        self.assertEqual(removed, "/tmp/b")
        roots = get_workspace_search_roots()
        self.assertEqual(len(roots), 2)
        self.assertNotIn("/tmp/b", roots)

    def test_remove_invalid_index(self):
        set_workspace_search_roots(["/tmp/a"])
        ok, msg = remove_workspace_search_root(0)
        self.assertFalse(ok)
        ok, msg = remove_workspace_search_root(5)
        self.assertFalse(ok)

    def test_clear_roots(self):
        set_workspace_search_roots(["/tmp/a", "/tmp/b"])
        set_workspace_search_roots([])
        self.assertEqual(get_workspace_search_roots(), [])

    def test_changed_by_tracked(self):
        set_workspace_search_roots(["/tmp/a"], changed_by=12345)
        from utils import load_json, tasks_root
        data = load_json(tasks_root() / "state" / "agent_config.json")
        self.assertEqual(data.get("changed_by"), 12345)


class TestSetRoleStageModelValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("model_registry.find_model", return_value={
        "id": "claude-opus-4-6", "provider": "anthropic",
        "status": "available", "unavailable_reason": ""})
    def test_available_model_saves(self, mock_find):
        set_role_stage_model("pm", "claude-opus-4-6", provider="anthropic")
        from config import get_role_pipeline_stages
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        self.assertEqual(pm["model"], "claude-opus-4-6")

    @patch("model_registry.find_model", return_value={
        "id": "gpt-4-turbo", "provider": "openai",
        "status": "unavailable", "unavailable_reason": "API key未配置"})
    def test_unavailable_model_raises(self, mock_find):
        with self.assertRaises(ValueError) as cm:
            set_role_stage_model("pm", "gpt-4-turbo", provider="openai")
        self.assertIn("不可用", str(cm.exception))
        self.assertIn("API key", str(cm.exception))

    def test_empty_model_skips_validation(self):
        # Setting empty model should not validate
        set_role_stage_model("pm", "", provider="")
        from config import get_role_pipeline_stages
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        self.assertEqual(pm["model"], "")

    @patch("model_registry.find_model", return_value=None)
    def test_unknown_model_passes(self, mock_find):
        # Model not in registry passes (not unavailable)
        set_role_stage_model("pm", "custom-model-xyz", provider="anthropic")
        from config import get_role_pipeline_stages
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        self.assertEqual(pm["model"], "custom-model-xyz")

    def test_validate_false_skips(self):
        # With validate=False, should save even if model would be unavailable
        with patch("model_registry.find_model", return_value={
            "id": "bad-model", "status": "unavailable", "unavailable_reason": "err"}):
            # This should NOT raise because validate=False
            set_role_stage_model("pm", "bad-model", provider="openai", validate=False)
        from config import get_role_pipeline_stages
        stages = get_role_pipeline_stages()
        pm = next(s for s in stages if s["name"] == "pm")
        self.assertEqual(pm["model"], "bad-model")


if __name__ == "__main__":
    unittest.main()
