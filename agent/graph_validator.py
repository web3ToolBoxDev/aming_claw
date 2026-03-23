"""Graph-Aware Validator — Executor enforces acceptance graph constraints.

Pulls graph snapshot from Governance, caches with version CAS.
All checks are code-enforced, AI cannot bypass.
"""

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Status ordering for comparison
STATUS_ORDER = {
    "pending": 0, "testing": 1, "t2_pass": 2, "qa_pass": 3,
    "failed": -1, "waived": 2, "skipped": -1,
}


class GraphValidator:
    """Validates actions against the acceptance graph. Code-enforced."""

    def __init__(self, governance_url: str = "", token: str = ""):
        self._gov_url = governance_url or os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        self._token = token or os.getenv("GOV_COORDINATOR_TOKEN", "")
        self._cache: Optional[dict] = None
        self._cache_version: int = 0
        self._cache_ts: float = 0
        self._cache_ttl: int = 60

    def _gov_api(self, path: str) -> dict:
        import requests
        try:
            headers = {"X-Gov-Token": self._token}
            resp = requests.get(f"{self._gov_url}{path}", headers=headers, timeout=5)
            return resp.json()
        except Exception:
            return {}

    def _get_graph(self, project_id: str) -> dict:
        """Get graph with TTL cache."""
        now = time.time()
        if self._cache and now - self._cache_ts < self._cache_ttl:
            return self._cache

        result = self._gov_api(f"/api/wf/{project_id}/export?format=json")
        self._cache = result
        self._cache_version = result.get("version", 0)
        self._cache_ts = now
        return result

    def invalidate_cache(self):
        """Force cache refresh on next access."""
        self._cache = None

    # ── Constraint checks ──

    def check_file_coverage(self, changed_files: list[str], project_id: str) -> list[str]:
        """Check all changed files have node coverage. Returns uncovered files."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            nodes = {n.get("id", ""): n for n in nodes}

        covered = set()
        for node in nodes.values():
            for f in node.get("primary", []):
                covered.add(f)
            for f in node.get("secondary", []):
                covered.add(f)

        uncovered = []
        for f in changed_files:
            matched = any(
                f.endswith(cf) or cf.endswith(f) or f == cf
                for cf in covered
            )
            if not matched:
                uncovered.append(f)
        return uncovered

    def check_deps_satisfied(self, node_id: str, project_id: str) -> list[str]:
        """Check all upstream deps are pass. Returns unsatisfied deps."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            nodes = {n.get("id", ""): n for n in nodes}

        node = nodes.get(node_id)
        if not node:
            return [f"{node_id}: not found"]

        unsatisfied = []
        for dep in node.get("deps", []):
            dep_node = nodes.get(dep)
            if not dep_node:
                unsatisfied.append(f"{dep}: not found")
            elif dep_node.get("verify_status") not in ("t2_pass", "qa_pass"):
                unsatisfied.append(f"{dep}: {dep_node.get('verify_status', 'unknown')}")
        return unsatisfied

    def check_gate_policy(self, node_id: str, project_id: str) -> list[str]:
        """Check gate requirements. Returns unmet gates."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            nodes = {n.get("id", ""): n for n in nodes}

        node = nodes.get(node_id)
        if not node:
            return [f"{node_id}: not found"]

        unmet = []
        for gate in node.get("gates", []):
            gate_node_id = gate.get("node", "")
            min_status = gate.get("min_status", "qa_pass")
            gate_node = nodes.get(gate_node_id)
            if not gate_node:
                unmet.append(f"{gate_node_id}: not found")
            elif not self._status_gte(gate_node.get("verify_status", "pending"), min_status):
                unmet.append(f"{gate_node_id}: need {min_status}, got {gate_node.get('verify_status')}")
        return unmet

    def check_artifacts(self, node_id: str, project_id: str) -> list[str]:
        """Check artifact requirements. Returns missing artifacts."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            nodes = {n.get("id", ""): n for n in nodes}

        node = nodes.get(node_id)
        if not node:
            return []

        missing = []
        for artifact in node.get("artifacts", []):
            if artifact.get("type") == "api_docs":
                section = artifact.get("section", "")
                docs = self._gov_api(f"/api/docs/{section}")
                if "error" in docs:
                    missing.append(f"api_docs:{section}")
        return missing

    def check_node_exists(self, node_id: str, project_id: str) -> bool:
        """Check if node exists in graph."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            return any(n.get("id") == node_id for n in nodes)
        return node_id in nodes

    def check_node_allows_modification(self, node_id: str, project_id: str) -> tuple[bool, str]:
        """Check if node's status allows file modification (pending/testing only)."""
        graph = self._get_graph(project_id)
        nodes = graph.get("nodes", {})
        if isinstance(nodes, list):
            nodes = {n.get("id", ""): n for n in nodes}

        node = nodes.get(node_id)
        if not node:
            return True, "node not found, allowing"

        status = node.get("verify_status", "pending")
        if status in ("qa_pass",):
            return False, f"node {node_id} is {status}, cannot modify"
        return True, "ok"

    def validate_propose_node(self, proposal: dict, project_id: str) -> tuple[bool, str]:
        """Validate a node creation proposal."""
        import re
        node = proposal.get("node", {})
        node_id = node.get("id", "")

        # ID format
        if not re.match(r"^L\d+\.\d+$", node_id):
            return False, f"ID format invalid: {node_id}"

        # Uniqueness
        if self.check_node_exists(node_id, project_id):
            return False, f"node {node_id} already exists"

        # Deps exist
        for dep in node.get("deps", []):
            if not self.check_node_exists(dep, project_id):
                return False, f"dep {dep} does not exist"

        # Path safety
        for f in node.get("primary", []):
            if ".." in f or f.startswith("/"):
                return False, f"unsafe path: {f}"

        return True, "ok"

    def get_version(self, project_id: str) -> int:
        """Get current graph version for CAS."""
        self._get_graph(project_id)
        return self._cache_version

    def check_version_unchanged(self, project_id: str, expected_version: int) -> bool:
        """CAS: check graph hasn't changed since validation."""
        summary = self._gov_api(f"/api/wf/{project_id}/summary")
        current = summary.get("version", 0)
        if current != expected_version:
            self.invalidate_cache()
            return False
        return True

    def _status_gte(self, actual: str, required: str) -> bool:
        """Check if actual status >= required status."""
        return STATUS_ORDER.get(actual, -1) >= STATUS_ORDER.get(required, 0)
