# Aming Claw Architecture v6 — Executor-Driven Architecture

> **⚠ 2026-03-26 Major Change: Old Telegram bot system completely removed.**
> The following 20 agent/ modules have been deleted: bot_commands.py, coordinator.py, executor.py, interactive_menu.py, task_accept.py, service_manager.py, backends.py, config.py, task_state.py, auth.py, model_registry.py, git_rollback.py, workspace.py, workspace_registry.py, workspace_queue.py, parallel_dispatcher.py, project_summary.py, task_retry.py, task_orchestrator.py, approval_manager.py.
>
> Current system uses uniformly:
> - **governance server** (port 40006) — Task registration, workflow, audit
> - **telegram_gateway** (port 40010) — Telegram message routing
> - **executor-gateway** (FastAPI port 8090) — Task execution
> - **executor_api.py** (port 40100) — Monitoring API
>
> Designs referencing old modules in this document are for historical reference only. Actual architecture flow: Gateway → Governance API → Task Registry → executor-gateway.

> Core principle: **Code manages flow, AI manages decisions.** All AI calls controlled by Executor code, AI cannot directly operate the system.
> AI process lifecycle managed by Executor, Coordinator directs Executor to dispatch context and memory.
>
> v6.1 adds PM role: Requirements analysis → PRD → node design → Coordinator orchestration → Dev execution. Complete requirements-to-delivery pipeline.

## 0. Role Overview (v6.1)

```
User Requirements
    ↓
PM (Requirements analysis + PRD + node design)
    ↓ PRD + proposed_nodes
Coordinator (Orchestration + decisions + reply to user)
    ↓ create_dev_task / create_test_task
Dev (Coding) → Tester (Testing) → QA (Acceptance)
    ↓ eval_complete
Coordinator (Evaluate results + archive)
    ↓
User receives reply
```

| Role | Input | Output | Cannot Do |
|------|-------|--------|-----------|
| **PM** | User requirements | PRD + node proposals + effort estimate | Write code, execute commands, create tasks, verify nodes |
| **Coordinator** | PRD + project status | Task orchestration + reply to user | Write code, requirements analysis |
| **Dev** | Task prompt | Code changes + tests | Converse with user, create tasks |
| **Tester** | Code changes | Test report + verify(testing/t2_pass) | Modify code, create tasks |
| **QA** | Test results | Acceptance decision + verify(qa_pass) | Modify code, run tests |

## 1. v5.1 → v6 Core Changes

| Dimension | v5.1 | v6 | Reason |
|-----------|------|----|--------|
| AI invocation | Gateway/AI calls CLI itself | **Executor unified CLI calls** | Code control = verifiable, interceptable, retryable |
| AI output | Free text + direct API operations | **Structured decision JSON** | Code parseable, verifiable |
| Task creation | Coordinator AI calls API to create | **Coordinator outputs decisions → Executor code creates** | AI does not directly operate system |
| AI lifecycle | No unified management | **Executor AI Lifecycle Manager** | Unified start/monitor/kill/reclaim |
| Result evaluation | Gateway sends directly to user | **Executor auto-creates Coordinator eval** | Decision closed loop |
| Memory archival | Manual | **Auto-triggered on task completion** | No omissions |
| Authorization check | verify_loop post-hoc check | **Executor code real-time interception** | Proactive defense |

## 2. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Human User (Telegram)                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Messages
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │
                    └──────┬──────────────┘
                           │
              ┌────────────▼──────────────────────┐
              │       Telegram Gateway             │
              │       (Docker, message send/receive)│
              │                                    │
              │  Responsibility: message send/receive only │
              │  Non-command message → write task file     │
              │  Receive notification → send Telegram      │
              └────────────┬──────────────────────┘
                           │ task files (shared-volume)
─ ─ ─ ─ ─ ─ Docker ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
                           │
              ┌────────────▼──────────────────────────────────────┐
              │            Executor (host machine, resident)          │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         AI Lifecycle Manager                  │  │
              │  │                                              │  │
              │  │  Manage all AI processes:                       │  │
              │  │    create_session(role, context, prompt)      │  │
              │  │    monitor_session(pid, timeout)              │  │
              │  │    kill_session(pid)                          │  │
              │  │    collect_output(pid) → structured JSON       │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         Decision Validator                    │  │
              │  │                                              │  │
              │  │  Validate every action in AI output:            │  │
              │  │    check_permission(role, action_type)        │  │
              │  │    check_node_exists(node_id)                 │  │
              │  │    check_coverage(target_files)               │  │
              │  │    check_tool_policy(command)                 │  │
              │  │    verify_evidence(evidence)                  │  │
              │  │    validate_workflow_step(current → next)     │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         Task Orchestrator                     │  │
              │  │                                              │  │
              │  │  Task state machine + auto-trigger chain:       │  │
              │  │    user_message → coordinator                 │  │
              │  │    coordinator_decision → validate → execute  │  │
              │  │    dev_complete → coordinator_eval             │  │
              │  │    eval_complete → archive + notify            │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  │
              │  │Coord AI│  │Dev AI  │  │Test AI │  │QA AI   │  │
              │  │(Decide)│  │(Code)  │  │(Test)  │  │(Accept)│  │
              │  └────────┘  └────────┘  └────────┘  └────────┘  │
              └───────────────────────────────────────────────────┘
                           │
              ┌────────────▼──────────────────────┐
              │  Governance Service (Docker)        │
              │  Rule engine + state + audit          │
              └────────────────────────────────────┘
              ┌────────────────────────────────────┐
              │  dbservice (Docker) — Memory layer   │
              └────────────────────────────────────┘
              ┌────────────────────────────────────┐
              │  Redis (Docker) — Cache/queue/notify │
              └────────────────────────────────────┘
```

## 3. Core Component Design

### 3.1 AI Lifecycle Manager

Manages startup, monitoring, and reclamation of all AI processes. **AI cannot start AI by itself.**

```python
class AILifecycleManager:
    """Manage all AI processes. Code-controlled, AI cannot self-start."""

    def __init__(self):
        self.sessions: dict[str, AISession] = {}

    def create_session(
        self,
        role: str,           # coordinator / dev / tester / qa
        prompt: str,         # AI input
        context: dict,       # Injected context
        project_id: str,
        timeout_sec: int = 120,
        output_format: str = "structured",  # structured / freeform
    ) -> AISession:
        """Start an AI CLI process.

        - Assemble system prompt (role + context + output format requirements)
        - Start claude CLI subprocess
        - Record PID to sessions table
        - Return AISession object
        """

    def wait_for_output(self, session_id: str) -> AIOutput:
        """Wait for AI completion, collect stdout → parse into structured output."""

    def kill_session(self, session_id: str, reason: str) -> None:
        """Force terminate AI process. Record reason to audit."""

    def cleanup_orphans(self) -> int:
        """Scan all sessions, kill timed-out/no-heartbeat ones."""

    def list_active(self) -> list[AISession]:
        """List all active AI processes."""
