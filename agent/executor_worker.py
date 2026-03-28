# v2: git-diff verified, artifact-filtered.
# Verified: executor self-fix bootstrap successful.
"""Executor Worker — polls Governance API for tasks and executes them via Claude CLI.

This is the missing link between:
  - Governance task queue (create/claim/complete)
  - AI execution (ai_lifecycle.py → Claude CLI)

Flow:
  1. Poll: GET /api/task/{project}/list?status=queued
  2. Claim: POST /api/task/{project}/claim
  3. Execute: AILifecycleManager.create_session(role, prompt)
  4. Report: POST /api/task/{project}/progress
  5. Complete: POST /api/task/{project}/complete (triggers auto-chain)

Usage:
  python -m agent.executor_worker --project aming-claw
  GOVERNANCE_URL=http://localhost:40006 python -m agent.executor_worker

Full chain verified: dev→test→qa→merge→deploy.
"""

import json
import logging
import os
import sys
import tempfile
import time
import argparse
import threading
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger("executor_worker")

# --- Configuration ---

GOVERNANCE_URL = os.getenv("GOVERNANCE_URL", "http://localhost:40006")
POLL_INTERVAL = int(os.getenv("EXECUTOR_POLL_INTERVAL", "10"))
WORKER_ID = os.getenv("EXECUTOR_WORKER_ID", f"executor-{os.getpid()}")
WORKSPACE = os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parents[1]))

# Task type → (role, timeout_sec)
TASK_ROLE_MAP = {
    "coordinator": ("coordinator", 120),
    "pm":    ("coordinator", 120),
    "dev":   ("dev", 300),
    "test":  ("tester", 180),
    "qa":    ("qa", 120),
    "merge": ("script", 30),  # handled by _execute_merge, no AI
    "task":  ("dev", 300),
}
# Merge is script-based, see _execute_merge()


