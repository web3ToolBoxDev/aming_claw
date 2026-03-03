"""Tests for the i18n module and config language persistence.

Covers:
- t() translation with default and switched languages
- t() with format args interpolation
- Fallback chain: current lang -> zh -> key itself
- set_language / get_language / load_locale / reload_locale
- Invalid language fallback
- Locale caching behaviour
- set_config_language / get_config_language persistence
- _LazyTranslation proxy class from interactive_menu
- _TranslatedDict proxy class from interactive_menu
- language_select_keyboard structure
- Locale file key parity (zh and en have same structure)
"""
import json
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

import i18n  # noqa: E402
from i18n import get_language, load_locale, reload_locale, set_language, t  # noqa: E402
from interactive_menu import (  # noqa: E402
    _LazyTranslation,
    _TranslatedDict,
    language_select_keyboard,
)


def _reset_i18n():
    """Reset i18n module state to defaults for test isolation."""
    i18n._current_lang = "zh"
    i18n._locales.clear()


class TestTranslateDefaultLanguage(unittest.TestCase):
    """t() with default language (zh) returns Chinese text."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_simple_key(self):
        result = t("status.pending")
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "status.pending")

    def test_nested_key(self):
        result = t("menu.new_task")
        self.assertIn("新建任务", result)

    def test_default_language_is_zh(self):
        self.assertEqual(get_language(), "zh")


class TestTranslateEnglish(unittest.TestCase):
    """set_language('en') + t() returns English text."""

    def setUp(self):
        _reset_i18n()
        set_language("en")

    def tearDown(self):
        _reset_i18n()

    def test_english_key(self):
        result = t("status.pending")
        self.assertEqual(result, "Pending")

    def test_english_menu_key(self):
        result = t("menu.new_task")
        self.assertIn("New Task", result)

    def test_get_language_returns_en(self):
        self.assertEqual(get_language(), "en")


class TestTranslateWithFormatArgs(unittest.TestCase):
    """t() with format args: t('msg.task_created', code='T1', task_id='xxx', text='hello')."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_format_args_zh(self):
        result = t("msg.task_created", code="T1", task_id="xxx", text="hello")
        self.assertIn("T1", result)
        self.assertIn("xxx", result)
        self.assertIn("hello", result)

    def test_format_args_en(self):
        set_language("en")
        result = t("msg.task_created", code="T1", task_id="xxx", text="hello")
        self.assertIn("T1", result)
        self.assertIn("xxx", result)
        self.assertIn("hello", result)

    def test_format_args_partial(self):
        """Extra format args are silently ignored."""
        result = t("status.pending", extra="ignored")
        # Should still return the translated value without error
        self.assertNotEqual(result, "status.pending")

    def test_format_args_missing_key_no_crash(self):
        """If a format placeholder is missing from kwargs, t() should not crash."""
        # msg.task_created has {code}, {task_id}, {text} placeholders
        # Passing incomplete kwargs should not raise; value is returned as-is
        result = t("msg.task_created", code="T1")
        self.assertIsInstance(result, str)


class TestFallbackChain(unittest.TestCase):
    """Fallback: missing key in en falls back to zh value;
    completely missing key returns the key itself."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_missing_in_en_falls_back_to_zh(self):
        """If a key exists in zh but not in en, t() should return zh value."""
        set_language("en")
        zh_locale = load_locale("zh")
        en_locale = load_locale("en")
        # Find a key that exists in zh but not in en (if any),
        # or use a synthetic one to test the fallback.
        # We'll test by injecting a synthetic key.
        zh_locale.setdefault("_test_only", {})["fallback_key"] = "Chinese Fallback"
        # en should not have this key
        result = t("_test_only.fallback_key")
        self.assertEqual(result, "Chinese Fallback")
        # Cleanup
        del zh_locale["_test_only"]

    def test_completely_missing_key_returns_key(self):
        result = t("this.key.does.not.exist")
        self.assertEqual(result, "this.key.does.not.exist")

    def test_completely_missing_key_en_returns_key(self):
        set_language("en")
        result = t("this.key.does.not.exist.either")
        self.assertEqual(result, "this.key.does.not.exist.either")


class TestSetLanguageInvalid(unittest.TestCase):
    """set_language('invalid') falls back to 'zh'."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_invalid_language_falls_back(self):
        set_language("invalid")
        self.assertEqual(get_language(), "zh")

    def test_invalid_language_fr(self):
        set_language("fr")
        self.assertEqual(get_language(), "zh")

    def test_empty_string_falls_back(self):
        set_language("")
        self.assertEqual(get_language(), "zh")