```

```python
@dataclass
class AISession:
    session_id: str
    role: str           # coordinator / dev / tester / qa
    pid: int            # OS process ID
    project_id: str
    prompt: str
    context: dict
    started_at: str
    timeout_sec: int
    status: str         # running / completed / failed / killed
    output: AIOutput | None
```

### 3.2 AI Structured Output

**AI does not output free-form operations, only structured decision JSON.**

#### Coordinator Output Format

```json
{
  "reply": "User-visible reply text",
  "actions": [
    {
      "type": "create_dev_task",
      "prompt": "Fix deploy-governance.sh port from 40006 to 40000",
      "target_files": ["deploy-governance.sh"],
      "related_nodes": ["L13.5"],
      "priority": 1
    },
    {
      "type": "create_test_task",
      "prompt": "Test if pre-deploy-check passes after port fix",
      "depends_on": "After previous dev_task completes"
    },
    {
      "type": "query_governance",
      "endpoint": "/api/wf/amingClaw/summary"
    },
    {
      "type": "update_context",
      "current_focus": "Fix deployment port configuration",
      "decisions": ["Ports should be unified through nginx:40000"]
    }
  ],
  "needs_human_confirm": false,
  "confidence": "high"
}
```

#### Dev Output Format

```json
{
  "summary": "Fixed port configuration in deploy-governance.sh",
  "changed_files": ["deploy-governance.sh"],
  "test_results": {
    "ran": true,
    "passed": 15,
    "failed": 0,
    "command": "python -m pytest tests/"
  },
  "git_diff_summary": "+3 -3 lines in 1 file",
  "needs_review": false,
  "related_nodes": ["L13.5"]
}
```

#### Tester Output Format

```json
{
  "test_report": {
    "total": 1038,
    "passed": 1038,
    "failed": 0,
    "skipped": 0,
    "duration_sec": 37
  },
  "coverage_affected_nodes": ["L13.5"],
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {"passed": 1038, "failed": 0}
  },
  "recommendation": "pass"
}
```

### 3.3 Decision Validator

**Code validates every AI decision in real-time, rejects illegal ones and has AI re-analyze.**

```python
class DecisionValidator:
    """Validate every action in AI output. Code-enforced, not relying on AI self-discipline."""

    def validate(self, role: str, output: AIOutput, project_id: str) -> ValidationResult:
        """
        Returns:
            ValidationResult with:
                - approved_actions: list[Action]  — Passed validation
                - rejected_actions: list[{action, reason}]  — Rejected
                - needs_retry: bool  — Whether AI needs to re-analyze
        """
        results = ValidationResult()

        for action in output.actions:
            # 1. Role permission check
            if not self._check_role_permission(role, action.type):
                results.reject(action, f"{role} not authorized for {action.type}")
                continue

            # 2. Node existence check
            if action.related_nodes:
                for node in action.related_nodes:
                    if not self._node_exists(node, project_id):
                        results.reject(action, f"Node {node} does not exist")
                        continue

            # 3. File coverage check
            if action.target_files:
                uncovered = self._coverage_check(action.target_files, project_id)
                if uncovered:
                    results.reject(action, f"Files without node coverage: {uncovered}")
                    continue

            # 4. Tool policy check
            if action.type in ("run_command", "execute_script"):
                policy = self._check_tool_policy(action.command)
                if policy == "deny":
                    results.reject(action, f"Command forbidden by policy: {action.command}")
                    continue
                if policy == "approval":
                    results.reject(action, f"Command requires human confirmation: {action.command}")
                    continue

            # 5. Workflow step check
            if action.type == "verify_update":
                if not self._validate_transition(action):
                    results.reject(action, "Invalid state transition")
                    continue

            results.approve(action)

        return results
```

#### Role Permission Matrix

```python
ROLE_PERMISSIONS = {
    "coordinator": {
        "allowed": [
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "query_governance",
            "update_context",
            "reply_user",
            "archive_memory",
        ],
        "denied": [
            "modify_code",      # Coordinator does not modify code
            "run_tests",        # Coordinator does not run tests
            "verify_update",    # Coordinator does not directly verify nodes
            "release_gate",     # Needs to go through Executor code execution
        ],
    },
    "dev": {
        "allowed": [
            "modify_code",
            "run_tests",
            "git_diff",
            "read_file",
        ],
        "denied": [
            "create_dev_task",  # Dev cannot assign work to others
            "reply_user",       # Dev does not converse with users
            "release_gate",
            "delete_node",
        ],
    },
    "tester": {
        "allowed": [
            "run_tests",
            "read_file",
            "verify_update",    # testing / t2_pass
        ],
        "denied": [
            "modify_code",
            "reply_user",
            "create_dev_task",
        ],
    },
    "qa": {
        "allowed": [
            "verify_update",    # qa_pass
            "read_file",
            "query_governance",
        ],
        "denied": [
            "modify_code",
            "run_tests",
            "create_dev_task",
        ],
    },
}
```

### 3.4 Task Orchestrator

**Code controls task flow. Auto-trigger chain, does not rely on AI remembering what to do next.**

```python
class TaskOrchestrator:
    """Task orchestration. Code-driven, AI does not control flow."""

    def handle_user_message(self, chat_id: int, text: str, project_id: str):
        """User message → auto-create coordinator session."""
        # 1. Assemble context
        context = self.assemble_context(project_id, chat_id)

        # 2. Start Coordinator AI
        session = self.ai_manager.create_session(
            role="coordinator",
            prompt=text,
            context=context,
            project_id=project_id,
            output_format="structured",
        )

        # 3. Wait for output
        output = self.ai_manager.wait_for_output(session.session_id)

        # 4. Validate decisions
        validation = self.validator.validate("coordinator", output, project_id)

        # 5. Process results
        if validation.rejected_actions:
            # Some actions Rejected → have AI re-analyze
            retry_prompt = self._build_retry_prompt(output, validation)
            retry_session = self.ai_manager.create_session(
                role="coordinator",
                prompt=retry_prompt,
                context=context,
                project_id=project_id,
            )
            output = self.ai_manager.wait_for_output(retry_session.session_id)
            validation = self.validator.validate("coordinator", output, project_id)

        # 6. Execute approved actions
        for action in validation.approved_actions:
            self._execute_action(action, project_id)

        # 7. Send reply
        self.gateway_reply(chat_id, output.reply)

        # 8. Update context
        self._update_context(project_id, chat_id, text, output)

    def handle_dev_complete(self, task_id: str, dev_output: AIOutput, project_id: str):
        """Dev completes → auto-create Coordinator eval."""
        chat_id = self._get_task_chat_id(task_id)

        # 1. Code validates dev output
        dev_validation = self.validator.validate("dev", dev_output, project_id)

        if dev_validation.has_critical_issues:
            # Dev output has issues → auto-retry
            self._retry_dev_task(task_id, dev_validation.issues)
            return

        # 2. Assemble eval context
        eval_context = self.assemble_context(project_id, chat_id)
        eval_context["dev_result"] = {
            "task_id": task_id,
            "summary": dev_output.summary,
            "changed_files": dev_output.changed_files,
            "test_results": dev_output.test_results,
            "validation": dev_validation.summary,
        }

        # 3. Start Coordinator eval session
        eval_session = self.ai_manager.create_session(
            role="coordinator",
            prompt="Dev task completed, please evaluate results and decide next steps",
            context=eval_context,
            project_id=project_id,
        )

        eval_output = self.ai_manager.wait_for_output(eval_session.session_id)

        # 4. Validate + execute
        eval_validation = self.validator.validate("coordinator", eval_output, project_id)
        for action in eval_validation.approved_actions:
            self._execute_action(action, project_id)

        # 5. Reply to user
        self.gateway_reply(chat_id, eval_output.reply)

        # 6. Auto-archive
        self._auto_archive(project_id, task_id, dev_output, eval_output)

    def _auto_archive(self, project_id, task_id, dev_output, eval_output):
        """Auto-archive memory and context after task completion."""
        # Archive conversation history to session_context
        self.context_service.archive_if_expired(project_id)

        # Extract valuable decisions and write to long-term memory
        if eval_output.context_update and eval_output.context_update.get("decisions"):
            for decision in eval_output.context_update["decisions"]:
                self.memory_service.write({
                    "type": "decision",
                    "content": decision,
                    "scope": project_id,
                    "source_task": task_id,
                })

        # Record dev change summary
        if dev_output.changed_files:
            self.memory_service.write({
                "type": "workaround" if "fix" in dev_output.summary.lower() else "pattern",
                "content": dev_output.summary,
                "scope": project_id,
                "related_files": dev_output.changed_files,
            })
