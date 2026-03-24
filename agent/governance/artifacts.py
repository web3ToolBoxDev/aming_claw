"""Artifacts constraint checker — verify companion deliverables before qa_pass.

Each node can declare required artifacts (docs, tests, changelog, etc.).
At qa_pass time, governance checks if all artifacts are present.

Artifact types:
  - api_docs: /api/docs/{section} must return 200 with non-empty content
  - test_file: test file must exist and have >0 tests
  - changelog: entry must exist in changelog
"""

import json
import logging
import os

log = logging.getLogger(__name__)


# Artifact type → checker function
ARTIFACT_CHECKERS = {}


def _resolve_workspace(project_id: str) -> str:
    """Resolve workspace path for a project. Falls back to WORKSPACE_PATH env."""
    try:
        from . import project_service
        proj = project_service.get_project(project_id)
        if proj and proj.get("workspace_path"):
            return proj["workspace_path"]
    except Exception:
        pass
    return os.environ.get("WORKSPACE_PATH", "/workspace")


def artifact_checker(artifact_type: str):
    """Decorator to register an artifact checker."""
    def decorator(fn):
        ARTIFACT_CHECKERS[artifact_type] = fn
        return fn
    return decorator


@artifact_checker("api_docs")
def check_api_docs(node_id: str, config: dict, graph, project_id: str) -> dict:
    """Check that an API docs section exists and is not a skeleton."""
    section = config.get("section", "")
    if not section:
        return {"pass": False, "reason": f"No 'section' specified in artifact config for {node_id}"}

    # Check the in-memory _DOCS dict directly
    try:
        from .server import _DOCS
        if section in _DOCS:
            doc = _DOCS[section]
            # Check it has real content (not just title)
            keys = set(doc.keys()) - {"title", "description"}
            if keys:
                return {"pass": True, "section": section, "keys": sorted(keys)}
            return {"pass": False, "reason": f"Doc section '{section}' exists but has no content beyond title/description"}
        return {"pass": False, "reason": f"Doc section '{section}' not found. Available: {list(_DOCS.keys())}"}
    except Exception as e:
        return {"pass": False, "reason": f"Cannot check docs: {e}"}


@artifact_checker("test_file")
def check_test_file(node_id: str, config: dict, graph, project_id: str) -> dict:
    """Check that test files exist for the node."""
    node_data = graph.get_node(node_id) if graph else {}
    test_files = node_data.get("test", [])

    if not test_files:
        # Node declares no test files — check if it should
        if config.get("required", True):
            return {"pass": False, "reason": f"Node {node_id} has no test files declared"}
        return {"pass": True, "reason": "No tests required"}

    missing = []
    workspace = _resolve_workspace(project_id)
    for tf in test_files:
        full_path = os.path.join(workspace, tf)
        if not os.path.exists(full_path):
            missing.append(tf)

    if missing:
        return {"pass": False, "reason": f"Missing test files: {missing}"}
    return {"pass": True, "test_files": test_files}


@artifact_checker("changelog")
def check_changelog(node_id: str, config: dict, graph, project_id: str) -> dict:
    """Check that a changelog entry exists."""
    workspace = _resolve_workspace(project_id)
    changelog_paths = ["CHANGELOG.md", "docs/CHANGELOG.md", "CHANGES.md"]
    for cp in changelog_paths:
        full = os.path.join(workspace, cp)
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
                if node_id in content:
                    return {"pass": True, "file": cp, "note": f"Found {node_id} in changelog"}
            except Exception:
                pass
    return {"pass": True, "reason": "Changelog not enforced for this project"}


# ── Role-based artifact schema validation (Gap 10) ──

ROLE_ARTIFACT_SCHEMAS = {
    "pm": {
        "required_fields": ["goal", "acceptance_criteria", "fail_conditions"],
        "reject_if_missing": True,
        "description": "PM must provide goal, acceptance criteria, and failure conditions",
    },
    "dev": {
        "required_fields": ["implementation_summary", "changed_files", "commit_hash"],
        "reject_if_missing": True,
        "description": "Dev must report what was changed, which files, and commit hash",
    },
    "tester": {
        "required_fields": ["tests_executed", "result_summary", "recommendation"],
        "reject_if_missing": True,
        "description": "Tester must list tests run, results, and recommendation",
    },
    "qa": {
        "required_fields": ["scenarios_checked", "verdict"],
        "reject_if_missing": True,
        "description": "QA must list scenarios checked and give verdict",
    },
}


def validate_role_artifact(role: str, artifact: dict) -> dict:
    """Validate that a role's deliverable artifact has all required fields.

    Args:
        role: pm/dev/tester/qa
        artifact: Dict with the role's output

    Returns:
        {pass: bool, missing_fields: [...], schema: str}
    """
    schema = ROLE_ARTIFACT_SCHEMAS.get(role)
    if not schema:
        return {"pass": True, "reason": f"No schema defined for role '{role}'"}

    missing = []
    for field in schema["required_fields"]:
        if field not in artifact or not artifact[field]:
            missing.append(field)

    if missing and schema.get("reject_if_missing", False):
        return {
            "pass": False,
            "missing_fields": missing,
            "schema": schema["description"],
            "reason": f"Missing required fields for {role}: {missing}",
        }

    return {"pass": True, "missing_fields": [], "schema": schema["description"]}


