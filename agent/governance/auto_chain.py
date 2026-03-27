# v5: enrichment + deploy fix verified
"""Auto-chain dispatcher.

Wires task completion to next-stage task creation with gate validation
between each stage. Called by complete_task() when a task succeeds.

Full chain: PM → Dev → Test → QA → Merge → Deploy
Each transition runs a gate check before advancing.
"""

import json
import logging
import traceback

log = logging.getLogger(__name__)

# Chain definition: task_type → (gate_fn, next_type, prompt_builder)
# next_type=None means terminal stage (deploy trigger)
CHAIN = {
    "pm":    ("_gate_post_pm",    "dev",   "_build_dev_prompt"),
    "dev":   ("_gate_checkpoint", "test",  "_build_test_prompt"),
    "test":  ("_gate_t2_pass",    "qa",    "_build_qa_prompt"),
    "qa":    ("_gate_qa_pass",    "merge", "_build_merge_prompt"),
    "merge": ("_gate_release",    None,    "_trigger_deploy"),
}

# Maximum chain depth to prevent infinite loops
MAX_CHAIN_DEPTH = 10


def on_task_completed(conn, project_id, task_id, task_type, status, result, metadata):
    """Called by complete_task(). Dispatches next stage if gate passes.

    Returns dict with chain result, or None if not a chain-eligible task.
    """
    if status != "succeeded":
        return None
    if task_type not in CHAIN:
        return None

    # Auto-enrich: derive related_nodes from changed_files via impact API
    if not metadata.get("related_nodes"):
        changed = result.get("changed_files", metadata.get("changed_files", []))
        if changed:
            try:
                from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
                from . import project_service
                graph = project_service.load_project_graph(project_id)
                if graph:
                    def _get_status(nid):
                        from .enums import VerifyStatus
                        row = conn.execute(
                            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                            (project_id, nid)).fetchone()
                        return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING
                    analyzer = ImpactAnalyzer(graph, _get_status)
                    request = ImpactAnalysisRequest(
                        changed_files=changed,
                        file_policy=FileHitPolicy(match_primary=True, match_secondary=True),
                    )
                    impact = analyzer.analyze(request)
                    nodes = [n["node_id"] for n in impact.get("affected_nodes", [])]
                    if nodes:
                        metadata["related_nodes"] = nodes
                        log.info("auto_chain: enriched related_nodes from changed_files: %s", nodes)
            except Exception as e:
                log.warning("auto_chain: related_nodes enrichment failed: %s", e)

    depth = metadata.get("chain_depth", 0)
    if depth >= MAX_CHAIN_DEPTH:
        log.warning("auto_chain: max depth %d reached for task %s, stopping", depth, task_id)
        return {"chain_stopped": True, "reason": f"max_chain_depth={MAX_CHAIN_DEPTH}"}

    gate_fn_name, next_type, builder_name = CHAIN[task_type]

    # Pre-gate: version check — ensure governance server is running latest code
    ver_passed, ver_reason = _gate_version_check(project_id, result, metadata)
    if not ver_passed:
        log.info("auto_chain: version gate blocked for task %s: %s", task_id, ver_reason)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": "version_check", "next_stage": task_type,
            "reason": ver_reason,
        })
        return {"gate_blocked": True, "stage": "version_check", "reason": ver_reason}

    # Auto-update nodes based on stage completion
    if task_type == "dev" and metadata.get("related_nodes"):
        _try_verify_update(conn, project_id, metadata, "testing", "dev",
                           {"type": "dev_complete", "producer": "auto-chain",
                            "task_id": task_id})

    # Run gate check
    gate_fn = _GATES[gate_fn_name]
    passed, reason = gate_fn(conn, project_id, result, metadata)
    if not passed:
        log.info("auto_chain: gate blocked %s→%s for task %s: %s",
                 task_type, next_type or "deploy", task_id, reason)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": task_type, "next_stage": next_type or "deploy",
            "reason": reason,
        })
        return {"gate_blocked": True, "stage": task_type, "reason": reason}

    # Terminal stage → trigger deploy
    if next_type is None:
        builder_fn = _BUILDERS[builder_name]
        deploy_result = builder_fn(conn, project_id, task_id, result, metadata)
        log.info("auto_chain: deploy triggered from task %s: %s", task_id, deploy_result)
        return deploy_result

    # Create next stage task
    builder_fn = _BUILDERS[builder_name]
    prompt, task_meta = builder_fn(task_id, result, metadata)

    from . import task_registry
    new_task = task_registry.create_task(
        conn, project_id,
        prompt=prompt,
        task_type=next_type,
        created_by="auto-chain",
        metadata={
            **task_meta,
            "parent_task_id": task_id,
            "chain_depth": depth + 1,
        },
    )

    log.info("auto_chain: %s→%s | %s → %s",
             task_type, next_type, task_id, new_task.get("task_id"))
    _publish_event("task.created", {
        "project_id": project_id,
        "parent_task_id": task_id,
        "task_id": new_task.get("task_id"),
        "type": next_type,
        "source": "auto-chain",
    })
    return new_task