```

### 3.5 Context Assembly

**Executor code assembles AI context, AI does not pull data itself.**

```python
class ContextAssembler:
    """Assemble context for each AI session. Code-controlled, ensures consistency."""

    def assemble(self, project_id: str, chat_id: int, role: str) -> dict:
        """Assemble different granularity context based on role."""

        context = {}

        # 1. Conversation history (Coordinator needs)
        if role == "coordinator":
            history = self.context_service.load(project_id)
            context["conversation_history"] = history.get("recent_messages", [])[-10:]
            context["current_focus"] = history.get("current_focus", "")

        # 2. Project status (all roles need)
        summary = self.gov_api(f"/api/wf/{project_id}/summary")
        context["project_status"] = summary

        # 3. Active tasks (Coordinator needs)
        if role == "coordinator":
            runtime = self.gov_api(f"/api/runtime/{project_id}")
            context["active_tasks"] = runtime.get("active_tasks", [])
            context["queued_tasks"] = runtime.get("queued_tasks", [])

        # 4. Related memories (all roles as needed)
        memories = self.dbservice.search(project_id, role=role, limit=5)
        context["memories"] = memories

        # 5. Role-specific context
        if role == "dev":
            # Dev needs workspace path, git status
            context["workspace"] = str(self.workspace_path)
            context["git_status"] = self._get_git_status()

        if role == "tester":
            # Tester needs test command, coverage scope
            context["test_command"] = self.config.get("test_command", "pytest")
            context["affected_nodes"] = self._get_affected_nodes(project_id)

        return context
```

## 4. Complete Message Flow

### 4.1 User Message → Coordinator Conversation

```
User: "Help me fix the deployment port bug"
    │
    ▼
Gateway: write coordinator_chat task file (atomic)
    │
    ▼
Executor TaskOrchestrator.handle_user_message():
    │
    ├── 1. ContextAssembler assembles context
    │     - Conversation history (last 10)
    │     - Project status (104 nodes, all qa_pass)
    │     - Active tasks (0 running)
    │     - Related memories (deployment-related pitfall)
    │
    ├── 2. AILifecycleManager.create_session("coordinator", ...)
    │     → Start claude CLI, PID=12345
    │     → Wait for output
    │
    ├── 3. AI outputs structured decisions:
    │     {
    │       "reply": "Let me analyze the deployment port issue...",
    │       "actions": [
    │         {"type": "create_dev_task", "prompt": "Fix port", "target_files": ["deploy-governance.sh"]}
    │       ]
    │     }
    │
    ├── 4. DecisionValidator.validate("coordinator", output):
    │     ✅ create_dev_task — coordinator has permission
    │     ✅ target_files coverage — deploy-governance.sh has L13.5 coverage
    │
    ├── 5. Execute approved actions:
    │     → Create dev_task file
    │     → Update Task Registry
    │
    ├── 6. Gateway replies to user: "Let me analyze the deployment port issue..."
    │
    └── 7. Update context:
          → Save user message + coordinator reply
          → Update current_focus
```

### 4.2 Dev Completes → Coordinator Evaluation → Archive

```
Executor executes dev_task:
    │
    ├── AILifecycleManager.create_session("dev", ...)
    │     → claude CLI modifies code
    │
    ├── AI outputs:
    │     {"summary": "Modified port", "changed_files": ["deploy.sh"], "test_results": {...}}
    │
    ├── DecisionValidator.validate("dev", output):
    │     ✅ Modified files within allowed scope
    │     ✅ Tests passed
    │
    ▼
TaskOrchestrator.handle_dev_complete():  ← Code auto-triggered
    │
    ├── 1. Assemble eval context (with dev results)
    │
    ├── 2. AILifecycleManager.create_session("coordinator", eval_prompt)
    │     → Coordinator evaluates dev results
    │
    ├── 3. Coordinator output:
    │     {
    │       "reply": "Port fixed, tests passed. Recommend deployment.",
    │       "actions": [
    │         {"type": "update_context", "decisions": ["Unify ports through nginx:40000"]}
    │       ]
    │     }
    │
    ├── 4. Reply to user
    │
    └── 5. Auto-archive:
          → session_context: save conversation
          → dbservice: write decision "Unify ports through nginx:40000"
          → dbservice: write change summary
```

### 4.3 Validation Failure → Auto-Retry

```
Coordinator output:
  {
    "actions": [
      {"type": "modify_code", "file": "server.py"}  ← Unauthorized!
    ]
  }
    │
    ▼
DecisionValidator:
  ❌ coordinator not authorized for modify_code
    │
    ▼
TaskOrchestrator:
  Build retry prompt:
    "Your previous decisions were rejected:
     - modify_code: coordinator not authorized to modify code directly
     Please re-analyze, use create_dev_task to have dev role execute"
    │
    ▼
AILifecycleManager.create_session("coordinator", retry_prompt)
    │
    ▼
Coordinator re-outputs:
  {
    "actions": [
      {"type": "create_dev_task", "prompt": "Modify server.py ..."}
    ]
  }
  → ✅ Passed
