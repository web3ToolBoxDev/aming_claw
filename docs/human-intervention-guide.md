# Human Intervention Guide

## 1. Default Execution Model: Auto-Chain First

**Auto-chain is the DEFAULT.** Every task must first attempt the full automated pipeline before any human intervention is considered.

```
Dev → Gate → Tester → QA → Merge → Deploy
```

Each stage is triggered automatically by the preceding stage's success signal. The Observer's default stance is **hands-off**. Do not interrupt a running chain without a confirmed, specific reason from the criteria in Section 2.

> **Rule:** If you are considering manual intervention, ask yourself: "Has the system already been given a chance to handle this?" If the answer is no, submit via governance API (`POST /api/task/create`) first.

---

## 2. When Manual Intervention Is Permitted

Manual intervention is allowed **only** when one of the following four conditions is confirmed:

| Condition | Description | Trigger Threshold |
|-----------|-------------|-------------------|
| **Self-bootstrap paradox** | The system cannot fix itself because the fix requires the same broken component that needs fixing | Confirmed by 5-Whys (≥3 causal layers) |
| **Architectural decision** | A choice that changes module boundaries, data contracts, or pipeline topology | Any architectural scope change |
| **Credential change** | Password reset, token rotation, OAuth revocation, or any secret that must not pass through the AI pipeline | Any secret/credential operation |
| **Large-scope change** | A single task touches more than 10 files | `changed_files > 10` detected at Gate |

If your situation does not match one of these four conditions, **do not intervene manually.** Re-submit the task through governance API (`POST /api/task/create`) and let the auto-chain resolve it.

---

## 3. Operations Reference

### 3.1 Auto-Chain Handles These (Do Not Intervene)

| Operation | Auto-chain stage | Condition |
|-----------|-----------------|-----------|
| Unit test execution and verify-update | Tester | Test evidence present |
| QA pass | QA | e2e report + artifacts pass |
| Coverage check | Tester / QA | Runs automatically |
| Context save / load | Dev | Automatic |
| Memory writes | Dev | Automatic |
| Non-sensitive message replies | Dev | Automatic |
| Node status transitions | Gate | Signal-driven |
| Merge and deploy | Merge → Deploy | Gate approves |

### 3.2 Requires Human Confirmation Before Chain Continues

| Operation | Reason | How to Confirm |
|-----------|--------|----------------|
| New node / module creation | Architectural decision | Observer reviews proposal via governance API, confirms through `/api/wf/{pid}/verify-update` |
| Bulk baseline state change | Wide blast radius | AI lists change set → human confirms via governance API |
| Cross-project operation | Affects other projects | Notification via telegram_gateway → human confirms via governance API |

### 3.3 Must Be Executed Manually (Human Only)

| Operation | Reason | Method |
|-----------|--------|--------|
| Password / token reset | Security-sensitive | Human runs `init_project.py` |
| `refresh_token` revoke / rotate | Credential operation | Human calls `/api/token/revoke` |
| Release-gate final sign-off | Release decision | Human confirms via governance API (`POST /api/wf/{pid}/release-gate`) |
| Rollback execution | Irreversible, high risk | Human confirms via governance API (`POST /api/wf/{pid}/rollback`) |
| Node / project deletion | Irreversible | Human operates directly |
| Scheduled Task permission grant | Platform restriction | Human clicks "Always allow" |
| **New project registration** | Requires `.aming-claw.yaml` review | Human creates yaml, calls `POST /api/projects/register` |
| **Docker image rebuild** | Affects running services | Human runs `docker compose build` or deploy chain auto-triggers |

---

## 4. Observer SOP

### 4.1 Role

The Observer monitors the auto-chain health. The Observer does **not** drive tasks. The Observer watches:

- Stage transition signals (Dev → Gate → Tester → QA → Merge → Deploy)
- Executor heartbeats and stale-claim counts
- verify-update cycles (`t2_pass` / `t2_fail`)

### 4.2 Decision Flow: Governance API First

```
Issue detected
      │
      ▼
Submit issue description via governance API (POST /api/task/create)
      │
      ▼
Does the auto-chain resolve it within one full cycle?
      │
   YES ╌╌╌╌╌╌ Auto-chain fixed it → no intervention needed
      │
      NO
      ▼
Apply 5-Whys analysis (minimum 3 causal layers)
      │
      ▼
Does the root cause confirm a self-bootstrap paradox?
      │
   NO  ╌╌╌╌╌╌ Resubmit with more context, still no manual intervention
      │
      YES
      ▼
Proceed to 10-Step Manual Fix SOP (Section 4.4)
```

> **Key rule:** Manual intervention is only unlocked **after** the system has been given at least one attempt (via governance API) AND the 5-Whys confirms a self-bootstrap condition. Skipping the governance submission step is a protocol violation.

### 4.3 5-Whys Analysis

Trace at least **3 causal layers** before concluding manual intervention is required.

**Example:**
1. Why is the node stuck in `testing`? → verify-update keeps returning `t2_fail`.
2. Why does verify-update fail? → coverage-check exits non-zero.
3. Why does coverage-check fail? → The test file imports a module missing from the worktree.
4. *(optional)* Why is the module missing? → A prior commit deleted it without updating the import.
5. *(optional)* Why wasn't this caught automatically? → The executor's orphan-cleanup removed the branch before the dev session could push.

