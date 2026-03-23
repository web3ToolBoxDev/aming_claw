"""Context Assembler — Budget-aware context assembly for AI sessions.

Assembles role-specific context with token budget limits.
Ensures deterministic, stable context across sessions.
"""

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Token budget per role (approximate, 1 token ≈ 4 chars)
CONTEXT_BUDGET = {
    "pm": {
        "hard_context": 3000,      # project overview, node structure
        "conversation": 2000,      # user requirements discussion
        "memory": 2000,            # existing architecture, patterns
        "runtime": 500,            # current state
        "total_max": 7500,
    },
    "coordinator": {
        "hard_context": 3000,      # task, node, files, status
        "conversation": 3000,      # recent messages
        "memory": 1500,            # top-3 related
        "runtime": 500,            # active/queued tasks
        "total_max": 8000,
    },
    "dev": {
        "hard_context": 2000,      # task prompt, target files
        "memory": 1500,            # related pitfalls
        "git_context": 500,        # git status
        "total_max": 4000,
    },
    "tester": {
        "hard_context": 1500,      # test command, affected nodes
        "memory": 1000,            # test patterns
        "total_max": 3000,
    },
    "qa": {
        "hard_context": 1500,      # review scope
        "memory": 1000,            # qa criteria
        "total_max": 3000,
    },
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 chars for mixed CJK/English."""
    return len(text) // 3


def _truncate_to_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit token budget."""
    max_chars = max_tokens * 3
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(truncated)"


class ContextAssembler:
    """Assembles context for AI sessions with budget limits."""

    def __init__(self, governance_url: str = "", token: str = "",
                 dbservice_url: str = ""):
        self._gov_url = governance_url or os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        self._token = token or os.getenv("GOV_COORDINATOR_TOKEN", "")
        self._db_url = dbservice_url or os.getenv("DBSERVICE_URL", "http://localhost:40002")

    def assemble(self, project_id: str, chat_id: int, role: str,
                 prompt: str = "", extra: dict = None) -> dict:
        """Assemble context for an AI session.

        Args:
            project_id: Project identifier
            chat_id: Telegram chat ID (for conversation history)
            role: coordinator / dev / tester / qa
            prompt: The user message or task prompt
            extra: Additional context (e.g., dev_result for eval)

        Returns:
            Context dict ready for injection into system prompt
        """
        budget = CONTEXT_BUDGET.get(role, CONTEXT_BUDGET["coordinator"])
        context = {}
        used_tokens = 0

        # Layer 1: Hard context (always included)
        hard = self._fetch_hard_context(project_id, role, prompt, extra)
        hard_str = json.dumps(hard, ensure_ascii=False)
        hard_truncated = _truncate_to_budget(hard_str, budget.get("hard_context", 2000))
        context["project_status"] = hard
        used_tokens += _estimate_tokens(hard_truncated)

        # Layer 2: Conversation history (coordinator only)
        if "conversation" in budget and budget["conversation"] > 0:
            conv = self._fetch_conversation(project_id, chat_id)
            conv_str = json.dumps(conv, ensure_ascii=False)
            conv_truncated = _truncate_to_budget(conv_str, budget["conversation"])
            context["conversation_history"] = conv
            used_tokens += _estimate_tokens(conv_truncated)

        # Layer 3: Memory (related knowledge)
        if "memory" in budget and budget["memory"] > 0:
            remaining = budget["total_max"] - used_tokens
            mem_budget = min(budget["memory"], remaining)
            if mem_budget > 0:
                memories = self._fetch_memories(project_id, prompt, role, mem_budget)
                context["memories"] = memories
                used_tokens += _estimate_tokens(json.dumps(memories, ensure_ascii=False))

        # Layer 4: Runtime (active tasks)
        if "runtime" in budget and budget["runtime"] > 0:
            remaining = budget["total_max"] - used_tokens
            if remaining > 0:
                runtime = self._fetch_runtime(project_id)
                context["runtime"] = runtime
                used_tokens += _estimate_tokens(json.dumps(runtime, ensure_ascii=False))

        # Layer 5: Git context (dev only)
        if "git_context" in budget and budget["git_context"] > 0:
            remaining = budget["total_max"] - used_tokens
            if remaining > 0:
                git = self._fetch_git_context()
                context["git_status"] = git

        context["_token_budget"] = budget["total_max"]
        context["_tokens_used"] = used_tokens

        return context

    def _fetch_hard_context(self, project_id: str, role: str,
                            prompt: str, extra: dict = None) -> dict:
        """Fetch project status and node summary."""
        result = {}
        try:
            import requests
            headers = {"X-Gov-Token": self._token}
            resp = requests.get(f"{self._gov_url}/api/wf/{project_id}/summary",
                              headers=headers, timeout=5)
            summary = resp.json()
            result["total_nodes"] = summary.get("total_nodes", 0)
            result["by_status"] = summary.get("by_status", {})
        except Exception:
            result["status_error"] = "governance unavailable"

        if extra:
            result["extra"] = extra

        return result

    def _fetch_conversation(self, project_id: str, chat_id: int) -> list:
        """Fetch recent conversation history from session context."""
        try:
            import requests
            headers = {"X-Gov-Token": self._token}
            resp = requests.get(f"{self._gov_url}/api/context/{project_id}/load",
                              headers=headers, timeout=5)
            ctx = resp.json().get("context", {})
            messages = ctx.get("recent_messages", [])
            return messages[-10:]  # Last 10 messages
        except Exception:
            return []

    def _fetch_memories(self, project_id: str, query: str,
                        role: str, budget: int) -> list:
        """Fetch related memories from dbservice."""
        try:
            import requests
            resp = requests.post(f"{self._db_url}/knowledge/search",
                json={"query": query[:100], "scope": project_id, "limit": 3},
                timeout=3)
            results = resp.json().get("results", [])
            memories = []
            tokens_used = 0
            for r in results:
                content = r["doc"]["content"][:200]
                tokens_used += _estimate_tokens(content)
                if tokens_used > budget:
                    break
                memories.append({
                    "type": r["doc"].get("type", ""),
                    "content": content,
                })
            return memories
        except Exception:
            return []

    def _fetch_runtime(self, project_id: str) -> dict:
        """Fetch runtime status (active/queued tasks)."""
        try:
            import requests
            headers = {"X-Gov-Token": self._token}
            resp = requests.get(f"{self._gov_url}/api/runtime/{project_id}",
                              headers=headers, timeout=5)
            data = resp.json()
            return {
                "active_count": len(data.get("active_tasks", [])),
                "queued_count": len(data.get("queued_tasks", [])),
            }
        except Exception:
            return {}

    def _fetch_git_context(self) -> dict:
        """Fetch git status for dev context."""
        import subprocess
        workspace = os.getenv("CODEX_WORKSPACE", os.getcwd())
        try:
            status = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True, cwd=workspace, timeout=10
            ).stdout.strip()
            return {"status": status[:500]}
        except Exception:
            return {}