```

## 5. File Structure

> **Note (2026-03-26):** Old modules like executor.py, backends.py, task_orchestrator.py have been deleted. The following is historical design reference. Current actual structure is primarily governance/ and telegram_gateway/.

```
agent/                             # ⚠ Modules marked [deleted] below were removed on 2026-03-26
├── executor.py                    # [deleted] Original orchestrator entry point
├── ai_lifecycle.py                # NEW: AI Lifecycle Manager
├── decision_validator.py          # NEW: Decision validator
├── task_orchestrator.py           # [deleted] Original task orchestrator
├── context_assembler.py           # NEW: Context assembler
├── ai_output_parser.py            # NEW: AI output parser (JSON extraction)
├── role_permissions.py            # NEW: Role permission matrix
├── backends.py                    # [deleted] Original run_claude / run_codex
├── telegram_gateway/
│   └── gateway.py                 # Retained: Telegram message routing (:40010)
└── governance/
    ├── server.py                  # Retained: Rule engine (:40006)
    ├── task_registry.py           # Retained
    ├── session_context.py         # Retained
    └── ...
```

## 6. Coordinator System Prompt Template

```
You are the Coordinator for project {project_id}.

## Your Responsibilities
1. Understand user intent
2. Answer questions
3. If code modification is needed, output create_dev_task action
4. If confirmation is needed, ask the user

## Current Context
{context_json}

## Output Format (strict JSON)
You must output the following JSON format, no other content:

```json
{
  "reply": "Reply text for the user",
  "actions": [
    {
      "type": "create_dev_task | create_test_task | query_governance | update_context | reply_only",
      "prompt": "Task description (if task)",
      "target_files": ["File path"],
      "related_nodes": ["L node ID"]
    }
  ],
  "context_update": {
    "current_focus": "Current work focus",
    "decisions": ["Decisions made"]
  }
}
```

## Constraints
- You cannot directly modify code, must use create_dev_task to have dev role execute
- You cannot directly call APIs, only output query_governance action
- All your actions will be validated by code, unauthorized ones will be rejected
- If you are unsure, use reply_only to ask the user
```

## 7. Implementation Roadmap

### P0 — Core Framework (Must Complete First)

| Step | Content | Dependencies |
|------|------|------|
| 1 | ai_lifecycle.py: AILifecycleManager | None |
| 2 | ai_output_parser.py: Extract JSON from claude stdout | None |
| 3 | role_permissions.py: Role permission matrix | None |
| 4 | decision_validator.py: Validation logic | 2, 3 |
| 5 | context_assembler.py: Context assembly | None |
| 6 | task_orchestrator.py: handle_user_message | 1, 4, 5 |

### P1 — Closed-Loop Pipeline

| Step | Content | Dependencies |
|------|------|------|
| 7 | task_orchestrator: handle_dev_complete | 6 |
| 8 | Coordinator eval auto-trigger | 7 |
| 9 | Auto-retry (validation failure → re-analyze) | 4, 6 |
| 10 | Conversation history persistence (cross-message context) | 5 |
| 11 | Auto-archive (memory + context) | 7 |

### P2 — Enhancements

| Step | Content | Dependencies |
|------|------|------|
| 12 | Multi-role parallel (dev + tester simultaneously) | 6 |
| 13 | Task dependency chain (auto-run tester after dev completes) | 7 |
| 14 | Human confirmation flow (dangerous ops → Telegram confirm) | 6 |
| 15 | Memory-assisted decisions (inject pitfall into context) | 5 |
| 16 | Observability (trace_id across full chain) | 6 |

## 8. Compatibility with Existing System

> **2026-03-26 update:** Old Executor run loop, process_task, process_coordinator_chat and other components removed with module deletion. Current task execution handled by executor-gateway (port 8090).

| Existing Component | Status (2026-03-26) |
|---------|---------------|
| Gateway message send/receive (telegram_gateway) | **Retained** — Telegram message routing (:40010) |
| Gateway message classifier | **Deleted** — Removed with old system |
| Gateway handle_task_dispatch | **Deleted** — Removed with old system |
| Executor run loop (executor.py) | **Deleted** — Replaced by executor-gateway (:8090) |
| Executor process_task | **Deleted** — Replaced by executor-gateway |
| process_coordinator_chat | **Deleted** — Replaced by executor-gateway |
| Governance API | **Retained** — Rule engine (:40006) |
| Task Registry | **Retained** — Managed by Governance |
| Session Context | **Retained** — Called by ContextAssembler |
| dbservice | **Retained** — Called by auto-archive |
| verify_loop | **Retained** — As supplementary post-hoc check |

## 9. Security Boundary

```
Hard constraints (code-enforced, AI cannot bypass):

1. AI cannot start AI
   → AILifecycleManager is the only entry point
   → AI actions are just "requests", code decides whether to execute

2. AI cannot directly operate the system
   → All API calls executed by code
   → AI outputs JSON, code parses then operates

3. Role permissions cannot be exceeded
   → role_permissions.py hardcoded
   → DecisionValidator checks every action

4. Unauthorized auto-retry
   → Rejected actions fed back to AI with reasons
   → AI must re-decide within permission scope
   → Max 3 retries, then human intervention

5. Task flow controlled by code
   → dev completes → code auto-creates eval
   → Does not rely on AI remembering to call API
   → Does not rely on AI remembering to archive memory
```

## 10. Acceptance Graph Integration — Executor-Aware DAG

### 10.1 Why Executor Must Be Aware of Acceptance Graph

The acceptance graph is the rule core of the entire workflow. Before v5.1, AI queried the acceptance graph via API, then "voluntarily" followed rules.
This violates v6's core principle — **rules are code-enforced, not relying on AI self-discipline**.

```
v5.1:  AI queries acceptance graph → AI "voluntarily" follows → often skips steps
v6:    Executor code proactively fetches acceptance graph → encoded in validation logic → AI cannot bypass
```

### 10.2 Acceptance Graph Data Fetched by Executor

Executor fetches and caches acceptance graph snapshot from Governance at startup and periodically (every 60s):

```python
class GraphAwareValidator:
    """Acceptance graph-aware validator. Executor code enforces graph constraints."""

    def __init__(self):
        self._graph_cache = None
        self._cache_ts = 0
        self._cache_ttl = 60  # 60s refresh

    def refresh_graph(self, project_id: str):
        """Fetch acceptance graph snapshot from Governance."""
        self._graph_cache = gov_api("GET", f"/api/wf/{project_id}/export?format=json")
        self._cache_ts = time.time()
        # Cache structure:
        # {
        #   "nodes": {
        #     "L13.5": {
        #       "verify_status": "qa_pass",
        #       "deps": ["L13.1", "L13.2"],
        #       "gate_mode": "explicit",
        #       "gates": [{"node": "L13.1", "min_status": "qa_pass"}],
        #       "primary": ["deploy-governance.sh"],
        #       "secondary": ["scripts/pre-deploy-check.sh"],
        #       "artifacts": [{"type": "api_docs", "section": "deployment"}],
        #       "verify": "L4",
        #     }
        #   },
        #   "edges": [...],
        # }
```

### 10.3 Acceptance Graph Constraints Enforced in Executor

#### Constraint 1: File Modifications Must Have Node Coverage

```python
def check_file_coverage(self, changed_files: list[str], project_id: str) -> list[str]:
    """Check if all files AI wants to modify have node coverage.

    Returns: List of uncovered files (empty = all covered)
    """
    graph = self._get_graph(project_id)
    covered_files = set()
    for node_id, node in graph["nodes"].items():
        covered_files.update(node.get("primary", []))
        covered_files.update(node.get("secondary", []))

    uncovered = []
    for f in changed_files:
        if not any(f.endswith(cf) or cf.endswith(f) for cf in covered_files):
            uncovered.append(f)
    return uncovered