class TestGetLanguage(unittest.TestCase):
    """get_language() returns current language."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_default(self):
        self.assertEqual(get_language(), "zh")

    def test_after_switch_to_en(self):
        set_language("en")
        self.assertEqual(get_language(), "en")

    def test_after_switch_back_to_zh(self):
        set_language("en")
        set_language("zh")
        self.assertEqual(get_language(), "zh")


class TestLoadLocaleCaching(unittest.TestCase):
    """load_locale() caching works - calling twice returns same dict object."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_cache_hit(self):
        locale1 = load_locale("zh")
        locale2 = load_locale("zh")
        self.assertIs(locale1, locale2)

    def test_cache_hit_en(self):
        locale1 = load_locale("en")
        locale2 = load_locale("en")
        self.assertIs(locale1, locale2)

    def test_reload_clears_cache(self):
        locale1 = load_locale("zh")
        locale2 = reload_locale("zh")
        # reload should return a new dict (not the same object)
        self.assertIsNot(locale1, locale2)
        # But contents should be equivalent
        self.assertEqual(locale1.keys(), locale2.keys())

    def test_nonexistent_locale_cached_as_empty(self):
        locale = load_locale("xx_nonexistent")
        self.assertEqual(locale, {})
        # Second call returns same empty dict from cache
        locale2 = load_locale("xx_nonexistent")
        self.assertIs(locale, locale2)


class TestConfigLanguagePersistence(unittest.TestCase):
    """set_config_language() persists to agent_config.json and updates i18n runtime.
    get_config_language() reads from agent_config.json."""

    def setUp(self):
        _reset_i18n()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        _reset_i18n()
        self.tmp.cleanup()

    def test_set_config_language_persists(self):
        from config import get_config_language, set_config_language
        set_config_language("en", changed_by=12345)
        self.assertEqual(get_config_language(), "en")
        # i18n runtime should also be updated
        self.assertEqual(get_language(), "en")

    def test_set_config_language_zh(self):
        from config import get_config_language, set_config_language
        set_config_language("en")
        set_config_language("zh")
        self.assertEqual(get_config_language(), "zh")
        self.assertEqual(get_language(), "zh")

    def test_set_config_language_invalid_falls_back(self):
        from config import get_config_language, set_config_language
        set_config_language("invalid")
        self.assertEqual(get_config_language(), "zh")
        self.assertEqual(get_language(), "zh")

    def test_get_config_language_default(self):
        from config import get_config_language
        # No config file yet, should return default
        self.assertEqual(get_config_language(), "zh")

    def test_config_file_content(self):
        from config import set_config_language
        from utils import load_json, tasks_root
        set_config_language("en", changed_by=99)
        config_path = tasks_root() / "state" / "agent_config.json"
        self.assertTrue(config_path.exists())
        data = load_json(config_path)
        self.assertEqual(data["language"], "en")
        self.assertEqual(data["changed_by"], 99)
        self.assertIn("updated_at", data)

    def test_get_config_language_env_fallback(self):
        """When no config file and AGENT_LANGUAGE env is set, use that."""
        from config import get_config_language
        with patch.dict(os.environ, {"AGENT_LANGUAGE": "en"}):
            self.assertEqual(get_config_language(), "en")


