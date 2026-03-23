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