If at layer 3 (or deeper) you confirm a **self-bootstrap paradox** — the executor cannot run the fix because the fix must be applied to the executor itself, or the worktree is irrecoverably dirty — proceed to the 10-Step SOP.

### 4.4 10-Step Manual Fix SOP

| Step | Action | Command / Detail |
|------|--------|-----------------|
| 1 | **Stop executor** | `.\scripts\start-executor.ps1 -Takeover` then kill, or send SIGTERM to the executor PID |
| 2 | **Git clean** | `git -C <worktree> clean -fdx` — remove all untracked and generated files |
| 3 | **Declare reason** | Write a one-line reason in `shared-volume/codex-tasks/logs/manual-intervention-<date>.log` — include which 5-Whys layer confirmed the paradox |
| 4 | **Apply minimum fix** | Use Write/Edit tools in the dev AI session — worktree isolation grants write access; change as few files as possible |
| 5 | **Coverage check** | `python -m pytest --cov=agent agent/tests/ -q` — must pass with no regression |
| 6 | **verify-update** | Call the verify-update API with evidence; confirm `t2_pass` returned |
| 7 | **Verify loop** | Confirm the node transitions out of `testing` into `t2_pass`; watch for one full cycle |
| 8 | **Commit** | `git commit -m "manual-fix: <reason> (observer-intervened)"` in the worktree |
| 9 | **Write memory** | Update `MEMORY.md` or the relevant domain pack with the root cause and the fix applied |
| 10 | **Start executor** | `.\scripts\start-executor.ps1` (Windows) or `bash scripts/startup.sh` (Linux/macOS) — **from a terminal, not inside a Claude session** |

### 4.5 Prohibited Actions

| ❌ Do NOT | Reason |
|-----------|--------|
| Intervene without first submitting via governance API | Bypasses the auto-chain; wastes human time on problems the system can self-heal |
| Intervene without completing 5-Whys (≥3 layers) | Root cause unknown; fix will likely recur |
| Start executor from inside an active Claude/dev-AI session | Creates a child process owned by the session; orphaned when session exits |
| `curl` verify-update directly without evidence | Bypasses evidence-validation gate; produces false `t2_pass`, corrupts audit trail |
| Skip E2E verification | Hides regressions; E2E is the only end-to-end proof |
| `git reset --hard` on main without declaring reason | Destroys history; use a worktree branch instead |
| Merge fixes while executor is running | Race condition between executor task-claims and your edits |

---

## 5. Automated Escalation Notifications

When the system detects conditions that **may** require intervention (not confirmed yet), it sends a notification and pauses — it does not request manual action immediately.

### 5.1 Gate Blocked Event

When auto_chain's gate check fails (`gate.blocked` event), the chain pauses, and the `complete_task()` response includes blocking information:

```json
{
  "task_id": "task-xxx",
  "status": "succeeded",
  "auto_chain": {
    "gate_blocked": true,
    "stage": "dev",
    "reason": "Unrelated files modified: ['config.py']"
  }
}
```

**Observer flow for handling gate blocked:**

1. View blocking reason via task list API (`GET /api/task/list?project_id=xxx`)
2. Locate the issue based on `reason` (e.g., PRD missing fields, out-of-scope file modifications, test failures, etc.)
3. After fixing the issue, resubmit the task for that stage (`POST /api/task/create`); auto_chain will resume from that stage

Possible blocking reasons for each gate:

| Gate | Typical Reason |
|------|---------------|
| Post-PM | `PRD missing mandatory fields: ['target_files']` |
| Checkpoint | `No files changed` / `Unrelated files modified: [...]` / `Dev tests failed` |
| T2 Pass | `Tests failed: N failures` |
| QA Pass | `QA did not pass: recommendation=reject` |

### 5.2 Notification Format

```
[Auto-chain paused — governance review requested]
Reason: Detected potential large-scope change (14 files modified)
Task ID: task-20260326-001
Suggested action: Governance evaluates scope; if >10 files confirmed, human approval required.

Approve via: POST /api/wf/{pid}/verify-update with approval evidence
Or reject via: POST /api/wf/{pid}/verify-update with status "failed"
```

### 5.2 Pause-and-Wait Flow

```
Auto-chain detects pause condition
      │
      ▼
Notification sent via telegram_gateway (port 40010)
      │
      ▼
Governance server waits for human decision:
      │
      ├── Human approves via governance API → chain resumes
      ├── Human rejects via governance API  → task cancelled
      └── No response within timeout        → re-notify (max 3 times, then auto-cancel)
```

---

## 6. Acceptance and Verification

### 6.1 Acceptance Decision Table

| Node type | Auto-accept? | Human requirement |
|-----------|-------------|-------------------|
| Has unit tests | ✅ Tester auto | QA auto if e2e_report present |
| Infrastructure change | ✅ Tester auto | QA needs human to confirm service health |
| Scheduled Task behavior | ❌ | Human: verify via governance API → confirm via verify-update |
| UI / Telegram interaction | ❌ | Human: review via executor_api (port 40100) → confirm |
| Security-related (token/auth) | ❌ | Human verification required |

