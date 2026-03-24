# Production Deploy Guard

## Why

Without deploy guard, anyone (human or AI) can run `docker compose up` directly,
bypassing all safety checks (E2E, gatekeeper, pre-deploy). This has caused:

- Deploying code with failing E2E tests
- Skipping gatekeeper release-gate
- Missing coverage checks
- "Small problems" being deployed to production

## How It Works

Three layers of protection:

### Layer 1: Claude Code Hooks

A pre-tool-use hook intercepts Bash commands containing `docker compose up/down/restart/build`
and blocks them with a message to use `deploy.sh` instead.

### Layer 2: deploy.sh (Mandatory Entry Point)

`scripts/deploy.sh` is the **only** way to deploy. It runs these checks in order:

1. **E2E Verify** (`scripts/e2e-verify.sh`) — Tests all API endpoints + context continuity
2. **Pre-Deploy Check** (`scripts/pre-deploy-check.sh`) — Nodes, coverage, docs, memory, gatekeeper
3. **Release Gate** (Governance API) — Final approval

If any check fails, deployment is **blocked** and the failure reason is printed.

### Layer 3: Gatekeeper Integration

E2E results are recorded in the gatekeeper database (`check_type='e2e'`).
The release-gate reads these results. Stale or failed E2E blocks the release.

## Installation

### Linux / macOS

```bash
bash scripts/setup-guard.sh
```

### Windows (PowerShell)

```powershell
.\scripts\setup-guard.ps1
```

### What Gets Installed

| File | Purpose |
|------|---------|
| `scripts/check-deploy-guard.sh` | Hook script that blocks direct docker compose |
| `.claude/settings.json` | Claude Code hook configuration |
| `CLAUDE.md` (appended) | Human-readable deploy prohibition |

## Usage

### Deploy to Production

```bash
bash scripts/deploy.sh
```

This will:
1. Run E2E verification (all endpoints + context test)
2. Run pre-deploy checks (nodes, coverage, docs, memory)
3. Check release gate (gatekeeper must pass)
4. If all pass: `docker compose up -d --build`
5. Post-deploy health check
6. Sync dev environment data

### Check Without Deploying

```bash
# E2E only
bash scripts/e2e-verify.sh

# Pre-deploy only
bash scripts/pre-deploy-check.sh

# Release gate only
curl -X POST http://localhost:40000/api/wf/amingClaw/release-gate \
  -H "X-Gov-Token: $GOV_COORDINATOR_TOKEN"
```

## Uninstall

```bash
rm scripts/check-deploy-guard.sh
rm .claude/settings.json
# Remove deploy guard section from CLAUDE.md manually
```

## Observer SOP

When acting as observer, the following rules apply:

- **NEVER** run `docker compose up` directly
- **ALWAYS** use `bash scripts/deploy.sh`
- **NEVER** classify an E2E failure as "small problem"
- **ANY** E2E failure = STOP deployment
- If deploy.sh fails, fix the issue first, then retry deploy.sh

## FAQ

**Q: Can I use `--skip-e2e` or `SKIP_PRE_DEPLOY_CHECK`?**
A: No skip flags exist. All checks are mandatory.

**Q: What if E2E is broken but I need to deploy urgently?**
A: Fix the E2E test first. If the test itself is wrong, fix it. No exceptions.

**Q: What if gatekeeper blocks release?**
A: Check which check failed, fix the underlying issue, re-run deploy.sh.
