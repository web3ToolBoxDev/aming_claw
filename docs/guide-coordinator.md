# Coordinator Agent Guide

**Role:** Orchestrate workflows, assign roles, manage release gate.
**Cannot do:** Modify code directly, run tests.

---

## Setup

The Coordinator token is obtained by the human running `init_project.py`. Inject via environment variable.

```
Header: X-Gov-Token: gov-<coordinator-token>
Header: Content-Type: application/json
```

Send heartbeat every 60s:
```
POST /api/role/heartbeat
Body: {"project_id": "<pid>", "status": "idle"}
```

---

## Coordinator API Reference

| Operation | Method | Path |
|-----------|--------|------|
| Heartbeat | POST | `/api/role/heartbeat` |
| Assign role | POST | `/api/role/assign` |
| Revoke role | POST | `/api/role/revoke` |
| View team | GET | `/api/role/{pid}/sessions` |
| Import graph | POST | `/api/wf/{pid}/import-graph` |
| Update status | POST | `/api/wf/{pid}/verify-update` |
| Release gate | POST | `/api/wf/{pid}/release-gate` |
| Rollback | POST | `/api/wf/{pid}/rollback` |
| Export graph | GET | `/api/wf/{pid}/export?format=mermaid` |
| Pre-flight check | GET | `/api/wf/{pid}/preflight-check` |
| Impact analysis | GET | `/api/wf/{pid}/impact?files=a.js` |
| Promote memory | POST | `/api/mem/{pid}/promote` |
| Register domain pack | POST | `/api/mem/{pid}/register-pack` |
| Context snapshot | GET | `/api/context-snapshot/{pid}?task_id=X&role=coordinator` |

---

## Assign Role

```json
POST /api/role/assign
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "principal_id": "tester-001",
  "role": "tester",
  "scope": ["L0.*", "L1.*", "L2.*"]
}
```

Response contains the agent's token — distribute it to the corresponding agent.

## Revoke Role

```json
POST /api/role/revoke
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "session_id": "ses-xxx"
}
```

---

## Release Gate

```json
POST /api/wf/{pid}/release-gate

{
  "scope": ["L3.*", "L4.*"],
  "profile": "browser-core"
}
```

- `200` = ready to release
- `403` = blockers exist (response includes checklist)

---

## Rollback

```json
POST /api/wf/{pid}/rollback

{
  "snapshot_id": "snap-xxx",
  "reason": "Production rollback due to regression"
}
```

---

## Coordinator Release Workflow

```
1. GET  /api/role/{pid}/sessions           ← Confirm team is in place
2. GET  /api/wf/{pid}/summary              ← Confirm node statuses
3. GET  /api/wf/{pid}/preflight-check      ← System health check
4. POST /api/wf/{pid}/release-gate         ← Release gate check
   Body: {scope: ["L3.*", "L4.*"]}
5. If 403 → view blockers → assign corresponding role to handle
6. If 200 → ready to release
```

---

## Pre-flight Self-Check

Run before starting a chain or investigating issues:

```
GET /api/wf/{pid}/preflight-check
GET /api/wf/{pid}/preflight-check?auto_fix=true
```

| Check | What it validates |
|-------|-------------------|
| `system` | DB accessible, required tables exist |
| `version` | chain_version == git_head, sync freshness |
| `graph` | No orphan pending nodes without active tasks |
| `coverage` | All governance/*.py files in CODE_DOC_MAP |
| `queue` | No stuck claimed tasks (>30min), no circular retries |

With `auto_fix=true`: waives orphan nodes, marks stuck tasks as failed.

---

## Chain Context Inspection

```
GET /api/context-snapshot/{pid}?task_id=XXX&role=coordinator
```

Coordinator sees all stages, all result fields. Use to diagnose stuck chains.

Failed chains are auto-archived after retry exhaustion (no manual cleanup needed unless bootstrap paradox).

---

## Memory Management

### Promote Memory (Cross-Project)

```json
POST /api/mem/{pid}/promote
{"memory_id": "mem-012", "target_scope": "global", "reason": "Applicable to all projects"}
```

Promotable kinds: `failure_pattern`, `architecture`, `pattern`, `rule`, `decision`, `knowledge`.

### Register Domain Pack

```json
POST /api/mem/{pid}/register-pack
{"domain": "development", "types": {"architecture": {"durability": "permanent", "conflictPolicy": "replace"}}}
```

---

## Waive a Node (Skip Verification)

```json
POST /api/wf/{pid}/verify-update

{
  "nodes": ["L3.7"],
  "status": "waived",
  "evidence": {
    "type": "manual_review",
    "summary": {"reason": "Infra node, no code change"}
  }
}
```

---

## When Governance Is Unreachable

| Operation | Behavior |
|-----------|----------|
| verify-update | Block and wait (max 120s) |
| release-gate | Block and wait — cannot be skipped |
| mem/write | Cache locally, push when recovered |
