"""Tests for English Practice skill - config, menu, callback, and interaction flow."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from config import get_skill_english_practice, set_skill_english_practice  # noqa: E402
from interactive_menu import (  # noqa: E402
    PENDING_PROMPTS,
    eng_practice_confirm_keyboard,
    set_pending_action,
    get_pending_action,
    skills_menu_keyboard,
)


class TestConfigDefaultOff(unittest.TestCase):
    """test_config_default_off - 默认配置下技能为关闭"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_config_default_off(self):
        self.assertFalse(get_skill_english_practice())


class TestConfigToggleOnOff(unittest.TestCase):
    """test_config_toggle_on_off - 开关切换后状态正确持久化"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_config_toggle_on_off(self):
        # Default off
        self.assertFalse(get_skill_english_practice())
        # Toggle on
        set_skill_english_practice(True)
        self.assertTrue(get_skill_english_practice())
        # Toggle off
        set_skill_english_practice(False)
        self.assertFalse(get_skill_english_practice())

    def test_persistence(self):
        """Data persists in agent_config.json."""
        set_skill_english_practice(True, changed_by=12345)
        from utils import load_json, tasks_root
        data = load_json(tasks_root() / "state" / "agent_config.json")
        self.assertTrue(data.get("skill_english_practice"))
        self.assertEqual(data.get("changed_by"), 12345)


class TestSkillsMenuShowsStatus(unittest.TestCase):
    """test_skills_menu_shows_status - 技能菜单按钮显示正确的开关状态文本"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_skills_menu_shows_off(self):
        set_skill_english_practice(False)
        kb = skills_menu_keyboard()
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("menu:skill_eng_practice_toggle", all_data)
        all_text = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("关闭" in t or "OFF" in t for t in all_text))

    def test_skills_menu_shows_on(self):
        set_skill_english_practice(True)
        kb = skills_menu_keyboard()
        all_text = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any("开启" in t or "ON" in t for t in all_text))


class TestToggleCallbackSwitchesState(unittest.TestCase):
    """test_toggle_callback_switches_state - 回调处理正确切换状态"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_toggle_callback_switches_state(self, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_skill_english_practice(False)
        _handle_menu_callback("cb1", "menu:skill_eng_practice_toggle", 100, 200)
        self.assertTrue(get_skill_english_practice())
        # Should have sent confirmation message
        self.assertTrue(mock_send.called)
        msg = str(mock_send.call_args_list[0][0][1])
        self.assertTrue("开启" in msg or "enabled" in msg)

        # Toggle back off
        _handle_menu_callback("cb2", "menu:skill_eng_practice_toggle", 100, 200)
        self.assertFalse(get_skill_english_practice())


class TestTaskCreationShowsSkillStatus(unittest.TestCase):
    """test_task_creation_shows_skill_status - 任务创建提示中显示技能状态"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    @patch("bot_commands.workspace_select_keyboard", return_value={"inline_keyboard": []})
    @patch("workspace_registry.list_workspaces", return_value=[{"id": "ws1"}])
    @patch("workspace_registry.ensure_current_workspace_registered")
    def test_shows_status_off(self, mock_ensure, mock_list, mock_ws_kb, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_skill_english_practice(False)
        _handle_menu_callback("cb1", "menu:new_task", 100, 200)
        self.assertTrue(mock_send.called)
        sent_text = str(mock_send.call_args_list[0][0][1])
        self.assertTrue("已关闭" in sent_text or "OFF" in sent_text or "新建任务" in sent_text or "New Task" in sent_text)

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    @patch("workspace_registry.list_workspaces", return_value=[{"id": "ws1"}])
    @patch("workspace_registry.ensure_current_workspace_registered")
    def test_shows_eng_practice_prompt_when_on(self, mock_ensure, mock_list, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_skill_english_practice(True)
        _handle_menu_callback("cb1", "menu:new_task", 100, 200)
        self.assertTrue(mock_send.called)
        sent_text = str(mock_send.call_args_list[0][0][1])
        self.assertTrue("英文" in sent_text or "English" in sent_text)


class TestEngPracticeFlowConfirm(unittest.TestCase):
    """test_eng_practice_flow_confirm - 完整英文练习流程：输入英文 → AI 评估 → 确认 → 创建任务"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.task_inline_keyboard", return_value={"inline_keyboard": []})
    @patch("bot_commands.send_text")
    @patch("bot_commands._evaluate_english_text", return_value={
        "original": "Add a login button",
        "corrected": "Add a login button to the homepage",
        "issues": [{"type": "expression", "original": "Add a login button",
                     "suggestion": "Add a login button to the homepage",
                     "explanation": "Be more specific about location"}],
        "chinese_meaning": "在首页添加一个登录按钮",
    })
    def test_eng_practice_flow_confirm(self, mock_eval, mock_send, mock_kb):
        from bot_commands import handle_pending_action
        set_pending_action(100, 200, "eng_practice_input")

        # Step 1: User sends English text
        result = handle_pending_action(100, 200, "Add a login button")
        self.assertTrue(result)
        # Should have displayed AI evaluation result
        self.assertTrue(mock_send.called)
        eval_msg = str(mock_send.call_args_list[0][0][1])
        self.assertTrue("login" in eval_msg.lower() or "登录" in eval_msg)

        # Step 2: eng_practice_confirm pending action should be set
        from interactive_menu import peek_pending_action
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "eng_practice_confirm")
        self.assertEqual(pending["context"]["corrected_text"], "Add a login button to the homepage")

    @patch("bot_commands.task_inline_keyboard", return_value={"inline_keyboard": []})
    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    @patch("bot_commands.create_task", return_value="task-123")
    @patch("bot_commands.load_json", return_value={"task_code": "T0001"})
    def test_confirm_correct_creates_task(self, mock_load, mock_create, mock_send, mock_answer, mock_kb):
        from bot_commands import _handle_menu_callback
        # Set up eng_practice_confirm pending action
        set_pending_action(100, 200, "eng_practice_confirm", {
            "original_text": "Add a login button",
            "corrected_text": "Add a login button to the homepage",
            "chinese_meaning": "在首页添加一个登录按钮",
        })
        _handle_menu_callback("cb1", "menu:eng_confirm_correct", 100, 200)
        # Should call create_task with corrected text
        self.assertTrue(mock_create.called)
        call_args = mock_create.call_args
        self.assertIn("Add a login button to the homepage", str(call_args))


class TestEngPracticeFlowFallbackChinese(unittest.TestCase):
    """test_eng_practice_flow_fallback_chinese - 选择中文降级"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_fallback_chinese(self, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_pending_action(100, 200, "eng_practice_confirm", {
            "original_text": "Add a login button",
            "corrected_text": "Add a login button to the homepage",
            "chinese_meaning": "在首页添加一个登录按钮",
        })
        _handle_menu_callback("cb1", "menu:eng_confirm_chinese", 100, 200)
        # Should set pending action back to new_task
        from interactive_menu import peek_pending_action
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "new_task")


