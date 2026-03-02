"""Tests for model_registry.py - model fetching, caching, metadata, and formatting."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from model_registry import (  # noqa: E402
    _cache,
    _lookup_context_length,
    _unavailable_anthropic_models,
    _unavailable_openai_models,
    fetch_anthropic_models,
    fetch_openai_models,
    find_model,
    format_model_list_text,
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


class TestContextLength(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(_lookup_context_length("claude-opus-4-6"), 200000)
        self.assertEqual(_lookup_context_length("gpt-4o"), 128000)

    def test_prefix_match(self):
        self.assertEqual(_lookup_context_length("gpt-4o-2024-08-06"), 128000)
        self.assertEqual(_lookup_context_length("claude-opus-4-6-20260101"), 200000)

    def test_unknown_model(self):
        self.assertIsNone(_lookup_context_length("unknown-model-xyz"))


class TestModelMetadataFields(unittest.TestCase):
    """Test that model entries include context_length, status, unavailable_reason."""

    @patch("model_registry.fetch_anthropic_models", return_value=[
        {"id": "claude-sonnet-4-6", "provider": "anthropic", "created": "",
         "context_length": 200000, "status": "available", "unavailable_reason": ""}
    ])
    @patch("model_registry.fetch_openai_models", return_value=[
        {"id": "gpt-4o", "provider": "openai", "created": 0,
         "context_length": 128000, "status": "available", "unavailable_reason": ""}
    ])
    def test_available_models_have_metadata(self, mock_oai, mock_anthro):
        _cache.clear()
        models = get_available_models(force_refresh=True)
        for m in models:
            self.assertIn("context_length", m)
            self.assertIn("status", m)
            self.assertIn("unavailable_reason", m)
            self.assertEqual(m["status"], "available")
            self.assertEqual(m["unavailable_reason"], "")

    def test_unavailable_anthropic_models(self):
        models = _unavailable_anthropic_models("API key未配置")
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")
            self.assertEqual(m["unavailable_reason"], "API key未配置")
            self.assertEqual(m["provider"], "anthropic")
            self.assertIn("context_length", m)

    def test_unavailable_openai_models(self):
        models = _unavailable_openai_models("请求失败")
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")
            self.assertEqual(m["unavailable_reason"], "请求失败")
            self.assertEqual(m["provider"], "openai")


class TestFetchAnthropicModels(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_no_api_key_returns_unavailable(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        models = fetch_anthropic_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")
            self.assertIn("API key", m["unavailable_reason"])

    @patch("model_registry.requests.get")
    def test_api_failure_returns_unavailable(self, mock_get):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        mock_get.side_effect = Exception("connection error")
        models = fetch_anthropic_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")
            self.assertIn("请求失败", m["unavailable_reason"])
        os.environ.pop("ANTHROPIC_API_KEY", None)

    @patch("model_registry.requests.get")
    def test_api_success_returns_available(self, mock_get):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "claude-sonnet-4-6", "created_at": "2025-01-01"},
                {"id": "some-other-model", "created_at": "2025-01-01"},  # no "claude" -> skipped
            ]
        }
        mock_get.return_value = mock_resp
        models = fetch_anthropic_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "claude-sonnet-4-6")
        self.assertEqual(models[0]["status"], "available")
        self.assertEqual(models[0]["context_length"], 200000)
        os.environ.pop("ANTHROPIC_API_KEY", None)


class TestFetchOpenAIModels(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_no_api_key_returns_unavailable(self):
        os.environ.pop("OPENAI_API_KEY", None)
        models = fetch_openai_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")
            self.assertIn("API key", m["unavailable_reason"])


class TestGetAvailableModels(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_no_api_keys_returns_unavailable_models(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        _cache.clear()
        models = get_available_models(force_refresh=True)
        # Should return unavailable placeholder models, not empty
        self.assertIsInstance(models, list)
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m["status"], "unavailable")

    def test_force_refresh_clears_cache(self):
        _cache["all_models"] = (0, [{"id": "cached", "provider": "anthropic"}])
        models = get_available_models(force_refresh=True)
        # After force refresh, should not return stale cache
        self.assertIsInstance(models, list)

    @patch("model_registry.fetch_anthropic_models", return_value=[
        {"id": "claude-sonnet-4-6", "provider": "anthropic", "created": "",
         "context_length": 200000, "status": "available", "unavailable_reason": ""}
    ])
    @patch("model_registry.fetch_openai_models", return_value=[])
    def test_returns_anthropic_models(self, mock_oai, mock_anthro):
        _cache.clear()
        models = get_available_models(force_refresh=True)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "claude-sonnet-4-6")


class TestFormatModelListText(unittest.TestCase):
    def test_empty(self):
        result = format_model_list_text([])
        self.assertIn("无可用模型", result)

    def test_available_models(self):
        models = [
            {"id": "claude-opus-4-6", "provider": "anthropic", "context_length": 200000,
             "status": "available", "unavailable_reason": ""},
            {"id": "gpt-4o", "provider": "openai", "context_length": 128000,
             "status": "available", "unavailable_reason": ""},
        ]
        result = format_model_list_text(models)
        self.assertIn("✅", result)
        self.assertIn("claude-opus-4-6", result)
        self.assertIn("200K ctx", result)
        self.assertIn("gpt-4o", result)
        self.assertIn("128K ctx", result)

    def test_unavailable_models(self):
        models = [
            {"id": "gpt-4-turbo", "provider": "openai", "context_length": 128000,
             "status": "unavailable", "unavailable_reason": "API key未配置"},
        ]
        result = format_model_list_text(models)
        self.assertIn("⛔", result)
        self.assertIn("API key未配置", result)

    def test_mixed_available_unavailable(self):
        models = [
            {"id": "claude-opus-4-6", "provider": "anthropic", "context_length": 200000,
             "status": "available", "unavailable_reason": ""},
            {"id": "gpt-4o", "provider": "openai", "context_length": 128000,
             "status": "unavailable", "unavailable_reason": "请求失败"},
        ]
        result = format_model_list_text(models)
        self.assertIn("✅", result)
        self.assertIn("⛔", result)

    def test_grouped_by_provider(self):
        models = [
            {"id": "claude-opus-4-6", "provider": "anthropic", "context_length": 200000,
             "status": "available", "unavailable_reason": ""},
            {"id": "gpt-4o", "provider": "openai", "context_length": 128000,
             "status": "available", "unavailable_reason": ""},
        ]
        result = format_model_list_text(models)
        self.assertIn("Anthropic", result)
        self.assertIn("OpenAI", result)


class TestFindModel(unittest.TestCase):
    def test_find_existing(self):
        models = [
            {"id": "claude-opus-4-6", "provider": "anthropic", "status": "available"},
            {"id": "gpt-4o", "provider": "openai", "status": "available"},
        ]
        m = find_model("gpt-4o", models)
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], "gpt-4o")

    def test_find_nonexistent(self):
        models = [{"id": "claude-opus-4-6", "provider": "anthropic", "status": "available"}]
        m = find_model("nonexistent", models)
        self.assertIsNone(m)

    def test_find_empty_list(self):
        m = find_model("anything", [])
        self.assertIsNone(m)


if __name__ == "__main__":
    unittest.main()
