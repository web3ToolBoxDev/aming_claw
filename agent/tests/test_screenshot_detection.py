"""Tests for is_screenshot_text and infer_action — screenshot vs task classification."""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from bot_commands import is_screenshot_text, infer_action  # noqa: E402


class TestIsScreenshotText(unittest.TestCase):
    """is_screenshot_text should only match when the PRIMARY intent is screenshot."""

    # ── True positives: these ARE screenshot commands ──

    def test_bare_keyword_chinese(self):
        self.assertTrue(is_screenshot_text("截图"))

    def test_bare_keyword_english(self):
        self.assertTrue(is_screenshot_text("screenshot"))

    def test_slash_command(self):
        self.assertTrue(is_screenshot_text("/screenshot"))

    def test_slash_command_with_body(self):
        self.assertTrue(is_screenshot_text("/screenshot 看看桌面"))

    def test_slash_command_with_task_style_body(self):
        self.assertFalse(is_screenshot_text("/screenshot 命令误判修复"))

    def test_jieping(self):
        self.assertTrue(is_screenshot_text("截屏"))

    def test_polite_prefix_qing(self):
        self.assertTrue(is_screenshot_text("请截图"))

    def test_polite_prefix_bangwo(self):
        self.assertTrue(is_screenshot_text("帮我截图"))

    def test_polite_prefix_qingbangwo(self):
        self.assertTrue(is_screenshot_text("请帮我截图"))

    def test_polite_prefix_bangmang(self):
        self.assertTrue(is_screenshot_text("帮忙截个图"))

    def test_screenshot_with_description(self):
        self.assertTrue(is_screenshot_text("截图看看当前状态"))

    def test_jieping_with_desc(self):
        self.assertTrue(is_screenshot_text("截屏给我看"))

    def test_take_screenshot(self):
        self.assertTrue(is_screenshot_text("take screenshot"))

    def test_take_a_screenshot(self):
        self.assertTrue(is_screenshot_text("take a screenshot"))

    def test_short_screen(self):
        self.assertTrue(is_screenshot_text("screen"))

    def test_short_pingmu(self):
        self.assertTrue(is_screenshot_text("屏幕"))

    def test_short_duoping(self):
        self.assertTrue(is_screenshot_text("多屏"))

    def test_short_shuangping(self):
        self.assertTrue(is_screenshot_text("双屏"))

    def test_all_screens(self):
        self.assertTrue(is_screenshot_text("all screens"))

    def test_screencap(self):
        self.assertTrue(is_screenshot_text("screencap"))

    # ── True negatives: these are NOT screenshot commands ──

    def test_task_mentioning_screenshot_in_qa_output(self):
        """T0046 regression: task description quotes QA output containing '截图'."""
        text = (
            "修复qa报告发送至telegram信息没有正确显示结果的问题，"
            "收到的信息为4. QA ✅ → gpt-5.3-codex\n"
            "测试结果（测试报告、截图、日志、覆盖率、缺陷单）"
        )
        self.assertFalse(is_screenshot_text(text))

    def test_task_mentioning_fix_screenshot_command(self):
        """T0047 regression: task about fixing screenshot misclassification."""
        text = "检查任务T0046,修复任务没执行反而执行截图命令"
        self.assertFalse(is_screenshot_text(text))

    def test_task_starting_with_screenshot_command_phrase(self):
        self.assertFalse(is_screenshot_text("截图命令误判修复"))

    def test_task_starting_with_english_screenshot_command_phrase(self):
        self.assertFalse(is_screenshot_text("screenshot command misclassification fix"))

    def test_fix_screenshot_feature(self):
        self.assertFalse(is_screenshot_text("修复截图功能的bug"))

    def test_screenshot_keyword_in_middle_of_sentence(self):
        self.assertFalse(is_screenshot_text("请修复截图模块的性能问题"))

    def test_long_task_with_screenshot_mention(self):
        self.assertFalse(is_screenshot_text(
            "优化任务流水线，每个阶段任务情况可留档查询，包括截图和日志"
        ))

    def test_empty_string(self):
        self.assertFalse(is_screenshot_text(""))

    def test_none(self):
        self.assertFalse(is_screenshot_text(None))

    def test_normal_task(self):
        self.assertFalse(is_screenshot_text("添加用户认证功能"))

    def test_long_english_with_screenshot_word(self):
        self.assertFalse(is_screenshot_text(
            "Fix the bug where screenshot uploads fail silently in the gallery module"
        ))


class TestInferAction(unittest.TestCase):
    """infer_action should return 'screenshot' only for true screenshot intents."""

    def test_screenshot_command(self):
        self.assertEqual(infer_action("截图"), "screenshot")

    def test_task_with_incidental_screenshot_mention(self):
        text = "检查任务T0046,修复任务没执行反而执行截图命令"
        self.assertNotEqual(infer_action(text), "screenshot")

    def test_task_with_qa_output_quoting_screenshot(self):
        text = "修复qa报告问题，收到的信息包含测试报告、截图、日志"
        self.assertNotEqual(infer_action(text), "screenshot")


if __name__ == "__main__":
    unittest.main()