class TestLazyTranslation(unittest.TestCase):
    """The _LazyTranslation class from interactive_menu.py works correctly."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_str(self):
        lazy = _LazyTranslation("status.pending")
        self.assertIsInstance(str(lazy), str)
        self.assertNotEqual(str(lazy), "status.pending")

    def test_repr(self):
        lazy = _LazyTranslation("status.pending")
        self.assertIn("_LazyTranslation", repr(lazy))
        self.assertIn("status.pending", repr(lazy))

    def test_format(self):
        lazy = _LazyTranslation("msg.task_created")
        result = lazy.format(code="T1", task_id="xxx", text="hello")
        self.assertIn("T1", result)

    def test_contains(self):
        lazy = _LazyTranslation("status.pending")
        resolved = str(lazy)
        # The first character of the resolved string should be in the lazy proxy
        if resolved:
            self.assertIn(resolved[0], lazy)

    def test_len(self):
        lazy = _LazyTranslation("status.pending")
        self.assertEqual(len(lazy), len(str(lazy)))

    def test_eq_str(self):
        lazy = _LazyTranslation("status.pending")
        self.assertEqual(lazy, str(lazy))

    def test_eq_lazy(self):
        lazy1 = _LazyTranslation("status.pending")
        lazy2 = _LazyTranslation("status.pending")
        self.assertEqual(lazy1, lazy2)

    def test_hash(self):
        lazy = _LazyTranslation("status.pending")
        self.assertEqual(hash(lazy), hash(str(lazy)))

    def test_add(self):
        lazy = _LazyTranslation("status.pending")
        result = lazy + " suffix"
        self.assertTrue(result.endswith(" suffix"))

    def test_radd(self):
        lazy = _LazyTranslation("status.pending")
        result = "prefix " + lazy
        self.assertTrue(result.startswith("prefix "))

    def test_iter(self):
        lazy = _LazyTranslation("status.pending")
        chars = list(lazy)
        self.assertEqual(chars, list(str(lazy)))

    def test_getattr_delegates(self):
        """Attribute access like .strip() delegates to resolved string."""
        lazy = _LazyTranslation("status.pending")
        self.assertEqual(lazy.strip(), str(lazy).strip())

    def test_language_switch_reflects(self):
        """Lazy translation reflects language change at access time."""
        lazy = _LazyTranslation("status.pending")
        zh_val = str(lazy)
        set_language("en")
        en_val = str(lazy)
        self.assertNotEqual(zh_val, en_val)
        self.assertEqual(en_val, "Pending")


class TestTranslatedDict(unittest.TestCase):
    """The _TranslatedDict class from interactive_menu.py works correctly."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_getitem(self):
        td = _TranslatedDict("status")
        result = td["pending"]
        self.assertNotEqual(result, "status.pending")
        self.assertIsInstance(result, str)

    def test_get_existing(self):
        td = _TranslatedDict("status")._with_keys(["pending", "processing"])
        result = td.get("pending")
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "status.pending")

    def test_get_missing_returns_default(self):
        td = _TranslatedDict("status")._with_keys(["pending"])
        result = td.get("nonexistent_key_xyz", "fallback")
        # When key resolves to itself (raw_key), default is used
        self.assertEqual(result, "fallback")

    def test_contains(self):
        td = _TranslatedDict("status")._with_keys(["pending", "processing"])
        self.assertIn("pending", td)
        self.assertNotIn("nonexistent", td)

    def test_items(self):
        td = _TranslatedDict("status")._with_keys(["pending", "processing"])
        items = td.items()
        self.assertEqual(len(items), 2)
        keys = [k for k, v in items]
        self.assertIn("pending", keys)
        self.assertIn("processing", keys)

    def test_keys(self):
        td = _TranslatedDict("status")._with_keys(["pending", "processing"])
        self.assertEqual(td.keys(), ["pending", "processing"])

    def test_values(self):
        td = _TranslatedDict("status")._with_keys(["pending"])
        values = td.values()
        self.assertEqual(len(values), 1)
        self.assertIsInstance(values[0], str)

    def test_language_switch_reflects(self):
        """_TranslatedDict reflects language change at access time."""
        td = _TranslatedDict("status")
        zh_val = td["pending"]
        set_language("en")
        en_val = td["pending"]
        self.assertNotEqual(zh_val, en_val)
        self.assertEqual(en_val, "Pending")