### 6.2 Manual Acceptance Evidence Format

When human verification is required, the verify-update `evidence` must include `manual_verified`:

```json
{
  "type": "e2e_report",
  "producer": "qa-agent",
  "tool": "manual_e2e",
  "summary": {
    "passed": 1,
    "failed": 0,
    "manual_verified": true,
    "verified_by": "human",
    "verification_method": "telegram_message_test",
    "notes": "Sent test message, reply correct, ACK normal, no duplicate."
  }
}
```

### 6.3 Manual Acceptance Flow

```
AI completes implementation
      │
      ▼
AI notifies human via telegram_gateway (port 40010):
  "Node X implemented. Manual acceptance required:
   1. Verify functionality.
   2. Confirm correct behavior.
   3. Submit decision via governance API."
      │
      ▼
Human tests and submits decision
      │
      ├── Approved → Human calls POST /api/wf/{pid}/verify-update
      │               with evidence: { manual_verified: true, verified_by: "human" }
      │
      └── Rejected → Human calls verify-update with status "failed"
                      → AI fixes issue → requests acceptance again
```

---

## 7. Observer API

```
POST /observer/attach
  Body: { "session_id": "<your-session-id>", "scope": "all" | "node:<id>" }
  Response: { "ok": true, "token": "<observe-token>" }

POST /observer/detach
  Body: { "token": "<observe-token>" }
  Response: { "ok": true }

GET  /observer/status
  Headers: Authorization: Bearer <observe-token>
  Response: {
    "executor_pid": 12345,
    "active_tasks": 3,
    "stale_claimed": 0,
    "orphan_count": 0,
    "last_heartbeat": "2026-03-26T10:00:00Z"
  }
```

> Tokens are session-scoped and expire when you call `/observer/detach` or when the executor restarts.

---

## 8. Write/Edit Access in Dev AI Sessions

Dev AI sessions run inside **worktree isolation** — each session receives a dedicated git worktree with its own branch:

- The `Write` and `Edit` tools have full write access within the worktree directory.
- Changes are **isolated** from `main` until explicitly committed and merged.
- The Observer can inspect, diff, or reset a worktree without affecting other sessions.
- After manual intervention, always commit on the worktree branch and open a PR — never push directly to `main` from a manual session.

---

## 9. Summary

```
DEFAULT — auto-chain handles everything:
  Dev → Gate → Tester → QA → Merge → Deploy

Human confirmation required (chain pauses):
  New node/module  |  Bulk baseline change  |  Cross-project operation  |  >10 file change

Human execution required (no auto option):
  Token/credential management  |  Release sign-off  |  Scheduled Task authorization

Manual intervention unlocked only when:
  (1) System was given at least one attempt (via governance API), AND
  (2) 5-Whys (≥3 layers) confirms a self-bootstrap paradox
      OR an architectural / credential / large-scope condition applies
```

## Chain Context & Crash Recovery (Phase 8)

### Automatic Recovery
When governance restarts, `ChainContextStore.recover_from_db()` replays `chain_events` to rebuild all active chain states. No manual intervention needed for normal crashes.

### When Manual Intervention IS Needed
- **Bootstrap paradox**: the chain context module itself needs fixing (self-referential failure)
- **Corrupted chain_events**: events replayed in wrong order or with missing payloads
- **Stale chains**: failed chains are now auto-archived (retry exhausted → archived). Completed chains archived after merge. Only bootstrap paradox or corrupted events need manual cleanup.

### Observer Chain Inspection
Use `GET /api/context-snapshot/{pid}?task_id=XXX&role=coordinator` to see full chain state including all stages, gate reasons, and result summaries.

### Pre-flight Check
Run `GET /api/wf/{pid}/preflight-check` (or MCP tool `preflight_check`) before intervening. Checks system, version, graph, coverage, and queue health. Use `auto_fix=true` to auto-waive orphan nodes and fail stuck tasks.

## Changelog
- 2026-03-28: Batch 1 flow fixes — R1: test/QA gate fail creates dev retry (降级重跑) instead of same-stage escalate; R2: _build_qa_prompt requires exactly qa_pass or reject; M3: dev success writes pattern memory; S1: session_context skips empty session_summary when decisions=0 and messages=0
- 2026-03-28: Pre-flight self-check system, memory promote/register-pack APIs, merge memory write
- 2026-03-28: Chain Context Phase 8 complete: auto-archive failed chains, prompt in task.created events
- 2026-03-28: DB lock fix: auto_chain uses independent connection, guaranteed close via try/finally
- 2026-03-28: M3 skip_doc_check now requires bootstrap_reason; M4 release gate warns on missing nodes
- 2026-03-28: Chain Context crash recovery and observer inspection added
- 2026-03-26: auto_chain.py implementation complete, full pipeline PM→Dev→Test→QA→Merge→Deploy auto-scheduling with gate validation
- 2026-03-26: Old Telegram bot system fully removed (bot_commands, coordinator, executor, and 20 other modules), unified on governance API