```

**Trigger timing**: When Dev AI outputs changed_files, Executor code checks.

```python
# TaskOrchestrator.handle_dev_complete()
uncovered = self.graph_validator.check_file_coverage(
    dev_output.changed_files, project_id
)
if uncovered:
    # Reject dev results, let Coordinator decide whether to create nodes
    self._reject_dev_output(task_id, f"Files without node coverage: {uncovered}")
    return
```

#### Constraint 2: Node Dependencies Must Be Satisfied

```python
def check_node_deps_satisfied(self, node_id: str, project_id: str) -> list[str]:
    """Check if all upstream dependencies of a node have passed.

    Returns: List of unsatisfied dependency nodes
    """
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return [f"node {node_id} not found"]

    unsatisfied = []
    for dep in node.get("deps", []):
        dep_node = graph["nodes"].get(dep)
        if not dep_node:
            unsatisfied.append(f"{dep}: not found")
        elif dep_node.get("verify_status") not in ("t2_pass", "qa_pass"):
            unsatisfied.append(f"{dep}: {dep_node.get('verify_status', 'unknown')}")
    return unsatisfied
```

**Trigger timing**: When Coordinator AI outputs verify_update action.

```python
# DecisionValidator.validate()
if action.type == "verify_update":
    unsatisfied = self.graph_validator.check_node_deps_satisfied(
        action.node_id, project_id
    )
    if unsatisfied:
        results.reject(action, f"Upstream dependencies not satisfied: {unsatisfied}")
```

#### Constraint 3: Gate Policy Check

```python
def check_gate_policy(self, node_id: str, target_status: str, project_id: str) -> bool:
    """Check if node gate policy allows advancing to target status."""
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return False

    gates = node.get("gates", [])
    for gate in gates:
        gate_node_id = gate.get("node")
        min_status = gate.get("min_status", "qa_pass")
        gate_node = graph["nodes"].get(gate_node_id)
        if not gate_node:
            return False
        actual_status = gate_node.get("verify_status", "pending")
        if not self._status_gte(actual_status, min_status):
            return False
    return True
```

#### Constraint 4: Role Verification Level Limits

```python
def check_role_verify_level(self, role: str, target_status: str) -> bool:
    """Check if role is authorized to advance to target status.

    tester: pending → testing → t2_pass
    qa:     t2_pass → qa_pass
    coordinator: cannot directly verify
    """
    ROLE_VERIFY_LIMITS = {
        "tester": {"testing", "t2_pass"},
        "qa": {"qa_pass"},
        "coordinator": set(),  # coordinator cannot directly verify
        "dev": set(),          # dev cannot verify
    }
    allowed = ROLE_VERIFY_LIMITS.get(role, set())
    return target_status in allowed
```

#### Constraint 5: Artifacts Completeness Check

```python
def check_artifacts_complete(self, node_id: str, project_id: str) -> list[str]:
    """Check if node artifacts constraints are satisfied.

    Returns: List of missing artifacts
    """
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return []

    missing = []
    for artifact in node.get("artifacts", []):
        if artifact["type"] == "api_docs":
            section = artifact.get("section", "")
            docs = gov_api("GET", f"/api/docs/{section}")
            if "error" in docs:
                missing.append(f"api_docs:{section}")
    return missing
```

**Trigger timing**: Auto-check when advancing to qa_pass.

#### Constraint 6: New Files Must Have Nodes First

```python
def check_new_files_have_nodes(self, dev_output, project_id: str) -> list[str]:
    """Check if files newly created by dev have corresponding nodes.

    Existing file modifications → check coverage
    New file creation → must have node first (or Coordinator proposes creation)
    """
    graph = self._get_graph(project_id)
    all_tracked_files = set()
    for node in graph["nodes"].values():
        all_tracked_files.update(node.get("primary", []))
        all_tracked_files.update(node.get("secondary", []))

    new_untracked = []
    for f in dev_output.get("new_files", []):
        if f not in all_tracked_files:
            new_untracked.append(f)
    return new_untracked
```

### 10.4 Acceptance Graph Modification Permissions — Executor Controlled

**AI cannot directly modify acceptance graph. All graph changes validated by Executor code.**

```python
# Coordinator AI output (request, not direct operation)
{
    "actions": [
        {
            "type": "propose_node",
            "node": {
                "id": "L15.1",
                "title": "New feature X",
                "deps": ["L14.1"],
                "primary": ["agent/new_module.py"],
                "verify": "L4",
                "description": "..."
            },
            "reason": "dev needs to create new file agent/new_module.py, currently no node coverage"
        },
        {
            "type": "propose_node_update",
            "node_id": "L13.5",
            "changes": {
                "secondary": {"add": ["scripts/new_helper.sh"]}
            },
            "reason": "New helper script needs tracking"
        }
    ]
}
```

```python
class GraphModificationValidator:
    """Acceptance graph modification validation. Code-enforced, AI cannot directly modify graph."""

    def validate_propose_node(self, proposal: dict, project_id: str) -> ValidationResult:
        """Validate new node proposal."""
        node = proposal["node"]
        result = ValidationResult()

        # 1. ID format check
        if not re.match(r"^L\d+\.\d+$", node["id"]):
            result.reject("Invalid ID format, should be L{layer}.{index}")
            return result

        # 2. ID uniqueness
        graph = self._get_graph(project_id)
        if node["id"] in graph["nodes"]:
            result.reject(f"Node {node['id']} already exists")
            return result

        # 3. Dependency existence
        for dep in node.get("deps", []):
            if dep not in graph["nodes"]:
                result.reject(f"Dependency {dep} does not exist")
                return result

        # 4. Cycle detection
        if self._would_create_cycle(graph, node["id"], node.get("deps", [])):
            result.reject("Would create circular dependency")
            return result

        # 5. Primary file path format check
        for f in node.get("primary", []):
            if ".." in f or f.startswith("/"):
                result.reject(f"Unsafe file path: {f}")
                return result

        result.approve()
        return result

    def validate_propose_update(self, update: dict, project_id: str) -> ValidationResult:
        """Validate node update proposal."""
        node_id = update["node_id"]
        graph = self._get_graph(project_id)

        if node_id not in graph["nodes"]:
            return ValidationResult(rejected=True, reason=f"Node {node_id} does not exist")

        # Only allow modifying secondary and description
        # primary / deps / gates modification requires human confirmation
        changes = update.get("changes", {})
        sensitive_fields = {"primary", "deps", "gates", "gate_mode", "verify"}
        for field in changes:
            if field in sensitive_fields:
                return ValidationResult(
                    rejected=True,
                    reason=f"Modifying {field} requires human confirmation",
                    needs_human_confirm=True
                )

        return ValidationResult(approved=True)
```

#### Execution Flow

```
Coordinator AI: {"type": "propose_node", "node": {...}}
    │
    ▼
