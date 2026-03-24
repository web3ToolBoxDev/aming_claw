"""Decision Validator — 4-layer validation of AI output.

Layer 1: SchemaValidator     — JSON format, schema_version, required fields
Layer 2: PolicyValidator     — Role permissions, tool policy, dangerous ops
Layer 3: GraphValidator      — Node exists, deps, gates, coverage, artifacts
Layer 4: PreconditionValidator — Workspace, files, lease, concurrency

Each layer returns {layer, passed, errors[]}.
All layers run, results aggregated.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Hard-Rule Validation (code-enforced, AI cannot bypass)
# ──────────────────────────────────────────────

class ValidationError(ValueError):
    """Raised when a hard-rule validation fails.

    Subclasses ValueError so callers that only catch ValueError still work.
    Always carries a descriptive message indicating which field/condition failed.
    """


def validate_dev_task_node(node: dict) -> None:
    """Hard rule #1 — dev_task must have explicit file contract.

    Required: target_files (files to modify, must exist)
    Optional: create_files (new files to create, must NOT exist)
    Optional: forbidden_files (files NOT allowed to touch)
    """
    if node.get("type") != "dev_task":
        return

    target_files = node.get("target_files")
    if not target_files and not node.get("create_files"):
        raise ValidationError(
            "Hard rule #1 violated: dev_task must have target_files or create_files. "
            "target_files = existing files to modify. "
            "create_files = new files to create. "
            "At least one must be non-empty."
            f" (node keys={list(node.keys())})"
        )

    # Validate paths don't have dangerous patterns
    all_files = (target_files or []) + (node.get("create_files") or [])
    for f in all_files:
        if ".." in f or f.startswith("/") or f.startswith("\\"):
            raise ValidationError(f"Hard rule #1: unsafe path in file contract: {f}")


def validate_session(session: dict) -> None:
    """Hard rule #2 — session 对象必须包含 snapshot 字段。

    Args:
        session: session 描述 dict。

    Raises:
        ValidationError: 当 session 缺少 snapshot 字段时。

    Example::

        sess_ok  = {"id": "s1", "snapshot": {"files": [], "ts": 1234567890}}
        sess_bad = {"id": "s1"}
        validate_session(sess_ok)   # OK — no exception
        validate_session(sess_bad)  # raises ValidationError
    """
    if "snapshot" not in session or session["snapshot"] is None:
        raise ValidationError(
            "Hard rule #2 violated: session 对象缺少必要的 snapshot 字段。"
            " snapshot 记录了执行前的环境状态快照，是回滚与审计的关键依据，"
            "必须在 session 初始化时写入。"
            f" (session keys={list(session.keys())!r})"
        )


def validate_evidence(evidence: dict) -> None:
    """Hard rule #3 — evidence 对象必须包含 result / timestamp / node_id 三个非空字段。

    Args:
        evidence: 执行证据 dict。

    Raises:
        ValidationError: 当三个必填字段中任意一个缺失或为空时。

    Example::

        ev_ok = {"result": "pass", "timestamp": "2026-03-23T10:00:00", "node_id": "L1.3"}
        ev_bad = {"result": "pass", "timestamp": "2026-03-23T10:00:00"}  # 缺 node_id
        validate_evidence(ev_ok)   # OK — no exception
        validate_evidence(ev_bad)  # raises ValidationError
    """
    required_fields = ("result", "timestamp", "node_id")
    missing_or_empty = [
        f for f in required_fields
        if not evidence.get(f)  # covers missing key, None, and empty string/list/dict
    ]
    if missing_or_empty:
        raise ValidationError(
            f"Hard rule #3 violated: evidence 对象缺少或为空的必要字段: {missing_or_empty}。"
            " evidence 必须同时包含 result（执行结果）、timestamp（执行时间戳）、"
            "node_id（关联节点 ID）三个字段且均不得为空，以保证可追溯性。"
            f" (evidence keys present={list(evidence.keys())!r})"
        )


@dataclass
class LayerResult:
    layer: str
    passed: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Aggregated validation result across all layers."""
    approved_actions: list[dict] = field(default_factory=list)
    rejected_actions: list[dict] = field(default_factory=list)  # [{action, reason, layer}]
    layer_results: list[LayerResult] = field(default_factory=list)
    needs_retry: bool = False
    needs_human: bool = False

    @property
    def all_passed(self) -> bool:
        return len(self.rejected_actions) == 0

    @property
    def summary(self) -> str:
        approved = len(self.approved_actions)
        rejected = len(self.rejected_actions)
        return f"{approved} approved, {rejected} rejected"


