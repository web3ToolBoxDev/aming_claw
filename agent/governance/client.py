"""GovernanceClient SDK — HTTP client with bounded retry and degradation.

Strict mode (verify-update, release-gate): bounded retry + deadline
Lenient mode (mem/write, audit): local cache + flush on recovery
"""

import json
import time
import logging
import requests

log = logging.getLogger(__name__)


class GovernanceClient:
    """HTTP client for the governance service."""

    def __init__(
        self,
        base_url: str = "http://localhost:40000",
        token: str = "",
        max_retries: int = 5,
        base_delay: float = 2.0,
        deadline_sec: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.deadline_sec = deadline_sec
        self._offline_queue: list[tuple] = []

    def _headers(self, idem_key: str = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Gov-Token"] = self.token
        if idem_key:
            h["Idempotency-Key"] = idem_key
        return h

    def _post(self, path: str, data: dict, idem_key: str = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.post(url, json=data, headers=self._headers(idem_key), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.get(url, params=params, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    # --- Strict Mode: bounded retry ---

    def _strict_post(self, path: str, data: dict, idem_key: str = None) -> dict:
        """Bounded retry with exponential backoff and deadline."""
        deadline = time.time() + self.deadline_sec
        last_error = None

        for attempt in range(self.max_retries):
            if time.time() > deadline:
                break
            try:
                return self._post(path, data, idem_key)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                delay = min(self.base_delay * (2 ** attempt), max(0, deadline - time.time()))
                if delay > 0:
                    time.sleep(delay)

        return {
            "status": "governance_unavailable",
            "action": "escalate_to_coordinator",
            "retries_exhausted": True,
            "error": str(last_error),
        }

    # --- Lenient Mode: cache locally ---

    def _lenient_post(self, path: str, data: dict) -> dict:
        try:
            return self._post(path, data)
        except (requests.ConnectionError, requests.Timeout):
            self._offline_queue.append((path, data))
            return {"status": "cached_locally", "queue_size": len(self._offline_queue)}

    def flush_offline_queue(self) -> int:
        """Flush cached operations. Returns count of successfully flushed."""
        flushed = 0
        while self._offline_queue:
            path, data = self._offline_queue[0]
            try:
                self._post(path, data)
                self._offline_queue.pop(0)
                flushed += 1
            except (requests.ConnectionError, requests.Timeout):
                break
        return flushed

    # --- Public API ---

    def verify_update(self, project_id: str, nodes: list, status: str, evidence: dict, idem_key: str = None) -> dict:
        return self._strict_post(
            f"/api/wf/{project_id}/verify-update",
            {"nodes": nodes, "status": status, "evidence": evidence},
            idem_key,
        )

    def release_gate(self, project_id: str, scope: list = None, profile: str = None) -> dict:
        return self._strict_post(
            f"/api/wf/{project_id}/release-gate",
            {"scope": scope, "profile": profile},
        )

    def get_summary(self, project_id: str) -> dict:
        return self._get(f"/api/wf/{project_id}/summary")

    def get_node(self, project_id: str, node_id: str) -> dict:
        return self._get(f"/api/wf/{project_id}/node/{node_id}")

    def impact_analysis(self, project_id: str, files: list) -> dict:
        return self._get(f"/api/wf/{project_id}/impact", {"files": ",".join(files)})

    def mem_write(self, project_id: str, module: str, kind: str, content: str, **kwargs) -> dict:
        return self._lenient_post(
            f"/api/mem/{project_id}/write",
            {"module_id": module, "kind": kind, "content": content, **kwargs},
        )

    def mem_query(self, project_id: str, module: str = None) -> dict:
        params = {}
        if module:
            params["module"] = module
        return self._get(f"/api/mem/{project_id}/query", params)

    def register_role(self, principal_id: str, project_id: str, role: str, scope: list = None) -> dict:
        result = self._post(
            "/api/role/register",
            {"principal_id": principal_id, "project_id": project_id, "role": role, "scope": scope or []},
        )
        if "token" in result:
            self.token = result["token"]
        return result

    def heartbeat(self, status: str = "idle") -> dict:
        return self._post("/api/role/heartbeat", {"status": status})

    # --- Session Persistence ---

    def connect_from_env(self, role: str = "", project_id: str = "") -> dict:
        """Connect using token from environment variable.

        Trust chain: human sets GOV_{ROLE}_TOKEN in .env → agent reads at startup.

        Returns: {token, session, connected, team_status}
        """
        from .session_persistence import connect_from_env, check_team_status

        result = connect_from_env(
            governance_url=self.base_url,
            role=role,
            project_id=project_id,
        )

        if result.get("connected"):
            self.token = result["token"]
            team = check_team_status(project_id, self.token, self.base_url)
            result["team_status"] = team
        elif result.get("token"):
            # Offline mode: have token but service unreachable
            self.token = result["token"]

        return result