Executor GraphModificationValidator:
    ├── ID format ✅
    ├── Uniqueness ✅
    ├── Dependencies exist ✅
    ├── No cycles ✅
    └── Path safe ✅
    │
    ▼ Passed
Executor code calls Governance API:
    POST /api/wf/{project_id}/import-graph (or node-create)
    │
    ▼
Acceptance graph update complete → audit record:
    {
        "action": "node_created",
        "node_id": "L15.1",
        "proposed_by": "coordinator_session_xxx",
        "validated_by": "executor_code",
        "timestamp": "..."
    }
```

### 10.5 Acceptance Graph Constraints Overview — Executor Code Execution Matrix

| Constraint | Trigger Timing | Check Content | Failure Handling |
|------|---------|---------|---------|
| File coverage | Dev AI outputs changed_files | Each file has corresponding node | Reject dev results → Coordinator decides to create node |
| Dependencies satisfied | verify_update action | All upstream nodes passed | Reject → tell AI which dependencies unsatisfied |
| Gate policy | verify_update to qa_pass | All gate conditions satisfied | Reject → list unsatisfied gates |
| Role verify level | verify_update action | Role permission matches target status | Reject → tell AI who has permission |
| Artifacts complete | Advancing to qa_pass | Docs/tests/evidence complete | Reject → list missing artifacts |
| New file needs node | Dev creates new file | New file has corresponding node | Pause → let Coordinator propose_node |
| Node creation validation | propose_node action | ID/dependencies/no cycles/path safe | Reject → reason fed back to AI |
| Node modification validation | propose_node_update | Sensitive fields need human confirmation | Needs confirmation → Telegram notifies human |
| Coverage new code | After Dev completes | All changed files have coverage | Reject → let Coordinator analyze |

### 10.6 Acceptance Graph Checks Throughout Dev Task Lifecycle

```
Coordinator creates dev_task:
    │
    ├── Executor checks:
    │   ✓ target_files all have node coverage?
    │   ✓ Corresponding node status allows modification? (pending/testing, cannot modify qa_pass)
    │   ✓ Node dependencies satisfied?
    │
    ▼ Passed → Start Dev AI
    │
Dev AI executes:
    │ ... modifies code ...
    │
    ▼ Completes
    │
    ├── Executor checks dev output:
    │   ✓ changed_files all have node coverage?
    │   ✓ No unrelated files modified?
    │   ✓ Newly created files have nodes?
    │   ✓ Tests passed?
    │
    ├── If issues found:
    │   → Build retry prompt (with rejection reasons)
    │   → Restart Dev AI (max 3 times)
    │
    ▼ Passed → Start Coordinator eval
    │
Coordinator eval:
    │
    ├── Executor checks eval output:
    │   ✓ No authorization overstepping (Coordinator cannot verify itself)
    │   ✓ If verify_update action → check role+dependencies+gate
    │   ✓ If propose_node → check ID/dependencies/no cycles
    │
    ▼ Passed → Execute actions + reply to user + auto-archive
```

## 11. Evidence Collection — Executor Independently Collects, Does Not Trust AI Self-Report

### 11.1 Core Principle

AI output falls into two categories, Executor treats them differently:

| Category | Source | Trust Level | Examples |
|------|------|--------|------|
| **Decision** | AI generated | Needs validation before execution | create_dev_task, reply, update_context |
| **Evidence** | **Executor independently collected** | Trustworthy | changed_files, test_results, git_diff |

```python
class EvidenceCollector:
    """Executor independently collects factual evidence, not relying on AI self-report."""

    def collect_after_dev(self, workspace: Path, before_snapshot: dict) -> DevEvidence:
        """After Dev AI execution, code independently collects actual results."""

        # 1. Actual changed_files — collected from git diff, not trusting AI report
        changed = subprocess.run(
            ["git", "diff", "--name-only", before_snapshot["commit"]],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip().splitlines()

        # 2. Actual new_files — collected from git status
        new_files = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip().splitlines()

        # 3. Actual test_results — collected from pytest/junit report
        test_result = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            capture_output=True, text=True, cwd=workspace,
            timeout=300
        )
        test_evidence = {
            "exit_code": test_result.returncode,
            "stdout": test_result.stdout[-2000:],
            "passed": test_result.returncode == 0,
        }

        # 4. Actual diff statistics
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", before_snapshot["commit"]],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip()

        return DevEvidence(
            changed_files=changed,
            new_files=new_files,
            test_results=test_evidence,
            diff_stat=diff_stat,
            collected_by="executor_code",
            collected_at=utc_iso(),
        )
```

### 11.2 AI Self-Report vs Executor Collection Comparison

```
After Dev AI completes:

AI self-report:                   Executor independent collection:
  changed_files: ["a.py"]          git diff: ["a.py", "b.py"]  ← Also modified b.py!
  test_results: {passed: 10}       pytest exit_code: 1          ← Tests actually failed!
  summary: "Fix complete"           diff_stat: "+50 -3"          ← Actual statistics

→ Executor uses independent collection as authoritative
→ AI self-report only used as reference for Coordinator eval
→ Inconsistencies recorded to audit
```

## 12. Task State Machine — Explicit Definition

### 12.1 Status Enum

```python
class TaskStatus(str, Enum):
    # Creation state
    CREATED = "created"
    QUEUED = "queued"

    # Execution state
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    WAITING_HUMAN = "waiting_human"
    BLOCKED_BY_DEP = "blocked_by_dep"

    # Terminal state
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"

    # Evaluation state
    EVAL_PENDING = "eval_pending"        # Awaiting Coordinator eval
    EVAL_APPROVED = "eval_approved"      # Coordinator confirmed pass
    EVAL_REJECTED = "eval_rejected"      # Coordinator requires redo

    # Notification state
    NOTIFY_PENDING = "notify_pending"
    NOTIFIED = "notified"

    # Archive state
    ARCHIVED = "archived"
```

### 12.2 State Transition Rules

```python
VALID_TRANSITIONS = {
    "created":           {"queued", "cancelled"},
    "queued":            {"claimed", "cancelled"},
    "claimed":           {"running", "queued"},           # claim failure can revert
    "running":           {"succeeded", "failed_retryable", "failed_terminal", "cancelled"},
    "waiting_retry":     {"queued"},                      # Re-queue
    "waiting_human":     {"queued", "cancelled"},         # Human decides
    "blocked_by_dep":    {"queued"},                      # Resume after dependency satisfied
    "succeeded":         {"eval_pending"},                # Auto-trigger eval
    "failed_retryable":  {"waiting_retry", "failed_terminal"},
    "failed_terminal":   {"notify_pending", "archived"},
    "eval_pending":      {"eval_approved", "eval_rejected"},
    "eval_approved":     {"notify_pending"},
    "eval_rejected":     {"queued"},                      # Re-execute
    "notify_pending":    {"notified"},
    "notified":          {"archived"},
    "cancelled":         {"archived"},
}
```

### 12.3 Task Extended Fields

```python
@dataclass
class Task:
    task_id: str
    task_type: str           # coordinator_chat / dev_task / test_task / qa_task
    status: TaskStatus
    project_id: str
    prompt: str

    # Scheduling
    attempt: int = 0
    max_attempts: int = 3
    priority: int = 0
    parent_task_id: str = ""  # Parent task (eval's parent is dev_task)

    # Lease
    lease_owner: str = ""
    lease_expire_at: str = ""

    # Tracking
    trace_id: str = ""
    idempotency_key: str = ""
    schema_version: str = "v1"

    # Evidence (Executor independently collected)
    evidence: dict = field(default_factory=dict)

    # AI decisions (needs validation)
    ai_decision: dict = field(default_factory=dict)

    # Timeline
    created_at: str = ""
    claimed_at: str = ""
    completed_at: str = ""
    archived_at: str = ""