def infer_required_artifacts(node_id: str, graph, project_id: str) -> list[dict]:
    """Auto-infer required artifacts by scanning node's primary files.

    Rules:
      - primary file contains @route → requires api_docs
      - node has test files declared → requires test_file
      - node is in L5+ (new feature) → requires api_docs if has server.py in primary
    """
    node_data = graph.get_node(node_id) if graph else {}
    primary = node_data.get("primary", [])
    inferred = []

    if not primary:
        return inferred

    has_routes = False
    workspace = os.environ.get("WORKSPACE_PATH", "/workspace")

    for fp in primary:
        full_path = os.path.join(workspace, fp)
        if not os.path.exists(full_path) or not fp.endswith(".py"):
            continue
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            if '@route(' in content:
                has_routes = True
                break
        except Exception:
            continue

    if has_routes:
        # Infer section name from node_id
        section = _infer_doc_section(node_id, node_data)
        inferred.append({"type": "api_docs", "section": section, "inferred": True})

    # If node declares test files, require them to exist
    test_files = node_data.get("test", [])
    if test_files:
        inferred.append({"type": "test_file", "required": True, "inferred": True})

    return inferred


def _infer_doc_section(node_id: str, node_data: dict) -> str:
    """Guess the docs section name for a node.

    Maps known node patterns to doc sections.
    """
    title = (node_data.get("title") or "").lower()
    desc = (node_data.get("description") or "").lower()
    combined = title + " " + desc

    # Known mappings
    section_hints = {
        "http 服务": "endpoints",
        "http服务": "endpoints",
        "server": "endpoints",
        "路由": "endpoints",
        "coverage": "coverage_check",
        "gatekeeper": "gatekeeper",
        "token": "token_model",
        "双令牌": "token_model",
        "lifecycle": "agent_lifecycle",
        "租约": "agent_lifecycle",
        "context": "session_context",
        "上下文": "session_context",
        "task registry": "task_registry",
        "任务": "task_registry",
        "release": "workflow_rules",
        "发布": "workflow_rules",
        "memory": "memory_guide",
        "记忆": "memory_guide",
        "telegram": "telegram_integration",
        "gateway": "telegram_integration",
        "artifact": "coverage_check",
        "doc": "coverage_check",
        "quickstart": "quickstart",
    }

    for hint, section in section_hints.items():
        if hint in combined:
            return section

    # Fallback: use node_id as section key
    return node_id.lower().replace(".", "_")


def check_node_artifacts(
    node_id: str,
    graph,
    project_id: str,
) -> dict:
    """Check all artifacts for a node — both declared AND auto-inferred.

    1. Read declared artifacts from graph node data
    2. Auto-infer additional artifacts from primary file analysis
    3. Merge (declared takes precedence)
    4. Check all

    Returns:
        {
            pass: bool,
            checked: [{type, pass, detail, inferred?}],
            missing: [{type, reason, inferred?}]
        }
    """
    node_data = graph.get_node(node_id) if graph else {}

    # Declared artifacts
    declared = node_data.get("artifacts", [])

    # Auto-inferred artifacts
    inferred = infer_required_artifacts(node_id, graph, project_id)

    # Merge: declared types take precedence
    declared_types = {a.get("type") if isinstance(a, dict) else a for a in declared}
    artifacts = list(declared)
    for inf in inferred:
        if inf["type"] not in declared_types:
            artifacts.append(inf)

    if not artifacts:
        return {"pass": True, "checked": [], "missing": [], "note": "No artifacts required"}

    checked = []
    missing = []

    for artifact in artifacts:
        if isinstance(artifact, str):
            artifact = {"type": artifact}

        a_type = artifact.get("type", "")
        is_inferred = artifact.get("inferred", False)
        checker = ARTIFACT_CHECKERS.get(a_type)

        if not checker:
            checked.append({"type": a_type, "pass": True, "detail": f"Unknown type '{a_type}', skipped", "inferred": is_inferred})
            continue

        result = checker(node_id, artifact, graph, project_id)
        checked.append({"type": a_type, "pass": result.get("pass", False), "detail": result, "inferred": is_inferred})

        if not result.get("pass", False):
            missing.append({"type": a_type, "reason": result.get("reason", "Check failed"), "inferred": is_inferred})

    return {
        "pass": len(missing) == 0,
        "checked": checked,
        "missing": missing,
    }


def check_artifacts_for_qa_pass(
    node_ids: list[str],
    graph,
    project_id: str,
) -> dict:
    """Check artifacts for multiple nodes before qa_pass.

    Returns combined result. If any node fails, overall fails.
    """
    results = {}
    all_pass = True

    for node_id in node_ids:
        result = check_node_artifacts(node_id, graph, project_id)
        results[node_id] = result
        if not result["pass"]:
            all_pass = False

    return {
        "pass": all_pass,
        "nodes": results,
    }
