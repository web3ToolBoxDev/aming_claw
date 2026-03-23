"""Approval Manager — Human-in-the-loop approval for sensitive operations.

Creates approval objects that are sent to Telegram for confirmation.
Tracks approval_id, requested_action, risk_reason, expiry, approved_by.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Approval:
    """A pending human approval."""
    approval_id: str
    project_id: str
    chat_id: int
    requested_action: dict       # The action that needs approval
    risk_reason: str
    created_at: float
    expires_at: float            # Auto-deny after expiry
    status: str = "pending"      # pending / approved / denied / expired
    approved_by: str = ""
    approved_scope: str = ""     # "once" or "session"
    decided_at: float = 0


class ApprovalManager:
    """Manages human approval flow for sensitive operations."""

    def __init__(self):
        self._pending: dict[str, Approval] = {}
        self._default_ttl = 300  # 5 minutes

    def create_approval(self, project_id: str, chat_id: int,
                        action: dict, risk_reason: str) -> Approval:
        """Create a pending approval and notify user via Telegram."""
        approval_id = f"approve-{uuid.uuid4().hex[:8]}"
        now = time.time()

        approval = Approval(
            approval_id=approval_id,
            project_id=project_id,
            chat_id=chat_id,
            requested_action=action,
            risk_reason=risk_reason,
            created_at=now,
            expires_at=now + self._default_ttl,
        )

        self._pending[approval_id] = approval

        # Send approval request to Telegram
        self._notify_approval_request(approval)

        log.info("Approval created: %s (action=%s, risk=%s)",
                 approval_id, action.get("type", "?"), risk_reason)

        return approval

    def approve(self, approval_id: str, approved_by: str = "human",
                scope: str = "once") -> Optional[Approval]:
        """Approve a pending request."""
        approval = self._pending.get(approval_id)
        if not approval:
            return None
        if approval.status != "pending":
            return approval

        # Check expiry
        if time.time() > approval.expires_at:
            approval.status = "expired"
            return approval

        approval.status = "approved"
        approval.approved_by = approved_by
        approval.approved_scope = scope
        approval.decided_at = time.time()

        log.info("Approval approved: %s by %s", approval_id, approved_by)
        return approval

    def deny(self, approval_id: str, reason: str = "") -> Optional[Approval]:
        """Deny a pending request."""
        approval = self._pending.get(approval_id)
        if not approval:
            return None
        if approval.status != "pending":
            return approval

        approval.status = "denied"
        approval.decided_at = time.time()

        log.info("Approval denied: %s", approval_id)
        return approval

    def check_expired(self) -> list[str]:
        """Check and expire old approvals. Returns expired IDs."""
        now = time.time()
        expired = []
        for aid, approval in self._pending.items():
            if approval.status == "pending" and now > approval.expires_at:
                approval.status = "expired"
                expired.append(aid)
        return expired

    def get_pending(self, project_id: str = "") -> list[Approval]:
        """Get all pending approvals."""
        result = []
        for approval in self._pending.values():
            if approval.status == "pending":
                if not project_id or approval.project_id == project_id:
                    result.append(approval)
        return result

    def _notify_approval_request(self, approval: Approval) -> None:
        """Send approval request to Telegram via Gateway."""
        action = approval.requested_action
        text = (
            f"[需要确认] {approval.risk_reason}\n\n"
            f"操作: {action.get('type', '?')}\n"
            f"详情: {action.get('prompt', '')[:200]}\n"
            f"审批ID: {approval.approval_id}\n"
            f"有效期: 5分钟\n\n"
            f"回复 '确认 {approval.approval_id}' 批准\n"
            f"回复 '拒绝 {approval.approval_id}' 拒绝"
        )

        try:
            import requests
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            token = os.getenv("GOV_COORDINATOR_TOKEN", "")
            requests.post(
                f"{gov_url}/gateway/reply",
                headers={"Content-Type": "application/json", "X-Gov-Token": token},
                json={"chat_id": approval.chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            log.exception("Failed to send approval notification")


# Global singleton
_manager: Optional[ApprovalManager] = None


def get_approval_manager() -> ApprovalManager:
    global _manager
    if _manager is None:
        _manager = ApprovalManager()
    return _manager
