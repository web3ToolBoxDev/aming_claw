#!/bin/bash
# Merge dev branch → main → pre-deploy-check → deploy
#
# Usage:
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/merge-and-deploy.sh dev/task-xxx
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/merge-and-deploy.sh dev/task-xxx --dry-run

set -e
cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[merge]${NC} $1"; }
err() { echo -e "${RED}[merge]${NC} $1"; }
warn() { echo -e "${YELLOW}[merge]${NC} $1"; }

BRANCH="${1:-}"
FLAG2="${2:-}"
COORD="${GOV_COORDINATOR_TOKEN:-}"
DRY_RUN=""
SKIP_DEPLOY=""
for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN="true" ;;
        --skip-deploy) SKIP_DEPLOY="true" ;;
    esac
done

if [ -z "$BRANCH" ]; then
    echo "Usage: $0 <branch-name> [--dry-run]"
    echo ""
    echo "Available dev branches:"
    git branch --list 'dev/*' 2>/dev/null || echo "  (none)"
    exit 1
fi

# 1. Check branch exists
log "Checking branch: ${BRANCH}"
if ! git rev-parse --verify "$BRANCH" > /dev/null 2>&1; then
    err "Branch '${BRANCH}' does not exist"
    exit 1
fi

# 2. Show diff
log "Changes in ${BRANCH} vs main:"
DIFF_STAT=$(git diff main..."${BRANCH}" --stat 2>/dev/null)
echo "$DIFF_STAT"

CHANGED_FILES=$(git diff main..."${BRANCH}" --name-only 2>/dev/null)
echo ""
log "Changed files:"
echo "$CHANGED_FILES"

if [ "$DRY_RUN" = "true" ]; then
    log "Dry run complete. No changes made."
    exit 0
fi

# 3. Rebase dev branch onto latest main (avoid merge conflicts from checkpoint drift)
log "Rebasing ${BRANCH} onto main..."
git checkout "${BRANCH}" 2>/dev/null
if ! git rebase main 2>&1; then
    err "Rebase failed — conflicts detected"
    git rebase --abort 2>/dev/null
    # Fallback: try merge directly (may produce merge commit)
    warn "Falling back to direct merge..."
    git checkout main 2>/dev/null
    if ! git merge "${BRANCH}" --no-ff -m "Merge ${BRANCH}: auto-chain approved" 2>&1; then
        err "Merge also failed — manual resolution needed"
        git merge --abort 2>/dev/null
        exit 1
    fi
else
    # Rebase succeeded, now fast-forward merge
    git checkout main 2>/dev/null
    git merge "${BRANCH}" --no-ff -m "Merge ${BRANCH}: auto-chain approved" 2>&1
fi

# 4. Skip deploy if requested (auto-chain tasks without governance nodes)
if [ "$SKIP_DEPLOY" = "true" ]; then
    log "Merge complete (deploy skipped per --skip-deploy)"
    log "Branch: ${BRANCH} → main"
    # Cleanup branch
    git branch -d "${BRANCH}" 2>/dev/null || true
    exit 0
fi

# 4b. Pre-deploy check
if [ -n "$COORD" ]; then
    log "Running pre-deploy check..."
    if bash scripts/pre-deploy-check.sh --skip-staging; then
        log "Pre-deploy check PASSED ✅"
    else
        err "Pre-deploy check FAILED ❌"
        warn "Branch merged but deploy blocked. Fix issues before deploying."
        exit 1
    fi
else
    warn "GOV_COORDINATOR_TOKEN not set, skipping pre-deploy check"
fi

# 5. Deploy
log "Deploying to production..."
docker compose -f docker-compose.governance.yml up -d --build governance telegram-gateway
sleep 8
docker compose -f docker-compose.governance.yml restart nginx

# 6. Verify
sleep 3
HEALTH=$(curl -sf http://localhost:40000/api/health 2>/dev/null | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
if [ "$HEALTH" = "ok" ]; then
    log "Production healthy ✅"
else
    err "Production health check failed!"
    exit 1
fi

# 7. Cleanup branch
log "Cleaning up branch: ${BRANCH}"
git branch -d "${BRANCH}" 2>/dev/null || true

# 8. Sync to dev environment
log "Syncing to dev environment..."
docker exec aming_claw-governance-1 sh -c "tar czf /tmp/gov-data.tar.gz -C /app/shared-volume/codex-tasks/state/governance ." 2>/dev/null
docker cp aming_claw-governance-1:/tmp/gov-data.tar.gz /tmp/gov-data.tar.gz 2>/dev/null
docker cp /tmp/gov-data.tar.gz aming_claw-governance-dev-1:/tmp/gov-data.tar.gz 2>/dev/null
docker exec aming_claw-governance-dev-1 sh -c "cd /app/shared-volume/codex-tasks/state/governance && tar xzf /tmp/gov-data.tar.gz" 2>/dev/null
docker compose -f docker-compose.governance.yml --profile dev restart governance-dev 2>/dev/null

log "=========================================="
log "  Merge + Deploy complete"
log "  Branch: ${BRANCH} → main"
log "  Production: healthy"
log "=========================================="