```

## 13. Validator Layers

```
┌───────────────────────────────────────┐
│ Layer 1: SchemaValidator              │
│   JSON format, schema_version, required fields │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 2: PolicyValidator              │
│   Role permissions, tool policy, dangerous op detection │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 3: GraphValidator               │
│   Node existence, dependencies met, gate, coverage │
│   Graph version consistency (version/etag CAS)    │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 4: ExecutionPreconditionValidator│
│   Workspace available, file exists, lease valid    │
│   Concurrency conflict detection, resource limits  │
└───────────────────────────────────────┘
```

Each layer independently returns `{layer, passed, errors[]}`, enabling precise audit of which layer blocked.

## 14. Error Classification and Retry Strategy

```python
class ErrorCategory(str, Enum):
    RETRYABLE_MODEL = "retryable_model"      # JSON parse failure, AI output format error
    RETRYABLE_ENV = "retryable_env"          # Network timeout, filesystem temporary error
    BLOCKED_BY_DEP = "blocked_by_dep"        # Graph dependency not satisfied
    NON_RETRYABLE_POLICY = "non_retryable"   # Permission denied, command deny
    NEEDS_HUMAN = "needs_human"              # Sensitive operation needs confirmation

RETRY_STRATEGY = {
    "retryable_model":     {"max_retries": 3, "backoff": "immediate", "action": "rebuild_prompt"},
    "retryable_env":       {"max_retries": 2, "backoff": "exponential", "action": "retry_same"},
    "blocked_by_dep":      {"max_retries": 0, "backoff": None, "action": "set_blocked_status"},
    "non_retryable":       {"max_retries": 0, "backoff": None, "action": "fail_terminal"},
    "needs_human":         {"max_retries": 0, "backoff": None, "action": "create_approval"},
}
```

## 15. Memory Write Governance

```python
class MemoryWriteGuard:
    """Governance check before memory write. Prevent polluting long-term memory."""

    def should_write(self, entry: dict, project_id: str) -> tuple[bool, str]:
        # 1. Dedup — check if highly similar memory already exists
        existing = self.dbservice.search(entry["content"][:100], scope=project_id, limit=3)
        for e in existing:
            if self._similarity(e["doc"]["content"], entry["content"]) > 0.85:
                return False, "duplicate"

        # 2. Source check — only qa_pass decisions can write to long-term memory
        if entry.get("type") == "decision":
            source_node = entry.get("related_node")
            if source_node:
                node = self.gov_api(f"/api/wf/{project_id}/node/{source_node}")
                if node.get("verify_status") != "qa_pass":
                    return False, "node_not_qa_pass"

        # 3. Confidence — below threshold is not written
        if entry.get("confidence", 1.0) < 0.6:
            return False, "low_confidence"

        # 4. TTL — workaround type auto-set 30 day expiry
        if entry.get("type") == "workaround":
            entry.setdefault("ttl_days", 30)

        return True, "ok"
```

## 16. Context Budget and Determinism

```python
CONTEXT_BUDGET = {
    "coordinator": {
        "hard_context": 3000,      # task, node, files, current status
        "conversation": 3000,      # recent 10 messages
        "memory": 1500,            # top-3 related
        "runtime": 500,            # active/queued tasks
        "total_max": 8000,
    },
    "dev": {
        "hard_context": 2000,      # task prompt, target files, workspace
        "memory": 1500,            # related pitfalls
        "git_context": 500,        # git status, recent commits
        "total_max": 4000,
    },
    "tester": {
        "hard_context": 1500,      # test command, affected nodes
        "memory": 1000,            # test patterns
        "total_max": 3000,
    },
    "qa": {
        "hard_context": 1500,      # review scope
        "memory": 1000,            # qa criteria
        "total_max": 3000,
    },
}
```

Context assembly strictly truncates by budget, ensuring determinism:

```python
def assemble(self, project_id, chat_id, role):
    budget = CONTEXT_BUDGET[role]
    context = {}

    # Fill by priority, truncate when over budget
    used = 0
    for layer in ["hard_context", "conversation", "memory", "runtime", "git_context"]:
        layer_budget = budget.get(layer, 0)
        if layer_budget == 0:
            continue
        data = self._fetch_layer(layer, project_id, chat_id, role)
        truncated = self._truncate_to_tokens(data, layer_budget)
        context[layer] = truncated
        used += self._count_tokens(truncated)
        if used >= budget["total_max"]:
            break

    return context
```

## 17. Graph Version Consistency (CAS)

```python
class GraphAwareValidator:
    def _get_graph(self, project_id):
        """Graph cache with version number."""
        now = time.time()
        if self._graph_cache and now - self._cache_ts < self._cache_ttl:
            return self._graph_cache

        result = gov_api("GET", f"/api/wf/{project_id}/export?format=json")
        self._graph_cache = result
        self._graph_version = result.get("version", 0)
        self._cache_ts = now
        return result

    def validate_with_cas(self, action, project_id):
        """Validate with graph version, compare-and-swap on execution."""
        graph = self._get_graph(project_id)
        validate_version = self._graph_version

        # ... validation logic ...

        # Check version unchanged at execution time
        current_version = gov_api("GET", f"/api/wf/{project_id}/summary").get("version", 0)
        if current_version != validate_version:
            # Graph was modified during validation → refresh and retry
            self._graph_cache = None
            return ValidationResult(rejected=True, reason="graph_version_conflict", retryable=True)

        return validation_result
```

## 18. Relationship with v5.1

v6 is not a rewrite, but adds a **code control layer** on top of v5.1:

```
v5.1:  Gateway → [AI free operation] → Results
v6:    Gateway → [Executor code] → [AI structured output] → [4-layer validation + independent evidence collection + graph CAS] → Results