class TestEngPracticeFlowRetryEnglish(unittest.TestCase):
    """test_eng_practice_flow_retry_english - 重新英文描述流程"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    def test_retry_english(self, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_pending_action(100, 200, "eng_practice_confirm", {
            "original_text": "Add a login button",
            "corrected_text": "Add a login button to the homepage",
            "chinese_meaning": "在首页添加一个登录按钮",
        })
        _handle_menu_callback("cb1", "menu:eng_confirm_retry", 100, 200)
        from interactive_menu import peek_pending_action
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "eng_practice_input")


class TestEngPracticeAiFailureFallback(unittest.TestCase):
    """test_eng_practice_ai_failure_fallback - AI 调用失败时降级创建任务"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.task_inline_keyboard", return_value={"inline_keyboard": []})
    @patch("bot_commands.send_text")
    @patch("bot_commands._evaluate_english_text", return_value=None)
    @patch("bot_commands.create_task", return_value="task-456")
    @patch("bot_commands.load_json", return_value={"task_code": "T0002"})
    def test_ai_failure_fallback(self, mock_load, mock_create, mock_eval, mock_send, mock_kb):
        from bot_commands import handle_pending_action
        set_pending_action(100, 200, "eng_practice_input")

        result = handle_pending_action(100, 200, "Add a login button")
        self.assertTrue(result)
        # Should have sent fallback message
        self.assertTrue(mock_send.called)
        msgs = [str(call[0][1]) for call in mock_send.call_args_list]
        self.assertTrue(any("失败" in m or "failed" in m.lower() for m in msgs))
        # Should still create task with original text
        self.assertTrue(mock_create.called)


class TestEngPracticeDisabledNoEffect(unittest.TestCase):
    """test_eng_practice_disabled_no_effect - 技能关闭时任务创建流程不受影响"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.answer_callback_query")
    @patch("bot_commands.send_text")
    @patch("workspace_registry.list_workspaces", return_value=[{"id": "ws1"}])
    @patch("workspace_registry.ensure_current_workspace_registered")
    def test_disabled_no_effect(self, mock_ensure, mock_list, mock_send, mock_answer):
        from bot_commands import _handle_menu_callback
        set_skill_english_practice(False)
        _handle_menu_callback("cb1", "menu:new_task", 100, 200)
        # Should set pending action to new_task (not eng_practice_input)
        from interactive_menu import peek_pending_action
        pending = peek_pending_action(100, 200)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "new_task")


class TestEngPracticeConfirmKeyboard(unittest.TestCase):
    """Tests for the eng_practice_confirm_keyboard and pending prompts."""

    def test_confirm_keyboard_structure(self):
        kb = eng_practice_confirm_keyboard()
        self.assertIn("inline_keyboard", kb)
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("menu:eng_confirm_correct", all_data)
        self.assertIn("menu:eng_confirm_chinese", all_data)
        self.assertIn("menu:eng_confirm_retry", all_data)

    def test_pending_prompts_has_eng_practice(self):
        self.assertIn("eng_practice_input", PENDING_PROMPTS)
        self.assertIn("eng_practice_confirm", PENDING_PROMPTS)


if __name__ == "__main__":
    unittest.main()
