"""Task State Machine — Explicit lifecycle states and transitions.

Code-enforced. Every state change goes through validate_transition().
"""

from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    # Creation
    CREATED = "created"
    QUEUED = "queued"

    # Execution
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    WAITING_HUMAN = "waiting_human"
    BLOCKED_BY_DEP = "blocked_by_dep"

    # Terminal
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"

    # Evaluation
    EVAL_PENDING = "eval_pending"
    EVAL_APPROVED = "eval_approved"
    EVAL_REJECTED = "eval_rejected"

    # Notification
    NOTIFY_PENDING = "notify_pending"
    NOTIFIED = "notified"

    # Archive
    ARCHIVED = "archived"


# Valid state transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "created":           {"queued", "cancelled"},
    "queued":            {"claimed", "cancelled", "blocked_by_dep"},
    "claimed":           {"running", "queued"},
    "running":           {"succeeded", "failed_retryable", "failed_terminal", "cancelled"},
    "waiting_retry":     {"queued"},
    "waiting_human":     {"queued", "cancelled"},
    "blocked_by_dep":    {"queued"},
    "succeeded":         {"eval_pending", "notify_pending"},
    "failed_retryable":  {"waiting_retry", "failed_terminal"},
    "failed_terminal":   {"notify_pending", "archived"},
    "eval_pending":      {"eval_approved", "eval_rejected"},
    "eval_approved":     {"notify_pending"},
    "eval_rejected":     {"queued"},
    "notify_pending":    {"notified"},
    "notified":          {"archived"},
    "cancelled":         {"archived"},
}

# Terminal states — no further transitions allowed
TERMINAL_STATES = {"archived"}

# States that indicate task is "done" (no more execution needed)
COMPLETION_STATES = {"succeeded", "failed_terminal", "cancelled",
                     "eval_approved", "eval_rejected", "notified", "archived"}

# States that allow retry
RETRYABLE_STATES = {"failed_retryable", "eval_rejected"}


class ErrorCategory(str, Enum):
    """Error classification for retry strategy."""
    RETRYABLE_MODEL = "retryable_model"       # JSON parse error, format issue
    RETRYABLE_ENV = "retryable_env"           # Network timeout, file system error
    BLOCKED_BY_DEP = "blocked_by_dep"         # Graph dependency not met
    NON_RETRYABLE_POLICY = "non_retryable"    # Permission denied, command deny
    NEEDS_HUMAN = "needs_human"               # Sensitive operation needs confirmation


RETRY_STRATEGY = {
    "retryable_model":      {"max_retries": 3, "backoff": "immediate",    "action": "rebuild_prompt"},
    "retryable_env":        {"max_retries": 2, "backoff": "exponential",  "action": "retry_same"},
    "blocked_by_dep":       {"max_retries": 0, "backoff": None,           "action": "set_blocked_status"},
    "non_retryable":        {"max_retries": 0, "backoff": None,           "action": "fail_terminal"},
    "needs_human":          {"max_retries": 0, "backoff": None,           "action": "create_approval"},
}


def validate_transition(current: str, target: str) -> tuple[bool, str]:
    """Check if state transition is valid.

    Returns:
        (valid: bool, reason: str)
    """
    if current == target:
        return False, f"no-op transition: {current} → {target}"

    if current in TERMINAL_STATES:
        return False, f"cannot transition from terminal state: {current}"

    allowed = VALID_TRANSITIONS.get(current, set())
    if target in allowed:
        return True, "ok"

    return False, f"invalid transition: {current} → {target}. Allowed: {allowed}"


def classify_error(error: str, context: dict = None) -> ErrorCategory:
    """Classify an error for retry strategy."""
    error_lower = error.lower()

    # JSON / format errors → retryable
    if any(kw in error_lower for kw in ["json", "parse", "format", "schema", "decode"]):
        return ErrorCategory.RETRYABLE_MODEL

    # Network / timeout → retryable
    if any(kw in error_lower for kw in ["timeout", "connection", "network", "refused"]):
        return ErrorCategory.RETRYABLE_ENV

    # Dependency / graph → blocked
    if any(kw in error_lower for kw in ["dependency", "dep", "gate", "blocked"]):
        return ErrorCategory.BLOCKED_BY_DEP

    # Permission / policy → terminal
    if any(kw in error_lower for kw in ["permission", "denied", "unauthorized", "forbidden"]):
        return ErrorCategory.NON_RETRYABLE_POLICY

    # Sensitive / dangerous → human
    if any(kw in error_lower for kw in ["confirm", "approval", "sensitive", "dangerous"]):
        return ErrorCategory.NEEDS_HUMAN

    # Default: retryable env
    return ErrorCategory.RETRYABLE_ENV


def get_retry_strategy(error_category: ErrorCategory) -> dict:
    """Get retry strategy for error category."""
    return RETRY_STRATEGY.get(error_category.value, RETRY_STRATEGY["retryable_env"])