class TestLanguageSelectKeyboard(unittest.TestCase):
    """Language select keyboard is correct."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    def test_keyboard_structure(self):
        kb = language_select_keyboard()
        self.assertIn("inline_keyboard", kb)
        rows = kb["inline_keyboard"]
        # Should have 2 rows: [zh, en] and [back]
        self.assertEqual(len(rows), 2)

    def test_language_buttons(self):
        kb = language_select_keyboard()
        lang_row = kb["inline_keyboard"][0]
        self.assertEqual(len(lang_row), 2)
        # Check callback data
        callbacks = [btn["callback_data"] for btn in lang_row]
        self.assertIn("menu:lang_zh", callbacks)
        self.assertIn("menu:lang_en", callbacks)

    def test_back_button(self):
        kb = language_select_keyboard()
        back_row = kb["inline_keyboard"][1]
        self.assertEqual(len(back_row), 1)
        self.assertEqual(back_row[0]["callback_data"], "menu:sub_system")


class TestLocaleKeyParity(unittest.TestCase):
    """Locale files have matching keys (zh and en have same structure)."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    @staticmethod
    def _collect_keys(data, prefix=""):
        """Recursively collect all leaf keys from a nested dict."""
        keys = set()
        for k, v in data.items():
            full_key = "{}.{}".format(prefix, k) if prefix else k
            if isinstance(v, dict):
                keys.update(TestLocaleKeyParity._collect_keys(v, full_key))
            else:
                keys.add(full_key)
        return keys

    def test_zh_and_en_have_same_keys(self):
        """Both locale files should have matching key structure."""
        locales_dir = AGENT_DIR / "locales"
        with open(str(locales_dir / "zh.json"), "r", encoding="utf-8") as f:
            zh_data = json.load(f)
        with open(str(locales_dir / "en.json"), "r", encoding="utf-8") as f:
            en_data = json.load(f)

        zh_keys = self._collect_keys(zh_data)
        en_keys = self._collect_keys(en_data)

        missing_in_en = zh_keys - en_keys
        missing_in_zh = en_keys - zh_keys

        self.assertEqual(
            missing_in_en, set(),
            "Keys present in zh.json but missing in en.json: {}".format(
                sorted(missing_in_en))
        )
        self.assertEqual(
            missing_in_zh, set(),
            "Keys present in en.json but missing in zh.json: {}".format(
                sorted(missing_in_zh))
        )


class TestLanguageSwitchRegistersCommands(unittest.TestCase):
    """Switching language via callback should re-register bot commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        state_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _reset_i18n()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()
        _reset_i18n()

    @patch("coordinator.register_bot_commands")
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_lang_en_registers_commands(self, mock_send, mock_answer, mock_register):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb1",
            "data": "menu:lang_en",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_register.assert_called_once()

    @patch("coordinator.register_bot_commands")
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_lang_zh_registers_commands(self, mock_send, mock_answer, mock_register):
        from bot_commands import handle_callback_query
        cb = {
            "id": "cb2",
            "data": "menu:lang_zh",
            "message": {"chat": {"id": 100}},
            "from": {"id": 200},
        }
        handle_callback_query(cb)
        mock_register.assert_called_once()


class TestLocaleLeafValues(unittest.TestCase):
    """Leaf value and top-level section checks for locale files."""

    def setUp(self):
        _reset_i18n()

    def tearDown(self):
        _reset_i18n()

    @staticmethod
    def _collect_keys(data, prefix=""):
        keys = set()
        for k, v in data.items():
            full_key = "{}.{}".format(prefix, k) if prefix else k
            if isinstance(v, dict):
                keys.update(TestLocaleLeafValues._collect_keys(v, full_key))
            else:
                keys.add(full_key)
        return keys

    def test_all_leaf_values_are_strings(self):
        """All leaf values in locale files should be strings."""
        locales_dir = AGENT_DIR / "locales"
        for lang in ("zh", "en"):
            with open(str(locales_dir / "{}.json".format(lang)),
                       "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in self._collect_keys(data):
                # Resolve the key through the nested dict
                parts = key.split(".")
                node = data
                for part in parts:
                    node = node[part]
                self.assertIsInstance(
                    node, str,
                    "Locale {}: key '{}' value is {} not str".format(
                        lang, key, type(node).__name__)
                )

    def test_zh_and_en_have_same_top_level_sections(self):
        """Both locale files should have same top-level sections."""
        locales_dir = AGENT_DIR / "locales"
        with open(str(locales_dir / "zh.json"), "r", encoding="utf-8") as f:
            zh_data = json.load(f)
        with open(str(locales_dir / "en.json"), "r", encoding="utf-8") as f:
            en_data = json.load(f)

        self.assertEqual(
            sorted(zh_data.keys()), sorted(en_data.keys()),
            "Top-level sections differ between zh and en"
        )


if __name__ == "__main__":
    unittest.main()
