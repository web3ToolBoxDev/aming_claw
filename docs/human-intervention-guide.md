# Human Intervention Guide

## 1. Default Execution Model: Auto-Chain First

**Auto-chain is the DEFAULT.** Every task must first attempt the full automated pipeline before any human intervention is considered.

```
Dev → Gate → Tester → QA → Merge → Deploy
```

Each stage is triggered automatically by the preceding stage's success signal. The Observer's default stance is **hands-off**. Do not interrupt a running chain without a confirmed, specific reason from the criteria in Section 2.

> **Rule:** If you are considering manual intervention, ask yourself: "Has the coordinator already been given a chance to handle this?" If the answer is no, submit via the coordinator first.

---

## 2. When Manual Intervention Is Permitted

Manual intervention is allowed **only** when one of the following four conditions is confirmed:

| Condition | Description | Trigger Threshold |
|-----------|-------------|-------------------|
| **Self-bootstrap paradox** | The system cannot fix itself because the fix requires the same broken component that needs fixing | Confirmed by 5-Whys (≥3 causal layers) |
| **Architectural decision** | A choice that changes module boundaries, data contracts, or pipeline topology | Any architectural scope change |
| **Credential change** | Password reset, token rotation, OAuth revocation, or any secret that must not pass through the AI pipeline | Any secret/credential operation |
| **Large-scope change** | A single task touches more than 10 files | `changed_files > 10` detected at Gate |

If your situation does not match one of these four conditions, **do not intervene manually.** Re-submit the task through the coordinator and let the auto-chain resolve it.

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
| New node / module creation | Architectural decision | Observer reviews proposal in Telegram, replies "confirm" |
| Bulk baseline state change | Wide blast radius | AI lists change set → human confirms in Telegram |
| Cross-project operation | Affects other projects | Telegram notification → human confirms |

### 3.3 Must Be Executed Manually (Human Only)

| Operation | Reason | Method |
|-----------|--------|--------|
| Password / token reset | Security-sensitive | Human runs `init_project.py` |
| `refresh_token` revoke / rotate | Credential operation | Human calls `/api/token/revoke` |
| Release-gate final sign-off | Release decision | Human confirms in Telegram |
| Rollback execution | Irreversible, high risk | Human confirms in Telegram |
| Node / project deletion | Irreversible | Human operates directly |
| Scheduled Task permission grant | Platform restriction | Human clicks "Always allow" |

---

## 4. Observer SOP

### 4.1 Role

The Observer monitors the auto-chain health. The Observer does **not** drive tasks. The Observer watches:

- Stage transition signals (Dev → Gate → Tester → QA → Merge → Deploy)
- Executor heartbeats and stale-claim counts
- verify-update cycles (`t2_pass` / `t2_fail`)

### 4.2 Decision Flow: Coordinator First

```
Issue detected
      │
      ▼
Submit issue description to coordinator via normal task submission
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

> **Key rule:** Manual intervention is only unlocked **after** the coordinator has been given at least one attempt AND the 5-Whys confirms a self-bootstrap condition. Skipping the coordinator step is a protocol violation.

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
| Intervene without first submitting to coordinator | Bypasses the auto-chain; wastes human time on problems the system can self-heal |
| Intervene without completing 5-Whys (≥3 layers) | Root cause unknown; fix will likely recur |
| Start executor from inside an active Claude/dev-AI session | Creates a child process owned by the session; orphaned when session exits |
| `curl` verify-update directly without evidence | Bypasses evidence-validation gate; produces false `t2_pass`, corrupts audit trail |
| Skip E2E verification | Hides regressions; E2E is the only end-to-end proof |
| `git reset --hard` on main without declaring reason | Destroys history; use a worktree branch instead |
| Merge fixes while executor is running | Race condition between executor task-claims and your edits |

---

## 5. Automated Escalation Notifications

When the system detects conditions that **may** require intervention (not confirmed yet), it sends a notification and pauses — it does not request manual action immediately.

### 5.1 Notification Format

```
[Auto-chain paused — coordinator review requested]
Reason: Detected potential large-scope change (14 files modified)
Task ID: task-20260326-001
Suggested action: Coordinator evaluates scope; if >10 files confirmed, human approval required.

Reply "approve-large-scope" to continue, or "reject" to cancel.
```

### 5.2 Pause-and-Wait Flow

```
Auto-chain detects pause condition
      │
      ▼
Telegram notification sent (not ACKed — stays in queue)
      │
      ▼
Next scheduled Task trigger:
  Check queue → find un-ACKed message → check for human reply
      │
      ├── Human replies "approve-*" → chain resumes → ACK both messages
      ├── Human replies "reject"    → task cancelled → ACK both messages
      └── No reply                  → re-notify (max 3 times, then auto-cancel)
```

---

## 6. Acceptance and Verification

### 6.1 Acceptance Decision Table

| Node type | Auto-accept? | Human requirement |
|-----------|-------------|-------------------|
| Has unit tests | ✅ Tester auto | QA auto if e2e_report present |
| Infrastructure change | ✅ Tester auto | QA needs human to confirm service health |
| Scheduled Task behavior | ❌ | Human: send message → observe reply → confirm ACK |
| UI / Telegram interaction | ❌ | Human: review screenshot → confirm |
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
AI notifies human via Telegram:
  "Node X implemented. Manual acceptance required:
   1. Send a test message in Telegram.
   2. Wait 1 minute and check for reply.
   3. Confirm reply content is correct.
   4. Confirm no duplicate messages.
   5. Reply 'accepted' or 'rejected: <reason>'."
      │
      ▼
Human tests and replies
      │
      ├── "accepted"        → AI submits verify-update (qa_pass)
      │                        evidence: { manual_verified: true, verified_by: "human" }
      │
      └── "rejected: <reason>" → AI fixes issue → requests acceptance again
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
  (1) Coordinator was given at least one attempt, AND
  (2) 5-Whys (≥3 layers) confirms a self-bootstrap paradox
      OR an architectural / credential / large-scope condition applies
```