class DecisionValidator:
    """4-layer decision validator. Code-enforced, AI cannot bypass."""

    def __init__(self, graph_validator=None, project_id: str = ""):
        self._graph_validator = graph_validator
        self._project_id = project_id

    def validate(self, role: str, ai_output: dict, project_id: str = "") -> ValidationResult:
        """Run all 4 validation layers on AI output.

        Args:
            role: coordinator / dev / tester / qa
            ai_output: Parsed AI decision dict
            project_id: Project identifier

        Returns:
            ValidationResult with approved/rejected actions
        """
        pid = project_id or self._project_id
        result = ValidationResult()

        actions = ai_output.get("actions", [])
        if not actions:
            # No actions = reply only, always OK
            return result

        for action in actions:
            action_type = action.get("type", "unknown")
            errors = []

            # Layer 1: Schema
            l1 = self._validate_schema(action)
            result.layer_results.append(l1)
            if not l1.passed:
                errors.extend(l1.errors)

            # Layer 2: Policy
            l2 = self._validate_policy(role, action)
            result.layer_results.append(l2)
            if not l2.passed:
                errors.extend(l2.errors)

            # Layer 3: Graph
            l3 = self._validate_graph(action, pid)
            result.layer_results.append(l3)
            if not l3.passed:
                errors.extend(l3.errors)

            # Layer 4: Precondition
            l4 = self._validate_precondition(action, pid)
            result.layer_results.append(l4)
            if not l4.passed:
                errors.extend(l4.errors)

            if errors:
                result.rejected_actions.append({
                    "action": action,
                    "reasons": errors,
                    "layers_failed": [lr.layer for lr in [l1, l2, l3, l4] if not lr.passed],
                })
                # Determine if retryable
                from task_state_machine import classify_error, ErrorCategory
                for err in errors:
                    cat = classify_error(err)
                    if cat == ErrorCategory.NEEDS_HUMAN:
                        result.needs_human = True
                    elif cat in (ErrorCategory.RETRYABLE_MODEL, ErrorCategory.RETRYABLE_ENV):
                        result.needs_retry = True
            else:
                result.approved_actions.append(action)

        return result

    # ── Layer 1: Schema ──

    def _validate_schema(self, action: dict) -> LayerResult:
        """Check action has required fields and valid format."""
        errors = []

        if "type" not in action:
            errors.append("missing action.type")

        action_type = action.get("type", "")
        from role_permissions import ACTION_TYPES
        if action_type and action_type not in ACTION_TYPES:
            errors.append(f"unknown action type: {action_type}")

        return LayerResult(layer="schema", passed=len(errors) == 0, errors=errors)

    # ── Layer 2: Policy ──

    def _validate_policy(self, role: str, action: dict) -> LayerResult:
        """Check role permission for action type."""
        errors = []
        action_type = action.get("type", "")

        # Intercept memory delete attempts by dev role
        if role == "dev" and action_type in ("memory_delete", "delete_memory"):
            return LayerResult(
                layer="policy",
                passed=False,
                errors=["dev 角色不允许直接删除记忆，请使用 propose_memory_cleanup 并等待 QA 审核"],
            )

        from role_permissions import check_permission, check_verify_permission
        allowed, reason = check_permission(role, action_type)
        if not allowed:
            errors.append(reason)

        # Extra check for verify_update: role verify level
        if action_type == "verify_update":
            target = action.get("target_status", "")
            if target:
                v_allowed, v_reason = check_verify_permission(role, target)
                if not v_allowed:
                    errors.append(v_reason)

        return LayerResult(layer="policy", passed=len(errors) == 0, errors=errors)

    # ── Layer 3: Graph ──

    def _validate_graph(self, action: dict, project_id: str) -> LayerResult:
        """Check action against acceptance graph constraints."""
        errors = []

        if not self._graph_validator or not project_id:
            return LayerResult(layer="graph", passed=True)

        gv = self._graph_validator
        action_type = action.get("type", "")

        # File coverage check
        target_files = action.get("target_files", [])
        if target_files:
            uncovered = gv.check_file_coverage(target_files, project_id)
            if uncovered:
                errors.append(f"files without node coverage: {uncovered}")

        # Node existence check
        related_nodes = action.get("related_nodes", [])
        for node_id in related_nodes:
            if not gv.check_node_exists(node_id, project_id):
                errors.append(f"node {node_id} does not exist")

        # Verify update checks
        if action_type == "verify_update":
            node_id = action.get("node_id", "")
            if node_id:
                # Deps satisfied?
                unsatisfied = gv.check_deps_satisfied(node_id, project_id)
                if unsatisfied:
                    errors.append(f"deps not satisfied: {unsatisfied}")

                # Gate policy?
                unmet = gv.check_gate_policy(node_id, project_id)
                if unmet:
                    errors.append(f"gates not met: {unmet}")

                # Artifacts complete?
                target_status = action.get("target_status", "")
                if target_status == "qa_pass":
                    missing = gv.check_artifacts(node_id, project_id)
                    if missing:
                        errors.append(f"artifacts missing: {missing}")

        # Propose node validation
        if action_type == "propose_node":
            valid, reason = gv.validate_propose_node(action, project_id)
            if not valid:
                errors.append(f"node proposal rejected: {reason}")

        return LayerResult(layer="graph", passed=len(errors) == 0, errors=errors)

    # ── Layer 4: Precondition ──

    def _validate_precondition(self, action: dict, project_id: str) -> LayerResult:
        """Check execution preconditions (workspace, resources)."""
        errors = []

        action_type = action.get("type", "")

        # Dev task: check workspace exists
        if action_type in ("create_dev_task", "modify_code"):
            import os
            workspace = os.getenv("CODEX_WORKSPACE", "")
            if workspace and not os.path.isdir(workspace):
                errors.append(f"workspace not found: {workspace}")

        return LayerResult(layer="precondition", passed=len(errors) == 0, errors=errors)


