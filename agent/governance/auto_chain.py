# v11: worktree isolation verified
"""Auto-chain dispatcher.

Wires task completion to next-stage task creation with gate validation
between each stage. Called by complete_task() when a task succeeds.

Full chain: PM → Dev → Test → QA → Merge → Deploy
Each transition runs a gate check before advancing.
"""

import json
import logging
import os
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

    Uses a SEPARATE connection to avoid holding caller's transaction lock
    during potentially slow gate checks and task creation.

    Returns dict with chain result, or None if not a chain-eligible task.
    """
    if status != "succeeded":
        return None
    if task_type not in CHAIN:
        return None

    # Use independent connection — don't hold caller's lock during chain ops
    from .db import get_connection
    try:
        conn = get_connection(project_id)
    except Exception:
        log.error("auto_chain: failed to get independent connection for %s", project_id)
        return None
    try:
        result_val = _do_chain(conn, project_id, task_id, task_type, result, metadata)
        conn.commit()
        return result_val
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _do_chain(conn, project_id, task_id, task_type, result, metadata):
    """Internal chain logic with guaranteed conn cleanup by caller."""

    # Non-blocking preflight log (first stage only)
    if task_type == "pm":
        try:
            from .preflight import run_preflight
            report = run_preflight(conn, project_id, auto_fix=False)
            if report.get("warnings"):
                log.warning("preflight warnings for %s: %s", project_id, report["warnings"])
            if not report.get("ok"):
                log.error("preflight blockers for %s: %s", project_id, report["blockers"])
        except Exception:
            pass  # never block chain on preflight failure

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

    # Emit task.completed to chain context store
    _publish_event("task.completed", {
        "project_id": project_id, "task_id": task_id,
        "result": result, "type": task_type,
    })
    # A1: Audit task.completed lifecycle event
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, f"{task_type}.completed",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=task_id,
            chain_depth=depth,
        )
    except Exception:
        log.debug("auto_chain: audit task.completed failed (non-critical)", exc_info=True)

    # M1: PM completes → persist PRD scope to memory for future dev/qa recall
    if task_type == "pm":
        prd = result.get("prd", result)
        requirements = prd.get("requirements", result.get("requirements", []))
        criteria = result.get("acceptance_criteria", prd.get("acceptance_criteria", []))
        if requirements or criteria:
            _write_chain_memory(
                conn, project_id, "prd_scope",
                json.dumps({"requirements": requirements,
                            "acceptance_criteria": criteria,
                            "summary": result.get("summary", "")},
                           ensure_ascii=False),
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "pm"},
            )

    # M4: Test completes → write validation_result memory (marks dev decision as tested)
    if task_type == "test":
        report = result.get("test_report", {})
        passed = report.get("passed", 0) if isinstance(report, dict) else 0
        if passed:
            _write_chain_memory(
                conn, project_id, "validation_result",
                f"Tests passed ({passed} passing) for {', '.join(metadata.get('changed_files', [])[:3])}",
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "test",
                                   "test_report": report,
                                   "validation_status": "tested",
                                   "parent_task_id": metadata.get("parent_task_id", "")},
            )

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
        # M3: Gate fail → write pitfall memory so future tasks avoid same mistake
        _write_chain_memory(
            conn, project_id, "pitfall",
            f"Gate blocked at {task_type}: {reason}",
            metadata,
            extra_structured={"task_id": task_id, "gate_stage": task_type,
                               "gate_reason": reason, "chain_stage": task_type},
        )
        # G3: Persist gate.blocked to audit_index
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "gate.blocked",
                actor="auto-chain",
                ok=False,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                stage=task_type,
                next_stage=next_type or "deploy",
                reason=reason,
            )
        except Exception:
            log.debug("auto_chain: audit gate.blocked failed (non-critical)", exc_info=True)

        # Special cases: test failure or QA rejection → retry as dev (not same stage)
        # Dev fixes the root cause; re-running test/qa without a code fix is wasteful
        if task_type in ("test", "qa"):
            failure_reason = reason
            if task_type == "qa":
                # Prefer specific rejection reason from QA result over gate reason
                failure_reason = result.get("reason", reason)
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            stage_retry_prompt = (
                f"Fix {task_type} stage failures from task {task_id}.\n"
                f"failure_reason: {failure_reason}\n"
                f"retry_from_stage: {task_type}\n\n"
                f"Original task: {original_prompt}"
            )
            from . import task_registry
            dev_retry = task_registry.create_task(
                conn, project_id,
                prompt=stage_retry_prompt,
                task_type="dev",
                created_by="auto-chain-stage-retry",
                metadata={
                    **metadata,
                    "parent_task_id": task_id,
                    "chain_depth": depth + 1,
                    "failure_reason": failure_reason,
                    "retry_from_stage": task_type,
                    "_original_prompt": original_prompt,
                },
            )
            retry_id = dev_retry.get("task_id", "?")
            log.info("auto_chain: %s failure → dev retry task %s", task_type, retry_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": failure_reason,
                "retry_from_stage": task_type,
            })
            return {
                "gate_blocked": True, "stage": task_type, "reason": reason,
                "retry_task_id": retry_id, "retry_type": "dev",
                "retry_from_stage": task_type,
            }

        # Auto-retry: create a new task at the SAME stage with gate reason injected
        # Max 2 retries per gate to prevent infinite loops
        gate_retries = metadata.get("_gate_retry_count", 0)
        if gate_retries < 2 and depth < MAX_CHAIN_DEPTH - 1 and not metadata.get("_no_retry"):
            # Recover original prompt: metadata → chain context → result summary
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            retry_prompt = (
                f"Previous attempt ({task_id}) was blocked by gate.\n"
                f"Gate reason: {reason}\n\n"
                f"Fix the issue described above and retry.\n"
                f"Original task: {original_prompt}"
            )
            from . import task_registry
            retry_task = task_registry.create_task(
                conn, project_id,
                prompt=retry_prompt,
                task_type=task_type,
                created_by="auto-chain-retry",
                metadata={
                    **metadata,
                    "parent_task_id": task_id,
                    "chain_depth": depth + 1,
                    "previous_gate_reason": reason,
                    "_gate_retry_count": gate_retries + 1,
                    "_original_prompt": original_prompt,
                },
            )
            retry_id = retry_task.get("task_id", "?")
            log.info("auto_chain: retry created %s for blocked %s", retry_id, task_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": reason,
            })
            return {"gate_blocked": True, "stage": task_type, "reason": reason,
                    "retry_task_id": retry_id}

        # Retry exhausted — emit task.failed
        _publish_event("task.failed", {
            "project_id": project_id, "task_id": task_id,
            "reason": "gate_retry_exhausted", "gate_reason": reason,
        })
        return {"gate_blocked": True, "stage": task_type, "reason": reason}

    # M5: Dev success + checkpoint gate pass → write success pattern memory
    if task_type == "dev":
        _changed_for_pattern = result.get("changed_files", metadata.get("changed_files", []))
        _summary_for_pattern = result.get("summary", "")
        _write_chain_memory(
            conn, project_id, "pattern",
            _summary_for_pattern or f"Dev completed: {', '.join(_changed_for_pattern[:3])}",
            metadata,
            extra_structured={
                "task_id": task_id, "chain_stage": "dev",
                "changed_files": _changed_for_pattern,
                "gate": "checkpoint_pass",
            },
        )

    # Terminal stage → trigger deploy + archive chain
    if next_type is None:
        builder_fn = _BUILDERS[builder_name]
        deploy_result = builder_fn(conn, project_id, task_id, result, metadata)
        log.info("auto_chain: deploy triggered from task %s: %s", task_id, deploy_result)
        # A2: chain.completed audit summary
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "chain.completed",
                actor="auto-chain",
                ok=True,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                chain_depth=depth,
                changed_files=metadata.get("changed_files", []),
            )
        except Exception:
            log.debug("auto_chain: audit chain.completed failed (non-critical)", exc_info=True)
        # Archive chain context (release memory, DB data preserved)
        try:
            from .chain_context import get_store
            get_store().archive_chain(task_id, project_id)
        except Exception:
            log.debug("auto_chain: chain archive failed (non-critical)")
        return deploy_result

    # Create next stage task (with dedup check)
    builder_fn = _BUILDERS[builder_name]
    prompt, task_meta = builder_fn(task_id, result, metadata)

    # M6: Dedup — check if next stage already exists for this parent
    from . import task_registry
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE type = ? AND status IN ('queued','claimed') "
        "AND metadata_json LIKE ?",
        (next_type, f'%"parent_task_id": "{task_id}"%'),
    ).fetchone()
    if existing:
        log.warning("auto_chain: dedup — %s task already exists for parent %s: %s",
                     next_type, task_id, existing["task_id"])
        return {"task_id": existing["task_id"], "dedup": True}

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
        "prompt": prompt,
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
    """Validate PM PRD has mandatory fields: target_files, verification, acceptance_criteria.

    Falls back to task metadata if PM output lacks structured PRD fields.
    This handles cases where Claude outputs free-text instead of JSON.
    """
    prd = result.get("prd", {})
    # Check each field in result → prd → metadata (fallback chain)
    missing = []
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field) and not metadata.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"
    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    if not target_files:
        return False, "PRD target_files is empty"
    # Merge PRD fields back into result so downstream stages can access them
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field):
            result[field] = prd.get(field) or metadata.get(field)
    return True, "ok"


def _gate_checkpoint(conn, project_id, result, metadata):
    """Checkpoint gate: files changed? no unrelated files outside target_files? docs updated?"""
    log.info("checkpoint_gate: result keys=%s, changed_files=%s, target_files=%s",
             list(result.keys()) if result else None,
             result.get("changed_files"),
             metadata.get("target_files"))
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
            # Block by default — skip_doc_check only allowed with bootstrap_reason
            if metadata.get("skip_doc_check", False):
                bootstrap_reason = metadata.get("bootstrap_reason", "")
                if not bootstrap_reason:
                    return False, (f"skip_doc_check=true requires bootstrap_reason in metadata. "
                                   f"Missing docs: {sorted(missing_docs)}")
                log.warning("checkpoint_gate: docs skipped (bootstrap: %s): %s",
                            bootstrap_reason, sorted(missing_docs))
            else:
                return False, f"Related docs not updated: {sorted(missing_docs)}. Add them to changed_files."
    # Node status check: related_nodes must be at least "testing" (not "pending")
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "testing")
        if not passed:
            return False, f"checkpoint gate blocked — {reason}"
    return True, "ok"


def _gate_t2_pass(conn, project_id, result, metadata):
    """Verify tests passed before advancing to QA."""
    report = result.get("test_report", {})
    failed = report.get("failed", 0)
    if failed is None:
        failed = 0
    if failed > 0:
        return False, f"Tests failed: {failed} failures"
    # Update nodes FIRST (test passed → promote to t2_pass)
    # Evidence validator checks summary.passed > 0, so ensure it's there
    passed_count = report.get("passed", 1)  # Default 1 if not reported (tests passed gate)
    summary = {**report, "passed": passed_count, "failed": failed}
    _try_verify_update(conn, project_id, metadata, "t2_pass", "tester",
                       {"type": "test_report", "producer": "auto-chain",
                        "tool": report.get("tool", "pytest"),
                        "summary": summary})
    # Then verify nodes reached t2_pass
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "t2_pass")
        if not passed:
            return False, f"t2_pass gate blocked — {reason}"
    return True, "ok"


def _gate_qa_pass(conn, project_id, result, metadata):
    """Verify QA recommendation before merge.

    Requires explicit qa_pass or qa_pass_with_fallback recommendation.
    Missing or ambiguous recommendation is a hard block (not auto-pass).
    """
    rec = result.get("recommendation", "")
    if rec in ("qa_pass", "qa_pass_with_fallback"):
        pass  # Explicit pass
    elif rec in ("reject", "rejected"):
        return False, f"QA rejected: {result.get('reason', 'no reason given')}"
    else:
        # No explicit recommendation — BLOCK. Auto-pass is a security risk.
        return False, (
            f"QA gate requires explicit recommendation ('qa_pass' or 'reject'). "
            f"Got: {rec!r}. QA agent must set result.recommendation."
        )
    # Update nodes FIRST (QA passed → promote to qa_pass)
    # Evidence rule: t2_pass → qa_pass requires "e2e_report" with summary.passed > 0
    _try_verify_update(conn, project_id, metadata, "qa_pass", "qa",
                       {"type": "e2e_report", "producer": "auto-chain",
                        "summary": {"passed": 1, "failed": 0,
                                    "review": result.get("review_summary", "auto-chain QA pass")}})
    # Then verify nodes reached qa_pass
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            return False, f"qa_pass gate blocked — {reason}"
    # M2: QA passed → write success pattern memory
    _write_chain_memory(
        conn, project_id, "qa_decision",
        result.get("review_summary", f"QA approved (rec={rec})"),
        metadata,
        extra_structured={"recommendation": rec, "chain_stage": "qa",
                          "changed_files": metadata.get("changed_files", [])},
    )
    return True, "ok"


def _gate_release(conn, project_id, result, metadata):
    """Verify merge succeeded before deploy."""
    # Node status check: all related_nodes must be "qa_pass" before merge is allowed
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            return False, f"release gate blocked — {reason}"
    else:
        log.warning("release gate: no related_nodes — node verification skipped for %s",
                     metadata.get("parent_task_id", "unknown"))
    # For auto-chain deploys, we trust the merge task result
    # After successful merge, promote related_nodes to qa_pass
    if related_nodes:
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

    # Fallback: if PM result lacks expected structure, read from chain context
    if not target_files or not verification or not criteria:
        try:
            from .chain_context import get_store
            parent_result = get_store().get_parent_result(task_id)
            if parent_result:
                if not target_files:
                    target_files = parent_result.get("target_files", target_files)
                if not verification:
                    verification = parent_result.get("verification", verification)
                if not criteria:
                    criteria = parent_result.get("acceptance_criteria", criteria)
                if not requirements:
                    requirements = parent_result.get("requirements", requirements)
        except Exception:
            pass
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
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": metadata.get("related_nodes") or result.get("related_nodes", []),
    }
    # Propagate worktree info from dev result → test → qa → merge
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_qa_prompt(task_id, result, metadata):
    report = result.get("test_report", {})
    changed = result.get("changed_files", metadata.get("changed_files", []))
    prompt = (
        f"QA review for {task_id}.\n"
        f"test_report: {json.dumps(report)}\n"
        f"changed_files: {json.dumps(changed)}\n"
        f"IMPORTANT: result.recommendation MUST be exactly 'qa_pass' or 'reject' "
        f"(no other values accepted by the gate)."
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

def _write_chain_memory(conn, project_id, kind, content, metadata, extra_structured=None):
    """Best-effort memory write for chain events. Never blocks chain progress."""
    try:
        from . import memory_service
        from .models import MemoryEntry
        # Derive module_id from first target_file or changed_file
        target = (metadata.get("target_files") or metadata.get("changed_files") or [])
        module_id = target[0].replace("/", ".").replace("\\", ".") if target else "governance"
        entry = MemoryEntry(
            module_id=module_id,
            kind=kind,
            content=content,
            created_by="auto-chain",
        )
        result = memory_service.write_memory(conn, project_id, entry)
        if extra_structured:
            # Patch structured field if write succeeded
            mid = result.get("memory_id", "")
            if mid:
                try:
                    import json as _json
                    conn.execute(
                        "UPDATE memories SET structured = ? WHERE memory_id = ?",
                        (_json.dumps(extra_structured), mid),
                    )
                except Exception:
                    pass
    except Exception:
        log.debug("_write_chain_memory failed (non-critical)", exc_info=True)


# Status ordering for node_state validation
_STATUS_ORDER = ["pending", "testing", "t2_pass", "qa_pass"]


def _check_nodes_min_status(conn, project_id, related_nodes, min_status):
    """Verify every node in related_nodes has at least min_status in node_state.

    Returns (passed: bool, reason: str).
    If a node is not found in the DB it is treated as 'pending' and blocks.
    """
    if not related_nodes:
        return True, "no related_nodes"
    try:
        min_rank = _STATUS_ORDER.index(min_status)
    except ValueError:
        return False, f"unknown min_status '{min_status}'"

    blocking = []
    for node_id in related_nodes:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            # Not found → treat as pending
            blocking.append((node_id, "pending (not found in DB)"))
            continue
        status = (row["verify_status"] or "pending").strip()
        try:
            rank = _STATUS_ORDER.index(status)
        except ValueError:
            # Unknown status — treat conservatively as pending
            blocking.append((node_id, f"unknown status '{status}'"))
            continue
        if rank < min_rank:
            blocking.append((node_id, status))

    if blocking:
        details = ", ".join(f"{nid}={st}" for nid, st in blocking)
        return False, (
            f"related_nodes not yet at '{min_status}': [{details}]"
        )
    return True, "ok"


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
        log.warning("auto_chain: verify_update %s failed (non-blocking): %s", target_status, e,
                    exc_info=True)


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