What is added is the middle code control layer + acceptance graph integration + evidence collection + state machine, without changing underlying services.
```

## 19. Implementation Roadmap (Final)

### P0 — Core Framework + Foundation Reinforcement

| Step | Content | Dependencies | Source |
|------|------|------|------|
| 1 | ai_lifecycle.py: AILifecycleManager | None | Original design |
| 2 | ai_output_parser.py: JSON extraction + schema_version | None | Original design + Review #5 |
| 3 | role_permissions.py: Role permission matrix | None | Original design |
| 4 | graph_validator.py: Acceptance graph constraints + version CAS | Gov API | Original design + Review #7 |
| 5 | evidence_collector.py: Executor independent collection | None | Review #2 |
| 6 | task_state_machine.py: Explicit status enum + transition rules | None | Review #4 |
| 7 | decision_validator.py: 4-layer validation | 2,3,4 | Original design + Review #6 |
| 8 | context_assembler.py: Budget-based Context assembly | None | Original design + Review #10 |
| 9 | task_orchestrator.py: handle_user_message | 1,7,8 | Original design |

### P1 — Closed-Loop + Reliability

| Step | Content | Dependencies | Source |
|------|------|------|------|
| 10 | handle_dev_complete + independent evidence validation | 5,9 | Original design + Review #2 |
| 11 | Coordinator eval auto-trigger | 10 | Original design |
| 12 | Error classification retry strategy | 7,9 | Review #8 |
| 13 | Conversation history persistence | 8 | Original design |
| 14 | Memory write governance (dedup/confidence/TTL) | dbservice | Review #9 |
| 15 | Auto-archive (memory + context) | 10,14 | Original design |
| 16 | propose_node validation | 4 | Original design |
| 17 | task file → DB+Redis driven | 6 | Review #1 |

### P2 — Enhancements

| Step | Content | Dependencies | Source |
|------|------|------|------|
| 18 | Execution sandbox (isolated workspace/command allowlist) | 9 | Review #3 |
| 19 | Multi-role parallel | 9 | Original design |
| 20 | Task dependency chain (dev→tester→qa auto) | 10 | Original design |
| 21 | Human approval objects (approval_id/scope) | 9 | Review #12 |
| 22 | Plan layer (request→plan→task) | 9 | Review #11 |
| 23 | Observability (trace_id + replay) | 9 | Review #13 |

### Review Feedback Adoption Summary

| Review # | Suggestion | Adopted | Section |
|-------|------|------|---------|
| 1 | task file → DB+Redis | ✅ P1 #17 | Section 12 State machine |
| 2 | Independent evidence collection | ✅ P0 #5 | Section 11 Evidence collection |
| 3 | Execution sandbox | ✅ P2 #18 | Deferred (single machine sufficient for now) |
| 4 | Explicit state machine | ✅ P0 #6 | Section 12 State machine |
| 5 | schema_version | ✅ P0 #2 | Section 12.3 Task fields |
| 6 | Validator layer split | ✅ P0 #7 | Section 13 Layered validation |
| 7 | graph CAS | ✅ P0 #4 | Section 17 Graph version consistency |
| 8 | Error classification retry | ✅ P1 #12 | Section 14 Retry strategy |
| 9 | Memory write governance | ✅ P1 #14 | Section 15 Memory governance |
| 10 | context budget | ✅ P0 #8 | Section 16 Budget and determinism |
| 11 | Plan layer | ✅ P2 #22 | Deferred |
| 12 | Approval objects | ✅ P2 #21 | Deferred |
| 13 | replay/audit chain | ✅ P2 #23 | Deferred |

## 20. v6.2 Implementation Supplement

### 20.1 Git Worktree Isolation (Implemented)

Dev AI no longer uses `git checkout -b` to create branches in the main working directory, switched to `git worktree add` for working in an isolated directory:

```
Main working directory: C:\Users\z5866\Documents\amingclaw\aming_claw\  (main branch, observer operations)
Worktree:   .worktrees/dev-task-xxx/                         (Dev branch, AI operations)
```

Flow:
1. `git worktree add -b dev/task-xxx .worktrees/dev-task-xxx` — Create isolated directory
2. Dev AI executes all operations within worktree directory
3. After completion `git worktree remove .worktrees/dev-task-xxx --force` — Cleanup
4. Branch retained for review and merge

Benefit: Observer and Executor can operate in parallel without file loss due to branch switching.

### 20.2 Chain Depth Limit (Implemented)

Prevent Dev→eval→Dev→eval infinite loops. TaskOrchestrator maintains a `_chain_depth` counter:

```
MAX_CHAIN_DEPTH = 4

handle_user_message()   → depth = 1
_trigger_coordinator_eval() → depth = 2
handle_test_complete()  → depth = 3
handle_qa_complete()    → depth = 4
Trigger again → Reject: "chain depth exceeded, stopping"
```

Reset to 0 each time `handle_user_message()` is called from Gateway.

### 20.3 Trace/Replay API (Implemented)

Executor API (:40100) added two read-only endpoints:

| Endpoint | Description |
|------|------|
| `GET /trace/{trace_id}` | Returns complete trace chain (all events from task creation to completion) |
| `GET /traces?project_id=amingClaw&limit=20` | Lists recent traces, supports filtering by project_id |

Data source: JSON files in `shared-volume/codex-tasks/processing/` and `results/`.

## Version Gate

Prevents manual code changes from bypassing the auto-chain workflow.

### Architecture

```
Host machine (git source of truth):
  executor_worker._sync_git_status() [each poll cycle]
    → git rev-parse HEAD
    → git diff --name-only
    │
    ▼
  POST /api/version-sync/{pid}
    → governance DB (git_head, dirty_files, git_synced_at)

Docker (no git):
  gateway → GET /api/version-check/{pid}
    → reads DB: git_head vs chain_version
    → HEAD ≠ CHAIN → block, reply to user
    → dirty files → block, reply to user

Auto-chain merge (_execute_merge):
  1. Update VERSION file (CHAIN_VERSION=new_hash)
  2. git commit --amend (include VERSION)
  3. POST /api/version-update (DB chain_version = new_hash)
  4. POST /api/version-sync (git_head = new_hash)
  → All aligned → next message passes gate
```

### Deploy Chain

After merge, `deploy_chain.run_deploy()` detects affected services and rebuilds:

| Service | Trigger files | Action |
|---------|--------------|--------|
| governance | `agent/governance/*` | `docker compose build governance` + `up -d` |
| gateway | `agent/telegram_gateway/*` | `docker compose build telegram-gateway` + `up -d` |
| executor | `agent/executor_worker.py`, `agent/ai_lifecycle.py` | ServiceManager reload |

Both governance and gateway do `build + up` (not just `restart`), ensuring code changes are deployed.

### Anti-tamper

VERSION file warns AI agents not to edit manually. Even if they do, the commit changes HEAD → no longer matches the value written → gate still blocks.

## Gate Retry

When a gate blocks, auto_chain creates a retry task at the same stage:

```
dev complete → checkpoint gate BLOCKS (reason: "docs not updated")
  → auto-create new dev task with prompt:
    "Previous attempt blocked: docs not updated. Fix and retry."
  → executor claims retry task → AI sees the gate reason → fixes
  → checkpoint gate passes → chain continues to test
```

Retry depth limited by `MAX_CHAIN_DEPTH` (10). Set `_no_retry: true` in metadata to disable.

## Changelog
- 2026-03-27: Gate retry mechanism (auto-retry on block with reason injection)
- 2026-03-27: Version gate, context snapshot, structured memory, role prompts with API reference
- 2026-03-26: Old Telegram bot system completely removed, unified to governance API