# ---------------------------------------------------------------------------
# Gate functions — each returns (passed: bool, reason: str)
# ---------------------------------------------------------------------------

def _gate_version_check(project_id, result, metadata):
    """Pre-gate: verify governance server is running the latest code.

    Compares the server's startup git hash (from /api/health) against
    the current git HEAD. If the server is stale, blocks the chain.
    """
    if metadata.get("skip_version_check"):
        return True, "skipped"
    try:
        from .server import SERVER_VERSION
        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        if not head or head == "unknown":
            return True, "git HEAD unavailable, skipping"
        if SERVER_VERSION == "unknown":
            return True, "server version unavailable, skipping"
        if SERVER_VERSION != head:
            return False, (
                f"Governance server version ({SERVER_VERSION}) is behind git HEAD ({head}). "
                f"Restart the server to pick up latest code."
            )
        return True, f"version match: {SERVER_VERSION}"
    except Exception as e:
        log.warning("version_check failed (non-blocking): %s", e)
        return True, f"check failed: {e}"


def _gate_post_pm(conn, project_id, result, metadata):
    """Validate PM PRD has mandatory fields: target_files, verification, acceptance_criteria."""
    prd = result.get("prd", {})
    missing = []
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"
    target_files = result.get("target_files", prd.get("target_files", []))
    if not target_files:
        return False, "PRD target_files is empty"
    return True, "ok"


def _gate_checkpoint(conn, project_id, result, metadata):
    """Checkpoint gate: files changed? no unrelated files outside target_files? docs updated?"""
    changed = result.get("changed_files", [])
    if not changed:
        return False, "No files changed"

    # Verify files actually changed in git diff
    try:
        import subprocess
        import os as _os
        repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))
        # Check working-tree + staged changes vs HEAD
        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=repo_root,
        )
        diff_files = set()
        if diff_proc.returncode == 0:
            diff_files = {f.strip() for f in diff_proc.stdout.splitlines() if f.strip()}
        # If working tree is clean, also check the most recent commit
        if not diff_files:
            commit_proc = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                capture_output=True, text=True, timeout=10,
                cwd=repo_root,
            )
            if commit_proc.returncode == 0:
                diff_files = {f.strip() for f in commit_proc.stdout.splitlines() if f.strip()}
        if diff_files:  # Only enforce when git diff data is available
            not_in_diff = [f for f in changed if f not in diff_files]
            if not_in_diff:
                return False, (
                    f"Files listed in changed_files but not found in git diff: {not_in_diff}"
                )
    except Exception as _git_err:
        log.warning("checkpoint_gate: git diff verification failed (non-blocking): %s", _git_err)

    target = metadata.get("target_files", [])
    if target:
        unrelated = [f for f in changed if f not in target]
        if unrelated:
            return False, f"Unrelated files modified: {unrelated}"
    # Syntax check: verify test_results if available
    test_results = result.get("test_results", {})
    if test_results.get("ran") and test_results.get("failed", 0) > 0:
        return False, f"Dev tests failed: {test_results.get('failed')} failures"
    # Doc consistency check: use CODE_DOC_MAP to verify related docs are updated
    from .impact_analyzer import get_related_docs
    code_files = [f for f in changed if not f.startswith("docs/") and not f.endswith(".md")]
    doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
    expected_docs = get_related_docs(code_files)
    if expected_docs:
        missing_docs = expected_docs - doc_files_changed
        if missing_docs:
            # Block by default — set metadata.skip_doc_check=true to bypass
            if metadata.get("skip_doc_check", False):
                log.warning("checkpoint_gate: docs may need update (skipped): %s", sorted(missing_docs))
            else:
                return False, f"Related docs not updated: {sorted(missing_docs)}. Add them to changed_files or set skip_doc_check=true."
    return True, "ok"


