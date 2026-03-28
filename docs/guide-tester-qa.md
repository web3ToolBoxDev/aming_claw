# Tester / QA Agent Guide

**Tester role:** Run T1+T2 tests, mark pending → T2-pass.
**QA role:** Run E2E tests, mark T2-pass → QA-pass.
**Cannot do:** Assign roles, mark T2-pass as QA (or skip T2).

---

## Setup

```
Header: X-Gov-Token: gov-<your-token>
Header: Content-Type: application/json
```

Send heartbeat every 60s:
```
POST /api/role/heartbeat
Body: {"project_id": "<pid>", "status": "idle"}
```

---

## State Transition Diagram

```
  PENDING ──→ TESTING ──→ T2_PASS ──→ QA_PASS
    │  ↑         │           │           │
    │  │         ↓           ↓           ↓
    │  └───── FAILED ←───────┘───────────┘
    │
    └──→ WAIVED (coordinator only)

  Forbidden: PENDING → QA_PASS (cannot skip T2)
```

---

## Tester: Mark T2-pass

```json
POST /api/wf/{pid}/verify-update
Header: X-Gov-Token: gov-<tester-token>
Header: Idempotency-Key: tester-001-L0.1-t2-20260322

{
  "nodes": ["L0.1", "L0.2"],
  "status": "t2_pass",
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {
      "passed": 162,
      "failed": 0,
      "exit_code": 0
    },
    "artifact_uri": "logs/test-run-20260322.json"
  }
}
```

## QA: Mark QA-pass

```json
POST /api/wf/{pid}/verify-update
Header: X-Gov-Token: gov-<qa-token>

{
  "nodes": ["L0.1"],
  "status": "qa_pass",
  "evidence": {
    "type": "e2e_report",
    "tool": "playwright",
    "summary": {
      "passed": 14,
      "failed": 0
    },
    "artifact_uri": "test/main-flow.spec.js"
  }
}
```

## Mark Failed (any role)

```json
POST /api/wf/{pid}/verify-update

{
  "nodes": ["L3.7"],
  "status": "failed",
  "evidence": {
    "type": "error_log",
    "summary": {"error": "Search timeout after 180s"},
    "artifact_uri": "logs/error-20260322.log"
  }
}
```

---

## Evidence Requirements

| Transition | Evidence Type | Required Fields |
|------------|--------------|-----------------|
| pending → t2_pass | `test_report` | `summary.passed > 0`, `summary.exit_code == 0` |
| t2_pass → qa_pass | `e2e_report` | `summary.passed > 0` |
| * → failed | `error_log` | `summary.error` or `artifact_uri` |
| failed → pending | `commit_ref` | `summary.commit_hash` (7-40 hex chars) |
| pending → waived | `manual_review` | No structural requirements (coordinator only) |

---

## Manual Acceptance (UI/Scheduled Task nodes)

When a node requires human verification:
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

---

## Typical Tester Workflow

```
1. GET  /api/wf/{pid}/summary              ← See which nodes are pending
2. GET  /api/mem/{pid}/query?kind=failure_pattern   ← Check known failures
3. (Run tests)
4. POST /api/wf/{pid}/verify-update        ← Mark T2-pass or failed
```

## Typical QA Workflow

```
1. GET  /api/wf/{pid}/summary              ← Confirm T2-pass nodes
2. (Run E2E tests / manual verification)
3. POST /api/wf/{pid}/verify-update        ← Mark QA-pass or failed
```

---

## API Reference

| Operation | Method | Path |
|-----------|--------|------|
| Heartbeat | POST | `/api/role/heartbeat` |
| View summary | GET | `/api/wf/{pid}/summary` |
| View node | GET | `/api/wf/{pid}/node/{nid}` |
| Mark T2-pass (Tester) | POST | `/api/wf/{pid}/verify-update` |
| Mark QA-pass (QA) | POST | `/api/wf/{pid}/verify-update` |
| Mark failed | POST | `/api/wf/{pid}/verify-update` |
| Query memory | GET | `/api/mem/{pid}/query?kind=failure_pattern` |

---

## Error Reference

| HTTP Status | Error Code | Action |
|-------------|-----------|--------|
| 400 `invalid_evidence` | Evidence fields wrong | Check evidence type + summary |
| 403 `gate_unsatisfied` | Upstream not passed | Ensure upstream nodes are T2-pass first |
| 403 `forbidden_transition` | Illegal state change | Cannot skip T2; cannot QA without T2-pass |
| 403 `scope_violation` | Out of scope | Contact Coordinator |

---

## When Governance Is Unreachable

| Operation | Behavior |
|-----------|----------|
| verify-update | Block and wait (max 120s) — do NOT mark status manually |
| mem/query | Return empty, do not block work |