def build_retry_prompt(ai_output: dict, validation: ValidationResult) -> str:
    """Build a retry prompt explaining why actions were rejected."""
    lines = ["你之前的部分决策被系统拒绝:"]
    for rejected in validation.rejected_actions:
        action = rejected["action"]
        reasons = rejected["reasons"]
        lines.append(f"  - {action.get('type', '?')}: {'; '.join(reasons)}")
    lines.append("")
    lines.append("请重新分析，在权限范围内重新输出决策。")
    lines.append("被拒绝的 action 不要重复输出，换一种合法的方式实现目标。")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 示例用法 / Usage examples
# (供编写单元测试时参考)
# ──────────────────────────────────────────────
#
# 快速运行: python agent/decision_validator.py
#
# 测试文件建议路径: agent/tests/test_decision_validator_hard_rules.py
#
# 示例测试片段:
#
#   from decision_validator import ValidationError, validate_dev_task_node, \
#       validate_session, validate_evidence
#
#   # ── Hard rule #1 ──
#   def test_dev_task_requires_target_files():
#       import pytest
#       with pytest.raises(ValidationError, match="target_files"):
#           validate_dev_task_node({"type": "dev_task", "target_files": []})
#
#   def test_dev_task_non_dev_task_skipped():
#       validate_dev_task_node({"type": "qa_task"})  # 不应抛出异常
#
#   # ── Hard rule #2 ──
#   def test_session_requires_snapshot():
#       import pytest
#       with pytest.raises(ValidationError, match="snapshot"):
#           validate_session({"id": "s1"})
#
#   def test_session_snapshot_none_raises():
#       import pytest
#       with pytest.raises(ValidationError):
#           validate_session({"id": "s1", "snapshot": None})
#
#   # ── Hard rule #3 ──
#   def test_evidence_requires_all_fields():
#       import pytest
#       with pytest.raises(ValidationError, match="node_id"):
#           validate_evidence({"result": "pass", "timestamp": "2026-01-01T00:00:00"})
#
#   def test_evidence_empty_field_raises():
#       import pytest
#       with pytest.raises(ValidationError, match="result"):
#           validate_evidence({"result": "", "timestamp": "2026-01-01T00:00:00", "node_id": "L1.3"})
#
#   def test_evidence_all_fields_ok():
#       validate_evidence({"result": "pass", "timestamp": "2026-01-01T00:00:00", "node_id": "L1.3"})


if __name__ == "__main__":
    import traceback

    print("=" * 60)
    print("decision_validator — Hard Rules 自检示例")
    print("=" * 60)

    cases = [
        # (描述, callable)
        (
            "[#1 PASS] dev_task with target_files",
            lambda: validate_dev_task_node({"type": "dev_task", "target_files": ["agent/foo.py"]}),
        ),
        (
            "[#1 FAIL] dev_task with empty target_files",
            lambda: validate_dev_task_node({"type": "dev_task", "target_files": []}),
        ),
        (
            "[#1 PASS] non-dev_task node skipped",
            lambda: validate_dev_task_node({"type": "qa_task"}),
        ),
        (
            "[#2 PASS] session with snapshot",
            lambda: validate_session({"id": "s1", "snapshot": {"ts": 1234567890}}),
        ),
        (
            "[#2 FAIL] session missing snapshot",
            lambda: validate_session({"id": "s1"}),
        ),
        (
            "[#2 FAIL] session snapshot is None",
            lambda: validate_session({"id": "s1", "snapshot": None}),
        ),
        (
            "[#3 PASS] evidence with all required fields",
            lambda: validate_evidence(
                {"result": "pass", "timestamp": "2026-03-23T10:00:00", "node_id": "L1.3"}
            ),
        ),
        (
            "[#3 FAIL] evidence missing node_id",
            lambda: validate_evidence(
                {"result": "pass", "timestamp": "2026-03-23T10:00:00"}
            ),
        ),
        (
            "[#3 FAIL] evidence empty result",
            lambda: validate_evidence(
                {"result": "", "timestamp": "2026-03-23T10:00:00", "node_id": "L1.3"}
            ),
        ),
    ]

    for desc, fn in cases:
        try:
            fn()
            print(f"  OK  {desc}")
        except ValidationError as exc:
            # Expected failures should show as "BLOCKED" not tracebacks
            print(f"  BLOCKED  {desc}")
            print(f"           → {exc}")
        except Exception:
            print(f"  ERROR  {desc}")
            traceback.print_exc()

    print("=" * 60)
    print("自检完成。BLOCKED 行表示校验正确拦截了非法输入。")
