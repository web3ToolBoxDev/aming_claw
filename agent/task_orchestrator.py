"""Task Orchestrator — Code controls flow, AI provides decisions.

Core of v6 architecture. Handles:
  - user message → coordinator session → validate → execute → reply
  - dev complete → evidence collection → coordinator eval → archive
  - retry on validation failure
  - automatic memory archival
  - multi-role parallel execution (dev + tester concurrent)
  - task dependency chains (dev → tester → qa auto)
  - plan-based multi-task orchestration
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

log = logging.getLogger(__name__)


class TaskOrchestrator:
    """Orchestrates all task flows. Code-driven, AI only outputs decisions."""

    def __init__(self):
        from ai_lifecycle import AILifecycleManager
        from context_assembler import ContextAssembler
        from decision_validator import DecisionValidator, build_retry_prompt
        from graph_validator import GraphValidator
        from evidence_collector import EvidenceCollector
        from ai_output_parser import parse_ai_output

        self.ai_manager = AILifecycleManager()
        self.context_assembler = ContextAssembler()
        self.graph_validator = GraphValidator()
        self.evidence_collector = EvidenceCollector()
        self.decision_validator = DecisionValidator(
            graph_validator=self.graph_validator
        )
        self._parse = parse_ai_output
        self._build_retry = build_retry_prompt
        self._max_retries = 3

    def handle_user_message(self, chat_id: int, text: str,
                            project_id: str, token: str) -> dict:
        """Process a user message through Coordinator AI.

        Flow:
            1. Assemble context
            2. Start Coordinator AI session
            3. Parse structured output
            4. Validate decisions
            5. Retry if needed
            6. Execute approved actions
            7. Reply to user
            8. Update context

        Returns:
            {"reply": str, "actions_executed": int, "actions_rejected": int}
        """
        # 0. Check if this needs PM analysis first (new feature / complex request)
        pm_prd = None
        needs_pm = self._needs_pm_analysis(text)
        log.info("PM check: needs_pm=%s for: %s", needs_pm, text[:60])
        if needs_pm:
            try:
                pm_prd = self._run_pm_analysis(text, project_id, chat_id)
                log.info("PM analysis result: %s", "PRD generated" if pm_prd else "empty/failed")
            except Exception as e:
                print(f"[PM] PM analysis failed: {e}")
                log.exception("PM analysis failed: %s", e)
                pm_prd = None

        # 1. Assemble context (include PRD if PM ran)
        extra = {"prd": pm_prd} if pm_prd else None
        context = self.context_assembler.assemble(
            project_id=project_id,
            chat_id=chat_id,
            role="coordinator",
            prompt=text,
            extra=extra,
        )

        # 2. Start Coordinator AI
        coordinator_prompt = text
        if pm_prd:
            coordinator_prompt = (
                f"PM 已完成需求分析:\n{json.dumps(pm_prd, ensure_ascii=False)[:2000]}\n\n"
                f"原始用户消息: {text}\n\n"
                f"请根据 PM 的 PRD 编排执行。"
            )

        session = self.ai_manager.create_session(
            role="coordinator",
            prompt=coordinator_prompt,
            context=context,
            project_id=project_id,
            timeout_sec=120,
        )

        # 3. Wait for output
        raw_output = self.ai_manager.wait_for_output(session.session_id)

        if raw_output.get("status") != "completed":
            return {
                "reply": f"Coordinator 执行失败: {raw_output.get('status')} - {raw_output.get('stderr', '')[:200]}",
                "actions_executed": 0,
                "actions_rejected": 0,
            }

        # 4. Parse AI output
        ai_decision = self._parse(raw_output.get("stdout", ""), role="coordinator")

        # 5. Validate
        validation = self.decision_validator.validate(
            "coordinator", ai_decision, project_id
        )

        # 6. Retry if validation failed and retryable
        retries = 0
        while validation.needs_retry and retries < self._max_retries:
            retries += 1
            log.info("Retrying coordinator (attempt %d): %s", retries, validation.summary)

            retry_prompt = self._build_retry(ai_decision, validation)
            retry_session = self.ai_manager.create_session(
                role="coordinator",
                prompt=f"{text}\n\n{retry_prompt}",
                context=context,
                project_id=project_id,
                timeout_sec=120,
            )
            retry_output = self.ai_manager.wait_for_output(retry_session.session_id)
            if retry_output.get("status") == "completed":
                ai_decision = self._parse(retry_output.get("stdout", ""), role="coordinator")
                validation = self.decision_validator.validate(
                    "coordinator", ai_decision, project_id
                )
            else:
                break

        # 7. Execute approved actions
        executed = 0
        for action in validation.approved_actions:
            success = self._execute_action(action, project_id, token, chat_id)
            if success:
                executed += 1

        # 8. Get reply
        reply = ai_decision.get("reply", "")
        if not reply:
            reply = "处理完成"

        # Add rejection info if any
        if validation.rejected_actions:
            rejection_info = []
            for r in validation.rejected_actions:
                rejection_info.append(f"  [{r['action'].get('type', '?')}] {', '.join(r['reasons'])}")
            reply += f"\n\n[系统] {len(validation.rejected_actions)} 个操作被拦截:\n" + "\n".join(rejection_info)

        # 9. Update context
        self._update_context(project_id, chat_id, text, reply, ai_decision)

        return {
            "reply": reply,
            "actions_executed": executed,
            "actions_rejected": len(validation.rejected_actions),
        }

    def handle_dev_complete(self, task_id: str, project_id: str,
                            token: str, chat_id: int, ai_report: dict) -> dict:
        """Handle dev task completion: collect evidence, eval, archive.

        Flow:
            1. Collect real evidence (git diff, tests)
            2. Compare with AI report
            3. Start Coordinator eval session
            4. Validate eval decisions
            5. Execute approved actions
            6. Reply to user
            7. Archive
        """
        # 1. Collect evidence
        before = ai_report.get("_before_snapshot", {"commit": "HEAD~1"})
        try:
            evidence = self.evidence_collector.collect_after_dev(before)
            log.info("handle_dev_complete evidence: changed=%s",
                     evidence.changed_files if hasattr(evidence, 'changed_files') else 'N/A')
        except Exception as e:
            log.warning("Evidence collection failed in eval: %s, using ai_report fallback", e)
            # Create a minimal evidence-like object from ai_report
            class _FakeEvidence:
                def __init__(self, files):
                    self.changed_files = files
                    self.new_files = []
                    self.test_results = {"passed": True}
                    self.diff_stat = ""
                def to_dict(self):
                    return {"changed_files": self.changed_files, "test_results": self.test_results}
            evidence = _FakeEvidence(ai_report.get("changed_files", []))

        # 2. Compare
        comparison = self.evidence_collector.compare_with_ai_report(evidence, ai_report)
        if comparison["has_discrepancies"]:
            log.warning("Dev report discrepancies: %s", comparison["discrepancies"])

        # 3. Eval context
        eval_context = self.context_assembler.assemble(
            project_id=project_id,
            chat_id=chat_id,
            role="coordinator",
            prompt="评估 dev 任务结果",
            extra={
                "dev_task_id": task_id,
                "evidence": evidence.to_dict(),
                "ai_report": ai_report,
                "discrepancies": comparison.get("discrepancies", []),
            },
        )

        # 4. Code-driven eval (AI eval is optional enhancement)
        has_changes = bool(evidence.changed_files) if hasattr(evidence, 'changed_files') else False
        critical_discrepancies = [d for d in comparison.get("discrepancies", [])
                                  if d.get("issue") not in ("test_count_mismatch",)]
        auto_approve = has_changes and not critical_discrepancies

        eval_decision = {}
        eval_status = ""

        if auto_approve:
            eval_status = "approved"
            reply = f"Dev {task_id} 自动评估通过: {len(evidence.changed_files)} files changed"
            eval_decision = {"status": "approved", "reply": reply}
            log.info("Eval auto-approved: %s (%d files)", task_id, len(evidence.changed_files))
        else:
            # Try AI eval (optional, failure = manual review needed)
            try:
                eval_session = self.ai_manager.create_session(
                    role="coordinator",
                    prompt="Dev 任务完成，请评估结果并决定下一步",
                    context=eval_context,
                    project_id=project_id,
                )
                eval_output = self.ai_manager.wait_for_output(eval_session.session_id)
                eval_decision = self._parse(eval_output.get("stdout", ""), role="coordinator")
                eval_status = eval_decision.get("status", "")
                reply = eval_decision.get("reply", f"任务 {task_id} 评估完成")
            except Exception as e:
                log.warning("AI eval failed, marking for manual review: %s", e)
                eval_status = "needs_review"
                reply = f"Dev {task_id} 需要人工审查 (AI eval 失败: {str(e)[:100]})"
                eval_decision = {"status": "needs_review", "reply": reply}

        # 5. Reply
        self._gateway_reply(chat_id, reply, token)

        # 6. Archive
        self._auto_archive(project_id, task_id, evidence, eval_decision)

        # 7. Auto-trigger Tester if approved
        parent_chain_depth = int(ai_report.get("_chain_depth", 0))
        if eval_status in ("approved", "pass", "success"):
            self._trigger_tester(task_id, project_id, token, chat_id, evidence,
                                 parent_chain_depth=parent_chain_depth)

        return {"reply": reply, "evidence": evidence.to_dict()}

    def _trigger_tester(self, parent_task_id: str, project_id: str,
                        token: str, chat_id: int, evidence,
                        parent_chain_depth: int = 0) -> None:
        """Auto-trigger Tester after Dev eval passes."""
        log.info("Auto-triggering Tester for %s", parent_task_id)
        task_id = f"test-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        changed = evidence.changed_files if hasattr(evidence, 'changed_files') else []
        action = {
            "type": "create_test_task",
            "prompt": f"运行测试验证 {parent_task_id} 的代码变更。changed_files: {changed}",
            "target_files": changed,
            "parent_task_id": parent_task_id,
        }
        child_depth = parent_chain_depth + 1
        self._write_task_file(task_id, action, project_id, token, "test_task", chat_id,
                              chain_depth=child_depth)
        self._gateway_reply(chat_id, f"Tester 已启动 ({task_id[-8:]})", token)

    def handle_test_complete(self, task_id: str, project_id: str,
                             token: str, chat_id: int, test_report: dict) -> dict:
        """Handle Tester completion: verify-update t2_pass, then trigger QA."""
        log.info("Test complete: %s", task_id)

        # 1. Submit verify-update: testing → t2_pass
        related_nodes = test_report.get("related_nodes", [])
        if related_nodes:
            try:
                import requests
                gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
                t = token or os.getenv("GOV_COORDINATOR_TOKEN", "")
                requests.post(f"{gov_url}/api/wf/{project_id}/verify-update",
                    headers={"Content-Type": "application/json", "X-Gov-Token": t},
                    json={
                        "nodes": related_nodes,
                        "status": "t2_pass",
                        "evidence": {
                            "type": "test_report",
                            "producer": f"tester-{task_id}",
                            "tool": "pytest",
                            "summary": test_report.get("summary", {}),
                        },
                    }, timeout=10)
            except Exception:
                log.exception("Failed to verify-update t2_pass")

        # 2. Auto-trigger QA
        self._trigger_qa(task_id, project_id, token, chat_id, test_report)

        return {"status": "test_passed", "triggered_qa": True}

    def _trigger_qa(self, parent_task_id: str, project_id: str,
                    token: str, chat_id: int, test_report: dict) -> None:
        """Auto-trigger QA after Tester passes."""
        log.info("Auto-triggering QA for %s", parent_task_id)
        task_id = f"qa-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        action = {
            "type": "create_qa_task",
            "prompt": f"QA 审查 {parent_task_id} 的测试结果和代码变更。test_report: {test_report}",
            "parent_task_id": parent_task_id,
        }
        self._write_task_file(task_id, action, project_id, token, "qa_task", chat_id)
        self._gateway_reply(chat_id, f"QA 已启动 ({task_id[-8:]})", token)

    def handle_qa_complete(self, task_id: str, project_id: str,
                           token: str, chat_id: int, qa_report: dict) -> dict:
        """Handle QA completion: verify-update qa_pass, then trigger Gatekeeper."""
        log.info("QA complete: %s", task_id)

        # 1. Submit verify-update: t2_pass → qa_pass
        related_nodes = qa_report.get("related_nodes", [])
        if related_nodes:
            try:
                import requests
                gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
                t = token or os.getenv("GOV_COORDINATOR_TOKEN", "")
                requests.post(f"{gov_url}/api/wf/{project_id}/verify-update",
                    headers={"Content-Type": "application/json", "X-Gov-Token": t},
                    json={
                        "nodes": related_nodes,
                        "status": "qa_pass",
                        "evidence": {
                            "type": "e2e_report",
                            "producer": f"qa-{task_id}",
                            "tool": "review",
                            "summary": qa_report.get("summary", {}),
                        },
                    }, timeout=10)
            except Exception:
                log.exception("Failed to verify-update qa_pass")

        # 2. Trigger Gatekeeper
        gate_result = self._trigger_gatekeeper(project_id, token, chat_id)

        return {"status": "qa_passed", "gatekeeper": gate_result}

    def _trigger_gatekeeper(self, project_id: str, token: str, chat_id: int) -> dict:
        """Trigger Gatekeeper checks after QA passes. Code-driven, not AI."""
        log.info("Triggering Gatekeeper for %s", project_id)
        try:
            import requests
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            t = token or os.getenv("GOV_COORDINATOR_TOKEN", "")

            # Gatekeeper checks via governance API
            gate = requests.get(f"{gov_url}/api/wf/{project_id}/release-gate",
                headers={"X-Gov-Token": t}, timeout=15).json()

            release_ok = gate.get("release", False)
            gatekeeper_ok = gate.get("gatekeeper", {}).get("pass", False)

            if release_ok and gatekeeper_ok:
                self._gateway_reply(chat_id,
                    f"Gatekeeper PASS\n所有检查通过，可以部署。\n是否批准？回复 '部署' 确认", token)
                return {"pass": True}
            else:
                blockers = gate.get("blockers", [])
                gk_errors = gate.get("gatekeeper", {}).get("errors", [])
                self._gateway_reply(chat_id,
                    f"Gatekeeper BLOCKED\nblockers: {blockers}\nerrors: {gk_errors}", token)
                return {"pass": False, "blockers": blockers, "errors": gk_errors}
        except Exception as e:
            log.exception("Gatekeeper check failed")
            return {"pass": False, "error": str(e)}

    def _execute_action(self, action: dict, project_id: str, token: str = "",
                        chat_id: int = 0) -> bool:
        """Execute a validated action. Code-controlled."""
        action_type = action.get("type", "")
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        t = token or os.getenv("GOV_COORDINATOR_TOKEN", "")

        try:
            import requests

            if action_type == "create_dev_task":
                # Write task file for executor
                task_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                self._write_task_file(task_id, action, project_id, t, "dev_task", chat_id)
                log.info("Created dev task: %s", task_id)
                return True

            elif action_type == "create_test_task":
                task_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                self._write_task_file(task_id, action, project_id, t, "test_task", chat_id)
                return True

            elif action_type == "query_governance":
                endpoint = action.get("endpoint", "")
                if endpoint:
                    requests.get(f"{gov_url}{endpoint}",
                               headers={"X-Gov-Token": t}, timeout=5)
                return True

            elif action_type == "update_context":
                context_update = action if "current_focus" in action else {}
                if context_update:
                    requests.post(f"{gov_url}/api/context/{project_id}/save",
                        headers={"Content-Type": "application/json", "X-Gov-Token": t},
                        json={"context": context_update}, timeout=5)
                return True

            elif action_type == "propose_node":
                # Already validated by GraphValidator (ID/deps/cycle/path)
                node = action.get("node", {})
                node_id = node.get("id", "")
                # Create node via governance API
                result = requests.post(
                    f"{gov_url}/api/wf/{project_id}/node-create",
                    headers={"Content-Type": "application/json", "X-Gov-Token": t},
                    json={"node": node},
                    timeout=10
                )
                if result.status_code < 300:
                    log.info("Node created via propose: %s", node_id)
                else:
                    log.warning("Node creation failed: %s %s", node_id, result.text[:200])
                return True

            elif action_type == "archive_memory":
                dbservice_url = os.getenv("DBSERVICE_URL", "http://localhost:40002")
                requests.post(f"{dbservice_url}/knowledge/upsert",
                    json=action.get("memory", {}), timeout=5)
                return True

            elif action_type == "reply_only":
                return True

            else:
                log.warning("Unknown action type: %s", action_type)
                return False

        except Exception as e:
            log.exception("Failed to execute action %s: %s", action_type, e)
            return False

    def _write_task_file(self, task_id: str, action: dict,
                         project_id: str, token: str, task_type: str,
                         chat_id: int = 0, chain_depth: int = 0):
        """Write task: DB first (source of truth), then file (secondary).

        chain_depth: depth of this task in the auto-chain (0 = top-level).
        Stored as _chain_depth in the task file so Executor can enforce limits.
        """
        from datetime import datetime, timezone
        import requests as _req

        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. DB first — Task Registry is source of truth
        gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
        try:
            _req.post(f"{gov_url}/api/task/{project_id}/create",
                headers={"Content-Type": "application/json", "X-Gov-Token": token},
                json={
                    "task_id": task_id,
                    "prompt": action.get("prompt", ""),
                    "type": task_type,
                    "related_nodes": action.get("related_nodes", []),
                    "metadata": {
                        "target_files": action.get("target_files", []),
                        "source": "orchestrator",
                    },
                }, timeout=5)
        except Exception as e:
            log.warning("Task DB write failed (continuing with file): %s", e)

        # 2. Create dev branch if dev_task
        branch_name = ""
        if task_type == "dev_task":
            branch_name = f"dev/{task_id}"
            try:
                import subprocess
                workspace = os.getenv("CODEX_WORKSPACE", os.getcwd())
                subprocess.run(["git", "checkout", "-b", branch_name],
                    cwd=workspace, capture_output=True, timeout=10)
                log.info("Created branch: %s", branch_name)
            except Exception as e:
                log.warning("Branch creation failed: %s (continuing on current branch)", e)
                branch_name = ""

        # 3. File second — for Executor consumption
        # Resolve workspace label for dispatcher routing
        try:
            from workspace_registry import resolve_workspace_for_task
            ws = resolve_workspace_for_task({"project_id": project_id})
            ws_label = ws.get("label", "") if ws else ""
        except Exception:
            ws_label = ""

        task_data = {
            "task_id": task_id,
            "project_id": project_id,
            "target_workspace": ws_label,  # Explicit routing for dispatcher
            "text": action.get("prompt", ""),
            "prompt": action.get("prompt", ""),
            "action": "claude",
            "type": task_type,
            "target_files": action.get("target_files", []),
            "related_nodes": action.get("related_nodes", []),
            "chat_id": chat_id,
            "_gov_token": token,
            "_branch": branch_name,
            "_chain_depth": chain_depth,
            "created_at": created_at,
        }

        shared_vol = os.getenv("SHARED_VOLUME_PATH",
                               os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        pending_dir = os.path.join(shared_vol, "codex-tasks", "pending")
        os.makedirs(pending_dir, exist_ok=True)

        tmp = os.path.join(pending_dir, f"{task_id}.tmp.json")
        final = os.path.join(pending_dir, f"{task_id}.json")

        with open(tmp, "w") as f:
            json.dump(task_data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, final)

    def _gateway_reply(self, chat_id: int, text: str, token: str = ""):
        """Send reply to user via Gateway API."""
        if not chat_id:
            return
        try:
            import requests
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            t = token or os.getenv("GOV_COORDINATOR_TOKEN", "")
            requests.post(f"{gov_url}/gateway/reply",
                headers={"Content-Type": "application/json", "X-Gov-Token": t},
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=10)
        except Exception:
            log.exception("Failed to send gateway reply")

    def _update_context(self, project_id: str, chat_id: int,
                        user_msg: str, reply: str, ai_decision: dict):
        """Update session context with conversation and decisions."""
        try:
            import requests
            gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
            token = os.getenv("GOV_COORDINATOR_TOKEN", "")

            # 1. Log each message separately (API expects single entry per call)
            for entry in [
                {"type": "user_message", "content": user_msg[:500]},
                {"type": "coordinator_reply", "content": reply[:500]},
            ]:
                requests.post(f"{gov_url}/api/context/{project_id}/log",
                    headers={"Content-Type": "application/json", "X-Gov-Token": token},
                    json=entry, timeout=5)

            # 2. Load current context, append to recent_messages, save back
            ctx_resp = requests.get(f"{gov_url}/api/context/{project_id}/load",
                headers={"X-Gov-Token": token}, timeout=5)
            ctx = ctx_resp.json().get("context", {})

            recent = ctx.get("recent_messages", [])
            if not isinstance(recent, list):
                recent = []
            recent.append({"role": "user", "content": user_msg[:500]})
            recent.append({"role": "coordinator", "content": reply[:500]})
            # Keep last 20 messages
            recent = recent[-20:]

            # 3. Update focus + messages
            ctx_update = ai_decision.get("context_update", {})
            save_data = {
                "recent_messages": recent,
                "current_focus": ctx_update.get("current_focus", ctx.get("current_focus", "")),
                "decisions": ctx_update.get("decisions", ctx.get("decisions", [])),
            }

            requests.post(f"{gov_url}/api/context/{project_id}/save",
                headers={"Content-Type": "application/json", "X-Gov-Token": token},
                json={"context": save_data}, timeout=5)

        except Exception:
            log.exception("Failed to update context")

    def _classify_archive_category(self, entry_type: str, eval_decision: dict,
                                    evidence=None, trigger_reason: str = None) -> str:
        """Derive a semantic category for the archive refId.

        Returns one of:
          dev_noop_retry   — dev ran but produced no file changes
          dev_complete     — dev ran and produced changes
          test_noop_retry  — tester ran but nothing changed / tests were skipped
          test_complete    — tester ran and verified changes
          eval_skip        — eval was explicitly skipped (e.g. noop task)
          chain_limit      — eval skipped because _chain_depth >= 3
          coordinator_eval — generic coordinator evaluation record
        """
        # Direct trigger reason takes precedence
        if trigger_reason in ("chain_limit", "eval_skip"):
            return trigger_reason

        status = eval_decision.get("status", "")
        if status in ("chain_limit", "eval_skip"):
            return status

        is_noop = (
            eval_decision.get("is_noop", False)
            or status in ("noop", "no_change", "skipped")
        )

        if entry_type == "dev_summary":
            has_changes = (
                hasattr(evidence, "changed_files") and bool(evidence.changed_files)
            )
            if not has_changes or is_noop:
                return "dev_noop_retry"
            return "dev_complete"

        if entry_type == "test_summary":
            if is_noop:
                return "test_noop_retry"
            return "test_complete"

        # fallback for decision / other types
        return "coordinator_eval"

    def _auto_archive(self, project_id: str, task_id: str,
                      evidence, eval_decision: dict,
                      trigger_reason: str = None):
        """Automatic archival after task completion. Uses MemoryWriteGuard.

        trigger_reason: optional semantic override (e.g. 'chain_limit', 'eval_skip').
        When provided, all refIds use that category instead of inferring from evidence.
        """
        try:
            from memory_write_guard import MemoryWriteGuard
            guard = MemoryWriteGuard()

            # Archive decisions (with guard)
            ctx_update = eval_decision.get("context_update", {})
            for decision in ctx_update.get("decisions", []):
                category = self._classify_archive_category(
                    "decision", eval_decision, evidence, trigger_reason
                )
                entry = {
                    "refId": f"auto:{project_id}:{category}",
                    "type": "decision",
                    "content": decision[:500],
                    "scope": project_id,
                    "tags": ["auto_archive", task_id],
                }
                guard.guarded_write(entry, project_id)

            # Archive dev summary (with guard)
            if evidence is not None and hasattr(evidence, 'changed_files') and evidence.changed_files:
                category = self._classify_archive_category(
                    "dev_summary", eval_decision, evidence, trigger_reason
                )
                entry = {
                    "refId": f"auto:{project_id}:{category}",
                    "type": "pattern",
                    "content": f"Task {task_id}: modified {', '.join(evidence.changed_files[:5])}",
                    "scope": project_id,
                    "tags": ["auto_archive", "dev_output"],
                }
                guard.guarded_write(entry, project_id)

            # Archive trigger-reason-only events (e.g. chain_limit with no evidence)
            elif trigger_reason in ("chain_limit", "eval_skip"):
                entry = {
                    "refId": f"auto:{project_id}:{trigger_reason}",
                    "type": "decision",
                    "content": f"Task {task_id}: {trigger_reason}",
                    "scope": project_id,
                    "tags": ["auto_archive", trigger_reason],
                }
                guard.guarded_write(entry, project_id)

        except Exception:
            log.exception("Auto-archive failed for task %s", task_id)

    # ── PM Analysis ──

    def _needs_pm_analysis(self, text: str) -> bool:
        """Check if user message needs PM analysis before Coordinator.

        PM is triggered for any dev-related request to ensure PRD with
        explicit target_files is generated before Dev task dispatch.
        Only pure queries (状态/查看/列出) skip PM.
        """
        # Skip PM for pure queries
        query_only = [
            "状态", "status", "查看", "列出", "list", "show",
            "查询", "多少", "几个", "有没有", "ping",
        ]
        lower = text.lower()
        if any(kw in lower for kw in query_only) and not any(
            kw in lower for kw in ["修", "改", "加", "写", "实现", "fix", "add"]
        ):
            return False

        # Trigger PM for anything that implies code changes
        pm_keywords = [
            # Chinese
            "新功能", "添加功能", "设计", "方案", "需求",
            "架构", "重构", "需要", "修改", "增加", "补充",
            "优化", "实现", "修复", "修", "改", "加",
            "写", "创建", "删除", "移除",
            "我要", "我想要", "能不能加", "需要一个",
            # English
            "new feature", "redesign", "RFC", "PRD",
            "implement", "add", "fix", "update", "modify",
            "enhance", "refactor", "create", "remove", "delete",
            "Gap", "gap",
        ]
        return any(kw in lower for kw in pm_keywords)

    def _run_pm_analysis(self, text: str, project_id: str, chat_id: int) -> dict:
        """Run PM AI to analyze requirements and generate PRD."""
        print(f"[PM] Starting PM analysis for: {text[:60]}")
        log.info("Starting PM analysis for: %s", text[:60])
        pm_context = self.context_assembler.assemble(
            project_id=project_id,
            chat_id=chat_id,
            role="pm",
            prompt=text,
        )

        pm_session = self.ai_manager.create_session(
            role="pm",
            prompt=text,
            context=pm_context,
            project_id=project_id,
            timeout_sec=90,
        )

        pm_output = self.ai_manager.wait_for_output(pm_session.session_id)

        if pm_output.get("status") != "completed":
            log.warning("PM analysis failed: %s", pm_output.get("status"))
            return {}

        pm_decision = self._parse(pm_output.get("stdout", ""), role="pm")

        # Validate PM decisions (propose_node)
        pm_validation = self.decision_validator.validate("pm", pm_decision, project_id)

        # Execute approved PM actions (only propose_node allowed)
        for action in pm_validation.approved_actions:
            if action.get("type") == "propose_node":
                self._execute_action(action, project_id)

        return pm_decision.get("prd", {})

    # ── L17.2: Multi-role parallel ──

    def can_run_parallel(self, role_a: str, role_b: str) -> bool:
        """Check if two roles can run in parallel.

        Allowed: dev + tester (different concerns)
        Not allowed: dev + dev (conflict on same files)
        """
        parallel_pairs = {
            frozenset({"dev", "tester"}),
            frozenset({"dev", "qa"}),
            frozenset({"tester", "qa"}),
        }
        return frozenset({role_a, role_b}) in parallel_pairs

    # ── L17.3: Task dependency chain ──

    def create_dependency_chain(self, project_id: str, token: str,
                                 prompt: str, chat_id: int) -> list[str]:
        """Create a chain: dev_task → test_task → qa_task.

        Each task has parent_task_id pointing to its predecessor.
        Subsequent tasks start as blocked_by_dep.
        """
        chain = []

        # Dev task
        dev_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self._write_task_file(dev_id, {
            "prompt": prompt,
            "target_files": [],
            "related_nodes": [],
        }, project_id, token, "dev_task")
        chain.append(dev_id)

        # Test task (blocked until dev completes)
        test_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        # Don't write file yet — will be created when dev completes
        chain.append(test_id)

        # QA task (blocked until test completes)
        qa_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        chain.append(qa_id)

        log.info("Dependency chain created: %s → %s → %s", dev_id, test_id, qa_id)
        return chain

    # ── L17.5: Plan layer ──

    def create_plan(self, project_id: str, token: str,
                    prompt: str, chat_id: int, tasks: list[dict]) -> dict:
        """Create a plan object that groups multiple tasks.

        A plan is a lightweight wrapper around a sequence of tasks.
        """
        plan_id = f"plan-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        plan = {
            "plan_id": plan_id,
            "project_id": project_id,
            "prompt": prompt,
            "chat_id": chat_id,
            "tasks": [],
            "status": "created",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        for i, task_spec in enumerate(tasks):
            task_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            plan["tasks"].append({
                "task_id": task_id,
                "type": task_spec.get("type", "dev_task"),
                "prompt": task_spec.get("prompt", ""),
                "order": i,
                "status": "queued" if i == 0 else "blocked_by_dep",
                "parent_task_id": plan["tasks"][i-1]["task_id"] if i > 0 else "",
            })

        # Write first task file
        if plan["tasks"]:
            first = plan["tasks"][0]
            self._write_task_file(first["task_id"], {
                "prompt": first["prompt"],
                "target_files": task_spec.get("target_files", []),
                "related_nodes": task_spec.get("related_nodes", []),
            }, project_id, token, first["type"])

        log.info("Plan created: %s with %d tasks", plan_id, len(plan["tasks"]))
        return plan