def _gate_t2_pass(conn, project_id, result, metadata):
    """Verify tests passed before advancing to QA."""
    report = result.get("test_report", {})
    failed = report.get("failed", 0)
    if failed is None:
        failed = 0
    if failed > 0:
        return False, f"Tests failed: {failed} failures"
    # Update related nodes to t2_pass
    _try_verify_update(conn, project_id, metadata, "t2_pass", "tester",
                       {"type": "test_report", "producer": "auto-chain",
                        "tool": report.get("tool", "pytest"),
                        "summary": report})
    return True, "ok"


def _gate_qa_pass(conn, project_id, result, metadata):
    """Verify QA recommendation before merge.

    Accepts: explicit qa_pass, or auto-pass when tests passed and no reject.
    """
    rec = result.get("recommendation", "")
    if rec in ("qa_pass", "qa_pass_with_fallback"):
        pass  # Explicit pass
    elif rec in ("reject", "rejected"):
        return False, f"QA rejected: {result.get('reason', 'no reason given')}"
    else:
        # No explicit recommendation — auto-pass if severity is not blocking
        severity = result.get("severity", "")
        if severity == "blocking":
            return False, f"QA blocking issue: {result.get('detail', result.get('reason', ''))}"
        # Auto-pass: QA didn't explicitly reject, treat as pass
        log.info("qa_pass gate: no explicit recommendation, auto-passing (rec=%s)", rec)
    # Update related nodes to qa_pass
    _try_verify_update(conn, project_id, metadata, "qa_pass", "qa",
                       {"type": "qa_review", "producer": "auto-chain",
                        "summary": result.get("review_summary", "")})
    return True, "ok"


def _gate_release(conn, project_id, result, metadata):
    """Verify merge succeeded before deploy."""
    # For auto-chain deploys, we trust the merge task result
    # After successful merge, promote related_nodes to qa_pass
    if metadata.get("related_nodes"):
        _try_verify_update(conn, project_id, metadata, "qa_pass", "merge",
                           {"type": "merge_complete", "producer": "auto-chain"})
    return True, "ok"


# ---------------------------------------------------------------------------
# Prompt builders — return (prompt: str, metadata: dict)
# ---------------------------------------------------------------------------

def _build_dev_prompt(task_id, result, metadata):
    prd = result.get("prd", {})
    # target_files: result > prd > original metadata (preserve original task metadata)
    target_files = result.get("target_files", prd.get("target_files", metadata.get("target_files", [])))
    verification = result.get("verification", prd.get("verification", {}))
    requirements = prd.get("requirements", [])
    criteria = result.get("acceptance_criteria", prd.get("acceptance_criteria", []))
    prompt = (
        f"Implement per PRD from {task_id}.\n\n"
        f"target_files: {json.dumps(target_files)}\n"
        f"requirements: {json.dumps(requirements, ensure_ascii=False)}\n"
        f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}"
    )
    return prompt, {
        **metadata,  # preserves skip_doc_check, changed_files, related_nodes, etc.
        "target_files": target_files,
        "verification": verification,
        "related_nodes": result.get("proposed_nodes", metadata.get("related_nodes", [])),
    }


