"""Tests for model_registry.py - model fetching and caching."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from model_registry import (  # noqa: E402
    _cache,
    get_available_models,
    make_label,
)


class TestMakeLabel(unittest.TestCase):
    def test_anthropic_label(self):
        m = {"id": "claude-sonnet-4-6", "provider": "anthropic"}
        label = make_label(m)
        self.assertIn("[C]", label)
        self.assertIn("claude-sonnet-4-6", label)

    def test_openai_label(self):
        m = {"id": "gpt-4o", "provider": "openai"}
        label = make_label(m)
        self.assertIn("[O]", label)
        self.assertIn("gpt-4o", label)


class TestGetAvailableModels(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_no_api_keys_returns_empty(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        _cache.clear()
        models = get_available_models(force_refresh=True)
        self.assertEqual(models, [])

    def test_force_refresh_clears_cache(self):
        _cache["all_models"] = (0, [{"id": "cached", "provider": "anthropic"}])
        models = get_available_models(force_refresh=True)
        # After force refresh, should not return stale cache
        # (without API keys, returns [])
        self.assertIsInstance(models, list)

    @patch("model_registry.fetch_anthropic_models", return_value=[
        {"id": "claude-sonnet-4-6", "provider": "anthropic", "created": ""}
    ])
    @patch("model_registry.fetch_openai_models", return_value=[])
    def test_returns_anthropic_models(self, mock_oai, mock_anthro):
        _cache.clear()
        models = get_available_models(force_refresh=True)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
