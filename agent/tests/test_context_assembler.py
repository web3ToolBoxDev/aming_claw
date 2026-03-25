"""Tests for context_assembler.py — ROLE_BUDGETS, token estimation, enforce_budget."""

import json
import logging
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Allow import of agent modules
agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, agent_dir)

from context_assembler import (
    ROLE_BUDGETS,
    _estimate_tokens,
    _truncate_to_budget,
    ContextAssembler,
)


class TestRoleBudgets(unittest.TestCase):
    """ROLE_BUDGETS dict contents."""

    def test_keys_present(self):
        for role in ("coordinator", "pm", "dev", "tester", "qa"):
            self.assertIn(role, ROLE_BUDGETS)

    def test_values(self):
        self.assertEqual(ROLE_BUDGETS["coordinator"], 8000)
        self.assertEqual(ROLE_BUDGETS["pm"], 6000)
        self.assertEqual(ROLE_BUDGETS["dev"], 4000)
        self.assertEqual(ROLE_BUDGETS["tester"], 3000)
        self.assertEqual(ROLE_BUDGETS["qa"], 3000)


class TestEstimateTokens(unittest.TestCase):
    """_estimate_tokens uses chars // 4."""

    def test_empty(self):
        self.assertEqual(_estimate_tokens(""), 0)

    def test_known_length(self):
        text = "a" * 400
        self.assertEqual(_estimate_tokens(text), 100)

    def test_fraction_truncated(self):
        # 10 chars → 10 // 4 = 2
        self.assertEqual(_estimate_tokens("a" * 10), 2)


class TestTruncateToBudget(unittest.TestCase):
    """_truncate_to_budget respects max_tokens * 4 char limit."""

    def test_no_truncation_needed(self):
        text = "hello"
        result = _truncate_to_budget(text, 10)
        self.assertEqual(result, text)

    def test_truncation_applied(self):
        text = "a" * 200
        result = _truncate_to_budget(text, 10)  # max 40 chars
        self.assertTrue(result.endswith("...(truncated)"))
        # content before marker must be <= 40 chars
        content_part = result[: result.index("\n...(truncated)")]
        self.assertLessEqual(len(content_part), 40)

    def test_exact_boundary(self):
        text = "b" * 40
        result = _truncate_to_budget(text, 10)  # max 40 chars
        self.assertEqual(result, text)  # exactly fits, no truncation


class TestAssembleRoleBudgetLookup(unittest.TestCase):
    """assemble() picks up correct total_budget from ROLE_BUDGETS."""

    def _make_assembler(self):
        ca = ContextAssembler()
        ca._fetch_hard_context = MagicMock(return_value={"status_error": "unavailable"})
        ca._fetch_conversation = MagicMock(return_value=[])
        ca._fetch_memories = MagicMock(return_value=[])
        ca._fetch_runtime = MagicMock(return_value={})
        ca._fetch_git_context = MagicMock(return_value={})
        return ca

    def test_known_role_budget(self):
        ca = self._make_assembler()
        ctx = ca.assemble("proj", 1, "dev")
        self.assertEqual(ctx["_token_budget"], 4000)

    def test_coordinator_budget(self):
        ca = self._make_assembler()
        ctx = ca.assemble("proj", 1, "coordinator")
        self.assertEqual(ctx["_token_budget"], 8000)

    def test_unknown_role_defaults_to_4000(self):
        ca = self._make_assembler()
        with self.assertLogs("context_assembler", level="WARNING") as cm:
            ctx = ca.assemble("proj", 1, "unknown_role")
        self.assertEqual(ctx["_token_budget"], 4000)
        self.assertTrue(any("unknown_role" in msg for msg in cm.output))

    def test_unknown_role_warning_message(self):
        ca = self._make_assembler()
        with self.assertLogs("context_assembler", level="WARNING") as cm:
            ca.assemble("proj", 1, "robot")
        self.assertTrue(
            any("ROLE_BUDGETS" in msg for msg in cm.output),
            msg=f"Expected 'ROLE_BUDGETS' in warning. Got: {cm.output}",
        )


class TestEnforceBudget(unittest.TestCase):
    """_enforce_budget trims low-priority sections first."""

    def _make_assembler(self):
        return ContextAssembler()

    def _big(self, n_tokens: int) -> list:
        """Return a list whose JSON serialisation ≈ n_tokens tokens."""
        # Each item is 4 chars → 1 token
        item = "aaaa"
        return [item] * n_tokens

    def test_no_trim_when_under_budget(self):
        ca = self._make_assembler()
        ctx = {
            "project_status": {"x": "y"},
            "conversation_history": ["msg1"],
            "_token_budget": 10000,
            "_tokens_used": 0,
        }
        result = ca._enforce_budget(ctx, 10000)
        self.assertIn("conversation_history", result)

    def test_conversation_trimmed_first(self):
        ca = self._make_assembler()
        # Build a context that is clearly over a tiny budget
        big_list = self._big(500)  # ~500 tokens worth
        ctx = {
            "project_status": {"x": "y"},
            "conversation_history": big_list,
            "memories": [{"content": "m"}],
            "_token_budget": 5,
            "_tokens_used": 0,
        }
        result = ca._enforce_budget(ctx, 5)
        # conversation_history should be removed when over budget
        conv = result.get("conversation_history", [])
        self.assertEqual(conv, [], "conversation_history must be emptied when over budget")
        # result must be significantly smaller than the original (>10x reduction)
        original_tokens = len(json.dumps(ctx, ensure_ascii=False)) // 4
        total_tokens = len(json.dumps(result, ensure_ascii=False)) // 4
        self.assertLess(total_tokens, original_tokens // 10)

    def test_protected_keys_kept(self):
        ca = self._make_assembler()
        big_list = self._big(500)
        ctx = {
            "project_status": {"important": "data"},
            "conversation_history": big_list,
            "_token_budget": 5,
            "_tokens_used": 0,
        }
        result = ca._enforce_budget(ctx, 5)
        self.assertIn("project_status", result)

    def test_memories_trimmed_after_conversation(self):
        ca = self._make_assembler()
        # conversation_history is empty; memories is large
        big_list = self._big(500)
        ctx = {
            "project_status": {"x": "y"},
            "memories": big_list,
            "_token_budget": 5,
            "_tokens_used": 0,
        }
        result = ca._enforce_budget(ctx, 5)
        mem = result.get("memories", [])
        self.assertEqual(mem, [], "memories must be emptied when over budget")
        # result must be significantly smaller than the original (>10x reduction)
        original_tokens = len(json.dumps(ctx, ensure_ascii=False)) // 4
        total_tokens = len(json.dumps(result, ensure_ascii=False)) // 4
        self.assertLess(total_tokens, original_tokens // 10)

    def test_tokens_used_updated_after_trim(self):
        ca = self._make_assembler()
        big_list = self._big(500)
        ctx = {
            "project_status": {"x": "y"},
            "conversation_history": big_list,
            "_token_budget": 20,
            "_tokens_used": 9999,
        }
        result = ca._enforce_budget(ctx, 20)
        # _tokens_used should reflect actual post-trim size
        expected = len(json.dumps(result, ensure_ascii=False)) // 4
        self.assertEqual(result["_tokens_used"], expected)


if __name__ == "__main__":
    unittest.main()
