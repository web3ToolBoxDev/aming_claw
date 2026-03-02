"""Tests for config.py - backend/model/pipeline configuration."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from config import (  # noqa: E402
    KNOWN_BACKENDS,
    KNOWN_STAGE_BACKENDS,
    PIPELINE_PRESETS,
    _parse_pipeline_stages,
    format_pipeline_stages,
    get_agent_backend,
    get_claude_model,
    get_model_provider,
    get_pipeline_stages,
    set_agent_backend,
    set_claude_model,
    set_pipeline_stages,
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
        stages = _parse_pipeline_stages("PLAN:CLAUDE CODE:CODEX")
        self.assertEqual(stages[0]["backend"], "claude")
        self.assertEqual(stages[1]["backend"], "codex")


class TestFormatPipelineStages(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_pipeline_stages([]), "(empty)")

    def test_single_stage(self):
        result = format_pipeline_stages([{"name": "code", "backend": "claude"}])
        self.assertEqual(result, "code(claude)")

    def test_multi_stage(self):
        stages = [
            {"name": "plan", "backend": "claude"},
            {"name": "code", "backend": "claude"},
            {"name": "verify", "backend": "codex"},
        ]
        result = format_pipeline_stages(stages)
        self.assertIn("→", result)
        self.assertIn("plan(claude)", result)
        self.assertIn("verify(codex)", result)


class TestKnownBackends(unittest.TestCase):
    def test_known_set(self):
        self.assertIn("codex", KNOWN_BACKENDS)
        self.assertIn("claude", KNOWN_BACKENDS)
        self.assertIn("pipeline", KNOWN_BACKENDS)

    def test_stage_backends(self):
        self.assertIn("codex", KNOWN_STAGE_BACKENDS)
        self.assertIn("claude", KNOWN_STAGE_BACKENDS)
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
        # Without config file, falls back to env or "codex"
        self.assertEqual(get_agent_backend(), "codex")

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


if __name__ == "__main__":
    unittest.main()