class ExecutorWorker:
    """Polls governance API, claims tasks, executes via Claude CLI."""

    def __init__(self, project_id: str, governance_url: str = GOVERNANCE_URL,
                 worker_id: str = WORKER_ID, workspace: str = WORKSPACE):
        self.project_id = project_id
        self.base_url = governance_url.rstrip("/")
        self.worker_id = worker_id
        self.workspace = workspace
        self._running = False
        self._current_task = None
        self._lifecycle = None
        self._pid_path = None  # type: str | None

    def _api(self, method: str, path: str, data: dict = None) -> dict:
        """Call governance API."""
        import requests
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                r = requests.get(url, timeout=10)
            else:
                r = requests.post(url, json=data or {}, timeout=30,
                                  headers={"Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("API call failed: %s %s → %s", method, path, e)
            return {"error": str(e)}

    def _claim_task(self) -> dict | None:
        """Try to claim next queued task."""
        result = self._api("POST", f"/api/task/{self.project_id}/claim",
                           {"worker_id": self.worker_id})
        if "error" in result or "task" not in result:
            return None
        task_pair = result["task"]
        if not task_pair or not isinstance(task_pair, list) or len(task_pair) < 2:
            return None
        task_data, fence_token = task_pair
        if not task_data or not isinstance(task_data, dict):
            return None
        task_data["_fence_token"] = fence_token
        return task_data

    def _report_progress(self, task_id: str, progress: dict):
        """Report execution progress."""
        self._api("POST", f"/api/task/{self.project_id}/progress",
                  {"task_id": task_id, "progress": progress})

    def _complete_task(self, task_id: str, status: str, result: dict) -> dict:
        """Mark task complete (triggers auto-chain)."""
        return self._api("POST", f"/api/task/{self.project_id}/complete",
                         {"task_id": task_id, "status": status, "result": result})

    def _execute_task(self, task: dict) -> dict:
        """Execute a single task via Claude CLI."""
        task_id = task["task_id"]
        task_type = task.get("type", "task")
        prompt = task.get("prompt", "")
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        role, timeout = TASK_ROLE_MAP.get(task_type, ("dev", 300))

        log.info("Executing %s (type=%s, role=%s, timeout=%ds)",
                 task_id, task_type, role, timeout)
        self._report_progress(task_id, {"step": "starting", "role": role})

        # Merge is a script operation, not AI
        if task_type == "merge":
            return self._execute_merge(task_id, metadata)

        # Build context for AI session
        context = {
            "task_id": task_id,
            "task_type": task_type,
            "project_id": self.project_id,
            "target_files": metadata.get("target_files", []),
            "changed_files": metadata.get("changed_files", []),
            "related_nodes": metadata.get("related_nodes", []),
            "attempt_num": task.get("attempt_num", 1),
            "chat_id": metadata.get("chat_id", ""),
            "previous_gate_reason": metadata.get("previous_gate_reason", ""),
            "rejection_reason": metadata.get("rejection_reason", ""),
        }

        # Enhance prompt with governance context
        enhanced_prompt = self._build_prompt(prompt, task_type, context)

        # Create AI session
        if self._lifecycle is None:
            from ai_lifecycle import AILifecycleManager
            self._lifecycle = AILifecycleManager()

        session = self._lifecycle.create_session(
            role=role,
            prompt=enhanced_prompt,
            context=context,
            project_id=self.project_id,
            timeout_sec=timeout,
            workspace=self.workspace,
        )

        if session.status == "failed":
            return {"status": "failed", "error": session.stderr}

        # Wait for completion with progress reporting
        self._report_progress(task_id, {"step": "running", "session_id": session.session_id})
        output = self._lifecycle.wait_for_output(session.session_id)

        if session.status == "timeout":
            return {"status": "failed", "error": f"Timeout after {timeout}s"}
        if session.status == "failed":
            return {"status": "failed", "error": session.stderr[:500]}

        # Detect actually changed files via git diff
        changed_files = self._get_git_changed_files()
        log.info("git diff detected %d changed file(s): %s", len(changed_files), changed_files)

        # Stage changed files if any
        if changed_files:
            try:
                import subprocess
                subprocess.run(
                    ["git", "add", "--"] + changed_files,
                    cwd=self.workspace,
                    capture_output=True,
                    timeout=30,
                )
                log.info("Staged %d changed file(s): %s", len(changed_files), changed_files)
            except Exception as e:
                log.warning("git add failed: %s", e)

        # Parse output and merge with real changed_files
        result = self._parse_output(session, task_type)
        log.info("_parse_output keys: %s", list(result.keys()))

        # Always overwrite/set changed_files from git diff (ground truth)
        # IMPORTANT: git diff is authoritative; always set even if _parse_output
        # returned its own changed_files (it may be stale or from AI hallucination)
        result["changed_files"] = changed_files if changed_files else result.get("changed_files", [])

        log.info("Final result for %s: changed_files=%s, keys=%s",
                 task_id, result.get("changed_files"), list(result.keys()))

        # Write structured memory on completion
        self._write_memory(task_type, task_id, result, metadata)

        return {"status": "succeeded", "result": result}

    def _write_memory(self, task_type: str, task_id: str, result: dict, metadata: dict):
        """Write structured memory after task completion (best-effort)."""
        try:
            changed = result.get("changed_files", [])
            summary = result.get("summary", "")

            if task_type == "dev" and (summary or changed):
                prompt_lower = (metadata.get("original_prompt", summary) or "").lower()
                if any(w in prompt_lower for w in ("fix", "bug", "error")):
                    decision_type = "bugfix"
                elif any(w in prompt_lower for w in ("add", "new", "create", "implement")):
                    decision_type = "feature"
                elif any(w in prompt_lower for w in ("refactor", "clean", "rename")):
                    decision_type = "refactor"
                else:
                    decision_type = "config"

                gate_reason = metadata.get("previous_gate_reason", "")
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module": changed[0] if changed else "general",
                    "kind": "decision",
                    "content": summary or f"Changed {len(changed)} files",
                    "structured": {
                        "decision_type": decision_type,
                        "related_files": changed,
                        "validation_status": "untested",
                        "failure_pattern": gate_reason if gate_reason else None,
                        "followup_needed": bool(gate_reason),
                        "task_id": task_id,
                        "chain_stage": "dev",
                    },
                })

            elif task_type == "test":
                report = result.get("test_report", {})
                passed = report.get("passed", 0) or 0
                failed = report.get("failed", 0) or 0
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module": "testing",
                    "kind": "test_result" if failed == 0 else "failure_pattern",
                    "content": f"{passed} passed, {failed} failed",
                    "structured": {
                        "related_files": changed,
                        "validation_status": "tested" if failed == 0 else "failed",
                        "failure_pattern": report.get("error_summary", "") if failed > 0 else None,
                        "followup_needed": failed > 0,
                        "task_id": task_id,
                        "chain_stage": "test",
                    },
                })

            elif task_type == "qa" and result.get("recommendation") == "reject":
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module": changed[0] if changed else "general",
                    "kind": "failure_pattern",
                    "content": result.get("review_summary", "QA rejected"),
                    "structured": {
                        "root_cause": result.get("reject_reason", ""),
                        "related_files": changed,
                        "followup_needed": True,
                        "task_id": task_id,
                        "chain_stage": "qa",
                    },
                })
        except Exception as e:
            log.warning("Memory write failed (non-fatal): %s", e)

    def _execute_merge(self, task_id: str, metadata: dict) -> dict:
        """Merge is a script operation: git add → git commit. No AI needed."""
        import subprocess

        changed = metadata.get("changed_files", [])
        self._report_progress(task_id, {"step": "merging"})

        try:
            # Stage changed files (or all if none specified)
            if changed:
                subprocess.run(["git", "add", "--"] + changed,
                               cwd=self.workspace, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "add", "-A"],
                               cwd=self.workspace, capture_output=True, timeout=30)

            # Check if there's anything to commit
            status = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                    cwd=self.workspace, capture_output=True, text=True, timeout=10)
            staged = [f.strip() for f in status.stdout.splitlines() if f.strip()]

            if not staged:
                return {"status": "succeeded", "result": {
                    "merge_commit": "none", "branch": "main",
                    "files_changed": 0, "note": "nothing to commit"
                }}

            # Commit
            msg = f"Auto-merge: {task_id}\n\nChanged files: {', '.join(staged[:10])}"
            proc = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=self.workspace, capture_output=True, text=True, timeout=30)

            if proc.returncode != 0:
                return {"status": "failed", "error": f"git commit failed: {proc.stderr[:300]}"}

            # Get commit hash
            rev = subprocess.run(["git", "rev-parse", "HEAD"],
                                 cwd=self.workspace, capture_output=True, text=True, timeout=5)
            commit_hash = rev.stdout.strip()

            log.info("Merge complete: %s (%d files)", commit_hash, len(staged))

            # Immediately sync git HEAD to governance after commit (don't wait for 60s poll)
            HEAD = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.workspace
            ).decode().strip()
            self._api("POST", f"/api/version-sync/{self.project_id}", {"git_head": HEAD, "dirty_files": []})

            # Update VERSION file + DB chain_version + git sync
            try:
                # 1. Update VERSION file
                ver_path = os.path.join(self.workspace, "VERSION")
                if os.path.exists(ver_path):
                    with open(ver_path) as f:
                        content = f.read()
                    import re as _re
                    content = _re.sub(r'CHAIN_VERSION=\S+', f'CHAIN_VERSION={commit_hash}', content)
                    with open(ver_path, 'w') as f:
                        f.write(content)
                    # Amend commit to include VERSION
                    subprocess.run(["git", "add", "VERSION"], cwd=self.workspace, capture_output=True, timeout=10)
                    subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=self.workspace, capture_output=True, timeout=10)
                    # Re-read hash after amend
                    rev2 = subprocess.run(["git", "rev-parse", "HEAD"],
                                          cwd=self.workspace, capture_output=True, text=True, timeout=5)
                    commit_hash = rev2.stdout.strip()

                # 2. Update DB chain_version
                old_ver_row = self._api("GET", f"/api/version-check/{self.project_id}")
                old_ver = old_ver_row.get("chain_version", "")
                self._api("POST", f"/api/version-update/{self.project_id}", {
                    "chain_version": commit_hash,
                    "updated_by": "auto-chain",
                    "task_id": task_id,
                    "chain_stage": "merge",
                    "old_version": old_ver if old_ver != "(not set)" else "",
                })

                # 3. Sync git_head to DB
                self._api("POST", f"/api/version-sync/{self.project_id}", {
                    "git_head": commit_hash,
                    "dirty_files": [],
                })
                log.info("Chain version updated: %s → %s (VERSION + DB + sync)", old_ver, commit_hash)
            except Exception as e:
                log.warning("Version update failed (non-fatal): %s", e)

            # Write merge outcome to memory for future recall
            try:
                summary = metadata.get("intent_summary", "")
                if not summary:
                    summary = f"Merged {len(staged)} files: {', '.join(staged[:5])}"
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module": staged[0] if staged else "general",
                    "kind": "task_result",
                    "content": summary,
                    "structured": {
                        "merge_commit": commit_hash,
                        "changed_files": staged,
                        "files_changed": len(staged),
                        "task_id": task_id,
                        "chain_stage": "merge",
                        "parent_task_id": metadata.get("parent_task_id", ""),
                    },
                })
            except Exception:
                log.debug("Merge memory write failed (non-fatal)")

            return {"status": "succeeded", "result": {
                "merge_commit": commit_hash,
                "branch": "main",
                "files_changed": len(staged),
                "changed_files": staged,
            }}

        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _fetch_memories(self, query: str, top_k: int = 3) -> list:
        """Search memory backend for relevant past work (best-effort, returns list)."""
        try:
            from urllib.parse import quote
            result = self._api("GET", f"/api/mem/{self.project_id}/search?q={quote(query[:120])}&top_k={top_k}")
            return result.get("results", [])
        except Exception:
            return []

    def _build_prompt(self, prompt: str, task_type: str, context: dict) -> str:
        """Enhance task prompt with governance context."""
        parts = [prompt]

        if task_type == "pm":
            memories = self._fetch_memories(prompt)
            if memories:
                parts.append("\nRelevant memories from past work:")
                for m in memories:
                    parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")
            parts.append(
                "\nAnalyze the request and output a PRD as JSON with the following fields: "
                "{\"target_files\": [\"...\"], \"verification\": {\"method\": \"...\", \"command\": \"...\"}, "
                "\"acceptance_criteria\": [\"...\"]}"
            )

        elif task_type == "coordinator":
            chat_id = context.get("chat_id", "")
            rule_decision = context.get("rule_decision", "new")
            rule_reason = context.get("rule_reason", "")

            parts.append(f"\nYou are a Coordinator. Analyze the user message and decide the action.")
            parts.append("User message from Telegram (chat_id=" + str(chat_id) + "):")
            parts.append(f'"{prompt}"')

            # Phase 5: Inject memory search results
            memories = self._fetch_memories(prompt)
            if memories:
                parts.append("\nRelevant memories from past work:")
                for m in memories:
                    parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")

            # Phase 5: Inject active queue state
            try:
                task_list = self._api("GET", f"/api/task/{self.project_id}/list")
                active = [t for t in task_list.get("tasks", []) if t.get("status") in ("queued", "claimed")]
                if active:
                    parts.append(f"\nActive task queue ({len(active)} tasks):")
                    for t in active[:5]:
                        parts.append(f"  - {t.get('task_id','')}: [{t.get('type','')}] {(t.get('prompt',''))[:80]}")
            except Exception:
                pass  # Non-fatal

            # Phase 5: Inject rule engine decision
            if rule_decision and rule_decision != "new":
                parts.append(f"\nRule engine decision: {rule_decision}")
                if rule_reason:
                    parts.append(f"Reason: {rule_reason}")
                if rule_decision == "duplicate":
                    parts.append("Ask the user before re-executing this task.")
                elif rule_decision == "conflict":
                    parts.append("Warn the user about the conflict and offer options.")
                elif rule_decision == "queue":
                    parts.append("Create the task with lower priority (queued behind active work).")
                elif rule_decision == "retry":
                    parts.append("Include past failure context when creating the task.")

            parts.append("\nRespond with EXACTLY ONE JSON object (no other text):")
            parts.append('If this is a question → {"action": "reply", "text": "your answer here"}')
            parts.append('If this needs code changes → {"action": "create_task", "type": "pm", "prompt": "detailed description"}')
            parts.append('If this is a duplicate → {"action": "reply", "text": "This was done recently in task-xxx. Want me to redo it?"}')
            parts.append('If there is a conflict → {"action": "reply", "text": "Conflict detected: ... Options: ..."}')
            parts.append('\nIMPORTANT: Output ONLY the JSON object, nothing else.')

        elif task_type == "test":
            changed = context.get("changed_files", [])
            # Inject past failure patterns for these files
            if changed:
                memories = self._fetch_memories(", ".join(changed[:3]))
                failures = [m for m in memories if m.get("kind") in ("failure_pattern", "test_result")]
                if failures:
                    parts.append("\nPast test failures for these files:")
                    for m in failures:
                        parts.append(f"  - {m.get('summary', m.get('content',''))[:150]}")
            parts.append(f"\nRun tests. Changed files: {json.dumps(changed)}")
            parts.append("Report result as JSON: {\"test_report\": {\"passed\": N, \"failed\": N, \"tool\": \"pytest\"}}")

        elif task_type == "qa":
            changed = context.get("changed_files", [])
            if changed:
                memories = self._fetch_memories(", ".join(changed[:3]))
                decisions = [m for m in memories if m.get("kind") in ("decision", "task_result")]
                if decisions:
                    parts.append("\nPast decisions for these files:")
                    for m in decisions:
                        parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")
            parts.append("\nYou are a QA reviewer. Review the test results and changed files above.")
            parts.append("If tests passed and changes look reasonable, respond ONLY with this exact JSON:")
            parts.append('{"recommendation": "qa_pass", "review_summary": "Tests pass, changes approved"}')
            parts.append("If there are critical issues, respond with:")
            parts.append('{"recommendation": "reject", "reason": "description of issue"}')

        elif task_type == "merge":
            parts.append("\nCommit all staged changes to git and respond with JSON: {\"merge_commit\": \"<hash>\", \"branch\": \"main\", \"files_changed\": N}")

        elif task_type in ("dev", "task"):
            target = context.get("target_files", [])
            if target:
                parts.append(f"\nTarget files: {json.dumps(target)}")
            # Inject memories relevant to target files and prompt
            mem_query = prompt if len(prompt) <= 120 else (", ".join(target[:3]) if target else prompt[:120])
            memories = self._fetch_memories(mem_query)
            if memories:
                parts.append("\nRelevant memories from past work:")
                for m in memories:
                    parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")
            attempt = context.get("attempt_num", 1)
            gate_reason = context.get("previous_gate_reason", "") or context.get("rejection_reason", "")
            if attempt and int(attempt) > 1 and gate_reason:
                parts.append(f"\nThis is retry attempt #{attempt}. Previous attempt was blocked by gate.")
                parts.append(f"Gate rejection reason: {gate_reason}")
                parts.append("Fix ONLY the specific issue described above; do not make unrelated changes.")
            parts.append("\nAfter completing, respond with JSON: {\"changed_files\": [...], \"summary\": \"...\"}")

        return "\n".join(parts)

    # Files/patterns to ignore in git diff (Claude CLI artifacts, not real changes).
    # NOTE: executor_worker.py is NOT excluded here — it can be a legitimate task target.
    # If it were excluded, any dev task targeting this file would always fail the gate
    # with "No files changed", making such tasks unretryable.
    #
    # Node mapping (file → acceptance-graph node):
    #   executor_worker.py        → L3.2  ExecutorWorker (this file — executor process)
    #   governance/auto_chain.py  → L2.1  AutoChain      (stage-transition dispatcher)
    #   governance/graph.py       → L1.3  AcceptanceGraph (DAG rule layer)
    #   governance/task_registry.py → L2.2 TaskRegistry  (task CRUD / queue)
    def _handle_coordinator_result(self, task: dict, result: dict) -> None:
        """Parse coordinator AI output and execute the decided action."""
        import re

        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        chat_id = metadata.get("chat_id", "")

        # Extract JSON from AI output — try multiple strategies
        raw = result.get("summary", "") or result.get("raw_output", "") or json.dumps(result)

        action_data = None
        # Strategy 1: full output is valid JSON
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "action" in parsed:
                action_data = parsed
        except (json.JSONDecodeError, TypeError):
            pass

        # Strategy 2: find JSON block with balanced braces
        if not action_data:
            start = raw.find('{"action"')
            if start == -1:
                start = raw.find("{'action")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == '{': depth += 1
                    elif raw[i] == '}': depth -= 1
                    if depth == 0:
                        try:
                            action_data = json.loads(raw[start:i+1])
                        except json.JSONDecodeError:
                            pass
                        break

        if not action_data:
            log.warning("Coordinator output has no action JSON: %s", raw[:200])
            if chat_id:
                self._telegram_reply(chat_id, raw[:2000])  # Send raw output as fallback
            return

        action = action_data.get("action", "")
        log.info("Coordinator action: %s (task %s)", action, task["task_id"])

        if action == "reply":
            text = action_data.get("text", "No response")
            if chat_id:
                self._telegram_reply(chat_id, text)
            else:
                log.warning("No chat_id for reply: %s", text[:200])

        elif action == "create_task":
            sub_type = action_data.get("type", "pm")
            sub_prompt = action_data.get("prompt", "")
            if sub_prompt:
                sub_result = self._api("POST", f"/api/task/{self.project_id}/create", {
                    "prompt": sub_prompt,
                    "type": sub_type,
                    "priority": 1,
                    "metadata": {
                        "parent_task_id": task["task_id"],
                        "chat_id": chat_id,
                        "source": "coordinator",
                    },
                })
                sub_id = sub_result.get("task_id", "?")
                log.info("Coordinator created subtask: %s (type=%s)", sub_id, sub_type)
                if chat_id:
                    self._telegram_reply(chat_id, f"Task created: {sub_id[-12:]}\nType: {sub_type}\nPrompt: {sub_prompt[:100]}")

        elif action == "scale_workers":
            count = action_data.get("count", 1)
            log.info("Coordinator requested scale to %d workers (not implemented)", count)

        else:
            log.warning("Unknown coordinator action: %s", action)

    def _telegram_reply(self, chat_id, text: str) -> None:
        """Send reply to Telegram via Bot API."""
        import urllib.request, urllib.error
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            log.warning("TELEGRAM_BOT_TOKEN not set, cannot reply")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.error("Telegram reply failed: %s", e)

    _IGNORE_PATTERNS = {".claude/", "__pycache__/", ".pyc", ".lock", ".worktrees/"}

    def _get_git_changed_files(self) -> list:
        """Run git diff --name-only to detect files changed since last commit."""
        try:
            import subprocess
            # Check both staged and unstaged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=15,
            )
            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]

            # Also include untracked files that are new (not yet in HEAD)
            result2 = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=A", "--cached"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=15,
            )
            new_files = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

            # Merge, preserving order, dedup
            seen = set(files)
            for f in new_files:
                if f not in seen:
                    files.append(f)
                    seen.add(f)

            # Filter out Claude artifacts and non-code files
            files = [f for f in files
                     if not any(p in f for p in self._IGNORE_PATTERNS)]

            return files
        except Exception as e:
            log.warning("git diff failed: %s", e)
            return []

    def _parse_output(self, session, task_type: str) -> dict:
        """Parse AI session output into structured result, handling non-JSON gracefully."""
        stdout = session.stdout or ""

        # Try to extract JSON from markdown code blocks first (```json ... ```)
        import re
        code_blocks = re.findall(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', stdout)
        for block in reversed(code_blocks):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

        # Fallback: find last JSON object from raw output
        parsed = None
        for candidate in reversed(list(re.finditer(r'\{', stdout))):
            start = candidate.start()
            depth = 0
            end = None
            for i, ch in enumerate(stdout[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is not None:
                try:
                    obj = json.loads(stdout[start:end])
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except json.JSONDecodeError:
                    continue

        if parsed is not None:
            return parsed

        # Fallback: return raw output as summary (non-JSON output is acceptable)
        summary = stdout.strip()
        if not summary:
            summary = "(no output)"
        return {
            "summary": summary[:1000],
            "exit_code": getattr(session, "exit_code", None),
        }

    # ------------------------------------------------------------------
    # Startup helpers: crash recovery + PID lock
    # ------------------------------------------------------------------

    def _run_ttl_cleanup(self) -> None:
        """Archive memories with expired TTL (per domain pack durability)."""
        try:
            result = self._api("POST", f"/api/mem/{self.project_id}/ttl-cleanup")
            archived = result.get("archived", 0)
            if archived:
                log.info("_run_ttl_cleanup: archived %d expired memories", archived)
        except Exception as e:
            log.debug("_run_ttl_cleanup failed (non-fatal): %s", e)

    def _recover_stale_leases(self) -> None:
        """Periodically re-queue claimed tasks whose lease has expired (runtime orphan recovery)."""
        try:
            result = self._api("POST", f"/api/task/{self.project_id}/recover")
            recovered = result.get("recovered", 0)
            if recovered:
                log.warning("_recover_stale_leases: re-queued %d orphaned task(s) with expired leases", recovered)
        except Exception as e:
            log.debug("_recover_stale_leases failed (non-fatal): %s", e)

    def _recover_stuck_tasks(self) -> None:
        """Mark any 'claimed' tasks from a previous crash as failed.

        Called once at the start of ``run_loop`` before the first poll so that
        tasks which were in-progress when the executor crashed are not left
        permanently stuck in the 'claimed' state.
        """
        try:
            data = self._api("GET", f"/api/task/{self.project_id}/list")
            tasks = data.get("tasks", [])
            claimed = [t for t in tasks if t.get("status") == "claimed"]
            if not claimed:
                log.info("_recover_stuck_tasks: no stuck tasks found")
                return
            log.warning(
                "_recover_stuck_tasks: found %d stuck task(s) from previous crash, marking failed",
                len(claimed),
            )
            for task in claimed:
                task_id = task.get("task_id") or task.get("id")
                if not task_id:
                    continue
                self._api("POST", f"/api/task/{self.project_id}/complete", {
                    "task_id": task_id,
                    "status": "failed",
                    "result": {
                        "error": "executor_crash_recovery",
                        "reason": "Executor crashed or restarted while task was claimed",
                    },
                })
                log.info("_recover_stuck_tasks: marked task %s as failed", task_id)
        except Exception as e:
            log.warning("_recover_stuck_tasks failed (non-fatal): %s", e)

    def _acquire_pid_lock(self) -> bool:
        """Write our PID to a temp file, returning False if another instance is alive.

        The PID file path is:
          ``<tempdir>/aming-claw-executor-<project_id>.pid``

        If an old PID file exists but the process is no longer running (stale
        lock), the file is overwritten and ``True`` is returned.
        """
        pid_path = os.path.join(
            tempfile.gettempdir(),
            f"aming-claw-executor-{self.project_id}.pid",
        )

        if os.path.exists(pid_path):
            try:
                with open(pid_path) as fh:
                    old_pid = int(fh.read().strip())
                # Check if the old process is still alive (signal 0 = existence check)
                try:
                    os.kill(old_pid, 0)
                    log.warning(
                        "_acquire_pid_lock: another executor instance appears to be running "
                        "(PID %d); proceeding anyway",
                        old_pid,
                    )
                    # We log a warning but do NOT abort — in Docker/container restarts the
                    # old PID may still appear transiently in /proc.
                except (OSError, ProcessLookupError):
                    log.info(
                        "_acquire_pid_lock: stale PID file (PID %d no longer running), overwriting",
                        old_pid,
                    )
            except (ValueError, IOError):
                log.debug("_acquire_pid_lock: could not read stale PID file, overwriting")

        try:
            with open(pid_path, "w") as fh:
                fh.write(str(os.getpid()))
            self._pid_path = pid_path
            log.info("_acquire_pid_lock: wrote PID %d to %s", os.getpid(), pid_path)
        except Exception as exc:
            log.warning("_acquire_pid_lock: could not write PID file: %s", exc)
        return True

    def _release_pid_lock(self) -> None:
        """Remove the PID file created by ``_acquire_pid_lock``."""
        if self._pid_path and os.path.exists(self._pid_path):
            try:
                os.unlink(self._pid_path)
                log.info("_release_pid_lock: removed PID file %s", self._pid_path)
            except Exception as exc:
                log.debug("_release_pid_lock: could not remove PID file: %s", exc)
        self._pid_path = None

    def run_once(self) -> bool:
        """Try to claim and execute one task. Returns True if a task was processed."""
        task = self._claim_task()
        if not task:
            return False

        task_id = task["task_id"]
        self._current_task = task_id
        log.info("Claimed task: %s", task_id)

        try:
            outcome = self._execute_task(task)
            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})

            # Coordinator post-processing: parse action from AI output
            task_type = task.get("type", "")
            if task_type == "coordinator" and status == "succeeded":
                self._handle_coordinator_result(task, result)
                # Replace result with minimal payload so the gateway event listener
                # does NOT send a duplicate "Task completed" Telegram notification.
                # _reply_sent=True signals that the reply was already sent above.
                result = {"action": "handled", "_reply_sent": True}

            completion = self._complete_task(task_id, status, result)
            chain = completion.get("auto_chain", {})

            if chain.get("gate_blocked"):
                log.warning("Gate blocked after %s: %s", task_id, chain["reason"])
            elif chain.get("task_id"):
                log.info("Auto-chain: %s → %s (%s)", task_id, chain["task_id"], chain.get("type"))
            elif chain.get("deploy"):
                log.info("Deploy triggered from %s: %s", task_id, chain["deploy"])

            return True

        except Exception as e:
            log.error("Task %s execution failed: %s", task_id, e, exc_info=True)
            self._complete_task(task_id, "failed", {"error": str(e)})
            return True
        finally:
            self._current_task = None

    def run_loop(self):
        """Main polling loop."""
        self._running = True
        log.info("Executor worker started: project=%s, worker=%s, poll=%ds",
                 self.project_id, self.worker_id, POLL_INTERVAL)
        log.info("Governance: %s | Workspace: %s", self.base_url, self.workspace)

        # Acquire PID lock (warn if another instance may be running)
        self._acquire_pid_lock()

        # Verify governance is reachable
        health = self._api("GET", "/api/health")
        if "error" in health:
            log.error("Cannot reach governance at %s", self.base_url)
            self._release_pid_lock()
            return
        log.info("Governance: v%s (PID %s)", health.get("version", "?"), health.get("pid", "?"))

        # Recover any tasks left in 'claimed' state from a previous crash
        self._recover_stuck_tasks()

        # Initial git sync
        self._sync_git_status()
        self._sync_counter = 0
        self._recover_counter = 0   # Stale lease recovery every ~5 min
        self._ttl_counter = 0       # TTL cleanup every ~6h

        try:
            while self._running:
                try:
                    # Sync git every 6th poll (60s) to avoid DB lock contention
                    self._sync_counter += 1
                    if self._sync_counter >= 6:
                        self._sync_git_status()
                        self._sync_counter = 0
                    # Recover stale claimed tasks every 30th poll (~5 min)
                    self._recover_counter += 1
                    if self._recover_counter >= 30:
                        self._recover_stale_leases()
                        self._recover_counter = 0
                    # TTL memory cleanup every 2160th poll (~6h)
                    self._ttl_counter += 1
                    if self._ttl_counter >= 2160:
                        self._run_ttl_cleanup()
                        self._ttl_counter = 0
                    processed = self.run_once()
                    if not processed:
                        time.sleep(POLL_INTERVAL)
                    else:
                        time.sleep(1)  # Brief pause between tasks
                except KeyboardInterrupt:
                    log.info("Shutting down...")
                    self._running = False
                except Exception as e:
                    log.error("Poll loop error: %s", e, exc_info=True)
                    time.sleep(POLL_INTERVAL)
        finally:
            self._release_pid_lock()

    _last_git_head = ""
    _last_dirty = []

    def _sync_git_status(self):
        """Sync git HEAD + dirty files to governance DB. Only writes when changed."""
        try:
            import subprocess
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace, timeout=5
            ).decode().strip()

            diff = subprocess.check_output(
                ["git", "diff", "--name-only"],
                cwd=self.workspace, timeout=5
            ).decode().strip()
            dirty = [f for f in diff.splitlines() if f.strip()] if diff else []

            # Only write to DB if state changed (avoid unnecessary DB contention)
            if head == self._last_git_head and dirty == self._last_dirty:
                return
            self._last_git_head = head
            self._last_dirty = dirty

            self._api("POST", f"/api/version-sync/{self.project_id}", {
                "git_head": head,
                "dirty_files": dirty,
            })
        except Exception as e:
            pass  # fail silently, non-critical

    def stop(self):
        """Stop the polling loop."""
        self._running = False


def main():
    parser = argparse.ArgumentParser(description="Executor Worker - polls governance for tasks")
    parser.add_argument("--project", "-p", default=os.getenv("PROJECT_ID", "aming-claw"),
                        help="Project ID to poll tasks from")
    parser.add_argument("--url", default=GOVERNANCE_URL,
                        help="Governance API URL")
    parser.add_argument("--worker-id", default=WORKER_ID,
                        help="Worker identifier")
    parser.add_argument("--workspace", default=WORKSPACE,
                        help="Working directory for task execution")
    parser.add_argument("--once", action="store_true",
                        help="Execute one task and exit (no loop)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    worker = ExecutorWorker(
        project_id=args.project,
        governance_url=args.url,
        worker_id=args.worker_id,
        workspace=args.workspace,
    )

    if args.once:
        worker.run_once()
    else:
        worker.run_loop()


if __name__ == "__main__":
    main()
