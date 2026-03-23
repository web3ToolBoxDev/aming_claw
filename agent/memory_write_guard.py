"""Memory Write Guard — Prevents memory pollution.

Checks before writing to long-term memory:
  1. Dedup (similarity > 0.85 → skip)
  2. Confidence threshold (< 0.6 → skip)
  3. Source validation (decision needs qa_pass node)
  4. TTL enforcement (workaround → 30 days)
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class MemoryWriteGuard:
    """Guards long-term memory from pollution."""

    def __init__(self, dbservice_url: str = "", governance_url: str = "", token: str = ""):
        self._db_url = dbservice_url or os.getenv("DBSERVICE_URL", "http://localhost:40002")
        self._gov_url = governance_url or os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        self._token = token or os.getenv("GOV_COORDINATOR_TOKEN", "")

    def should_write(self, entry: dict, project_id: str) -> tuple[bool, str]:
        """Check if memory entry should be written.

        Returns:
            (should_write: bool, reason: str)
        """
        content = entry.get("content", "")
        entry_type = entry.get("type", "")

        if not content or len(content.strip()) < 10:
            return False, "content too short"

        # 1. Dedup — check for similar existing entries
        if self._is_duplicate(content, project_id):
            return False, "duplicate (similarity > 0.85)"

        # 2. Confidence threshold
        confidence = entry.get("confidence", 1.0)
        if confidence < 0.6:
            return False, f"low confidence ({confidence})"

        # 3. Source validation — decisions need qa_pass backing
        if entry_type == "decision":
            related_node = entry.get("related_node", "")
            if related_node and not self._node_is_qa_pass(related_node, project_id):
                return False, f"decision requires qa_pass node (node {related_node} not qa_pass)"

        # 4. TTL enforcement
        if entry_type == "workaround":
            entry.setdefault("ttl_days", 30)

        if entry_type == "session_summary":
            entry.setdefault("ttl_days", 90)

        return True, "ok"

    def guarded_write(self, entry: dict, project_id: str) -> dict:
        """Write with guard check. Returns result dict."""
        should, reason = self.should_write(entry, project_id)
        if not should:
            log.info("Memory write blocked: %s (content: %s...)",
                     reason, entry.get("content", "")[:50])
            return {"written": False, "reason": reason}

        # Write to dbservice
        try:
            import requests
            entry.setdefault("scope", project_id)
            resp = requests.post(
                f"{self._db_url}/knowledge/upsert",
                json=entry, timeout=5
            )
            result = resp.json()
            if result.get("success"):
                log.info("Memory written: %s", entry.get("refId", ""))
                return {"written": True, "refId": entry.get("refId", "")}
            else:
                return {"written": False, "reason": result.get("error", "unknown")}
        except Exception as e:
            return {"written": False, "reason": str(e)[:200]}

    def _is_duplicate(self, content: str, project_id: str) -> bool:
        """Check if similar content already exists."""
        try:
            import requests
            resp = requests.post(
                f"{self._db_url}/knowledge/search",
                json={"query": content[:100], "scope": project_id, "limit": 3},
                timeout=3
            )
            results = resp.json().get("results", [])
            for r in results:
                existing = r["doc"]["content"]
                sim = self._simple_similarity(content, existing)
                if sim > 0.85:
                    return True
            return False
        except Exception:
            return False  # If can't check, allow write

    def _node_is_qa_pass(self, node_id: str, project_id: str) -> bool:
        """Check if node is qa_pass."""
        try:
            import requests
            resp = requests.get(
                f"{self._gov_url}/api/wf/{project_id}/node/{node_id}",
                headers={"X-Gov-Token": self._token}, timeout=5
            )
            data = resp.json()
            return data.get("verify_status") == "qa_pass"
        except Exception:
            return True  # If can't check, allow

    def _simple_similarity(self, a: str, b: str) -> float:
        """Simple character-level similarity (Jaccard on trigrams)."""
        if not a or not b:
            return 0.0

        def trigrams(s):
            s = s.lower().strip()
            return set(s[i:i+3] for i in range(len(s)-2))

        ta, tb = trigrams(a), trigrams(b)
        if not ta or not tb:
            return 0.0

        intersection = len(ta & tb)
        union = len(ta | tb)
        return intersection / union if union > 0 else 0.0