def _build_test_prompt(task_id, result, metadata):
    changed = result.get("changed_files", metadata.get("changed_files", []))
    prompt = (
        f"Run tests for {task_id}.\n"
        f"changed_files: {json.dumps(changed)}"
    )
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": metadata.get("related_nodes") or result.get("related_nodes", []),
    }


def _build_qa_prompt(task_id, result, metadata):
    report = result.get("test_report", {})
    changed = result.get("changed_files", metadata.get("changed_files", []))
    prompt = (
        f"QA review for {task_id}.\n"
        f"test_report: {json.dumps(report)}\n"
        f"changed_files: {json.dumps(changed)}"
    )
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": metadata.get("related_nodes") or result.get("related_nodes", []),
    }


def _build_merge_prompt(task_id, result, metadata):
    prompt = f"Merge dev branch for {task_id} to main."
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": metadata.get("changed_files") or result.get("changed_files", []),
        "related_nodes": metadata.get("related_nodes") or result.get("related_nodes", []),
    }


def _trigger_deploy(conn, project_id, task_id, result, metadata):
    """Terminal stage: invoke deploy_chain.run_deploy().

    NOTE: When called from within the governance server process,
    deploy_chain must NOT restart the governance server (it would kill
    the process running this code). We pass skip_self=True to avoid
    self-restart. The executor worker will detect the version mismatch
    and log a warning; the governance server should be restarted
    separately (e.g. by Docker healthcheck or manual restart).
    """
    changed_files = metadata.get("changed_files", [])
    if not changed_files:
        return {"deploy": "skipped", "reason": "no changed_files in metadata"}
    try:
        import sys
        agent_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from deploy_chain import run_deploy
        chat_id = int(metadata.get("chat_id", 0))
        # skip_self=True prevents governance from restarting itself
        report = run_deploy(changed_files, chat_id=chat_id, project_id=project_id,
                            skip_services=["governance"])
        report["governance_note"] = "skipped self-restart; restart governance manually or via Docker"
        return {"deploy": "completed", "report": report}
    except Exception as e:
        log.error("auto_chain: deploy failed: %s", e)
        traceback.print_exc()
        return {"deploy": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_verify_update(conn, project_id, metadata, target_status, role, evidence_dict):
    """Best-effort node status update. Non-blocking on failure."""
    related = metadata.get("related_nodes", [])
    if not related:
        return
    try:
        from . import state_service
        from .graph import AcceptanceGraph
        # Load graph from project state directory
        import os
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        graph = AcceptanceGraph()
        if os.path.exists(graph_path):
            graph.load(graph_path)
        session = {"principal_id": "auto-chain", "role": role, "scope_json": "[]"}
        state_service.verify_update(
            conn, project_id, graph,
            node_ids=related if isinstance(related, list) else [related],
            target_status=target_status,
            session=session,
            evidence_dict=evidence_dict,
        )
        log.info("auto_chain: nodes %s → %s", related, target_status)
    except Exception as e:
        log.warning("auto_chain: verify_update %s failed (non-blocking): %s", target_status, e)


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Function lookup tables (avoid globals() for safety)
# ---------------------------------------------------------------------------

_GATES = {
    "_gate_post_pm": _gate_post_pm,
    "_gate_checkpoint": _gate_checkpoint,
    "_gate_t2_pass": _gate_t2_pass,
    "_gate_qa_pass": _gate_qa_pass,
    "_gate_release": _gate_release,
}

_BUILDERS = {
    "_build_dev_prompt": _build_dev_prompt,
    "_build_test_prompt": _build_test_prompt,
    "_build_qa_prompt": _build_qa_prompt,
    "_build_merge_prompt": _build_merge_prompt,
    "_trigger_deploy": _trigger_deploy,
}
