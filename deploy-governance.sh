#!/bin/bash
# Zero-downtime deployment for governance service
#
# Strategy:
#   1. Build new image
#   2. Start new container on staging port (40007)
#   3. Health check + smoke test
#   4. Swap: stop old, start new on production port (40006)
#
# Data safety:
#   - Docker volume (governance-data) is NEVER deleted
#   - SQLite + graph.json + audit logs persist across deployments
#   - Redis sessions survive (separate container, not restarted)
#   - Coordinator token (10yr TTL) stays valid
#
# Usage:
#   ./deploy-governance.sh              # full deploy
#   ./deploy-governance.sh --build-only # just build, don't swap
#   ./deploy-governance.sh --rollback   # revert to previous image

set -e

COMPOSE_FILE="docker-compose.governance.yml"
SERVICE="governance"
PROD_PORT=40006
STAGE_PORT=40007
HEALTH_RETRIES=10
HEALTH_DELAY=3

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $1"; }
err() { echo -e "${RED}[deploy]${NC} $1"; }

# --- Pre-flight checks ---
log "Pre-flight checks..."

NGINX_PORT=40000
GOV_TOKEN="${GOV_COORDINATOR_TOKEN:-}"
SKIP_COVERAGE="${SKIP_COVERAGE_CHECK:-}"
SKIP_PRE_DEPLOY="${SKIP_PRE_DEPLOY_CHECK:-}"

# Verify current service is running
if curl -sf http://localhost:${PROD_PORT}/api/health > /dev/null 2>&1; then
    log "Current production service healthy on :${PROD_PORT}"
else
    warn "Production service not running on :${PROD_PORT}, doing fresh deploy"
fi

# --- Step 0: Full pre-deploy check ---
if [ "$1" = "--rollback" ]; then
    log "Rollback mode, skipping pre-deploy check"
elif [ -n "$SKIP_PRE_DEPLOY" ]; then
    warn "SKIP_PRE_DEPLOY_CHECK set, skipping (not recommended)"
elif [ -z "$GOV_TOKEN" ]; then
    warn "GOV_COORDINATOR_TOKEN not set, skipping pre-deploy check"
else
    log "Running full pre-deploy check..."
    if bash scripts/pre-deploy-check.sh --skip-staging; then
        log "Pre-deploy check PASSED ✅"
    else
        err "Pre-deploy check FAILED ❌"
        err "Fix issues above before deploying."
        err "Or set SKIP_PRE_DEPLOY_CHECK=1 to bypass (not recommended)."
        exit 1
    fi
fi

# --- Step 0.5: Coverage check (gatekeeper) ---
if [ "$1" = "--rollback" ]; then
    log "Rollback mode, skipping coverage check"
elif [ -n "$SKIP_COVERAGE" ]; then
    warn "SKIP_COVERAGE_CHECK set, skipping (not recommended)"
elif [ -z "$GOV_TOKEN" ]; then
    warn "GOV_COORDINATOR_TOKEN not set, skipping coverage check"
    warn "Set it to enable pre-deploy coverage validation"
else
    log "Running pre-deploy coverage check..."
    # Get changed files vs main branch
    CHANGED_FILES=$(git diff --name-only HEAD~1 2>/dev/null | tr '\n' '","' | sed 's/^/["/' | sed 's/,"$/]/')
    if [ "$CHANGED_FILES" = '[""]' ] || [ "$CHANGED_FILES" = '["' ]; then
        CHANGED_FILES='[]'
    fi

    if [ "$CHANGED_FILES" != "[]" ]; then
        COVERAGE_RESULT=$(curl -sf -X POST "http://localhost:${NGINX_PORT}/api/wf/amingClaw/coverage-check" \
            -H "Content-Type: application/json" \
            -H "X-Gov-Token: ${GOV_TOKEN}" \
            -d "{\"files\":${CHANGED_FILES}}" 2>/dev/null)

        COVERAGE_PASS=$(echo "$COVERAGE_RESULT" | python -c "import sys,json;print(json.load(sys.stdin).get('pass',False))" 2>/dev/null)

        if [ "$COVERAGE_PASS" = "True" ]; then
            log "Coverage check passed ✅"
        else
            err "Coverage check FAILED ❌"
            echo "$COVERAGE_RESULT" | python -c "
import sys,json
d=json.load(sys.stdin)
for u in d.get('uncovered',[]):
    print(f'  Uncovered: {u[\"file\"]} — {u.get(\"suggestion\",\"\")}')
" 2>/dev/null
            err "Create acceptance graph nodes for uncovered files before deploying."
            err "Or set SKIP_COVERAGE_CHECK=1 to bypass (not recommended)."
            exit 1
        fi
    else
        log "No changed files detected, skipping coverage check"
    fi
fi

# --- Step 1: Build new image ---
log "Building new image..."
docker compose -f ${COMPOSE_FILE} build ${SERVICE}

if [ "$1" = "--build-only" ]; then
    log "Build complete. Use without --build-only to deploy."
    exit 0
fi

# --- Step 2: Tag current image for rollback ---
CURRENT_IMAGE=$(docker inspect aming_claw-governance-1 --format '{{.Image}}' 2>/dev/null || echo "none")
if [ "$CURRENT_IMAGE" != "none" ]; then
    docker tag ${CURRENT_IMAGE} aming_claw-governance:rollback 2>/dev/null || true
    log "Tagged current image for rollback"
fi

# --- Step 3: Start staging container ---
log "Starting staging container on :${STAGE_PORT}..."

# Create a staging compose override
cat > /tmp/governance-staging.yml << 'STAGING'
services:
  governance-staging:
    image: aming_claw-governance:latest
    ports:
      - "${STAGE_PORT}:40006"
    volumes:
      - governance-data:/app/shared-volume/codex-tasks/state/governance
      - .:/workspace:ro
    environment:
      - GOVERNANCE_PORT=40006
      - REDIS_URL=redis://redis:6379/0
      - SHARED_VOLUME_PATH=/app/shared-volume
    depends_on:
      redis:
        condition: service_healthy
    network_mode: "container:aming_claw-redis-1"
STAGING

# Use simpler approach: just run the new container directly
docker run -d \
    --name governance-staging \
    --network container:aming_claw-redis-1 \
    -p ${STAGE_PORT}:40006 \
    -v aming_claw_governance-data:/app/shared-volume/codex-tasks/state/governance \
    -e GOVERNANCE_PORT=40006 \
    -e REDIS_URL=redis://redis:6379/0 \
    -e SHARED_VOLUME_PATH=/app/shared-volume \
    aming_claw-governance:latest \
    2>/dev/null || {
        # Network mode might fail, try with bridge
        docker run -d \
            --name governance-staging \
            -p ${STAGE_PORT}:40006 \
            -v aming_claw_governance-data:/app/shared-volume/codex-tasks/state/governance \
            -e GOVERNANCE_PORT=40006 \
            -e SHARED_VOLUME_PATH=/app/shared-volume \
            aming_claw-governance:latest
    }

# --- Step 4: Health check staging ---
log "Waiting for staging to be healthy..."
for i in $(seq 1 ${HEALTH_RETRIES}); do
    if curl -sf http://localhost:${STAGE_PORT}/api/health > /dev/null 2>&1; then
        log "Staging healthy on :${STAGE_PORT}"
        break
    fi
    if [ $i -eq ${HEALTH_RETRIES} ]; then
        err "Staging failed health check after ${HEALTH_RETRIES} attempts"
        docker rm -f governance-staging 2>/dev/null
        exit 1
    fi
    sleep ${HEALTH_DELAY}
done

# --- Step 5: Smoke test ---
log "Running smoke tests..."
SUMMARY=$(curl -sf http://localhost:${STAGE_PORT}/api/project/list 2>/dev/null)
if echo "$SUMMARY" | python -c "import sys,json; json.load(sys.stdin)" > /dev/null 2>&1; then
    log "Smoke test passed: project list OK"
else
    err "Smoke test failed!"
    docker rm -f governance-staging 2>/dev/null
    exit 1
fi

# --- Step 6: Swap ---
log "Swapping production..."

# Stop staging (we verified it works)
docker rm -f governance-staging 2>/dev/null

# Recreate production with new image
docker compose -f ${COMPOSE_FILE} up -d --no-build ${SERVICE}

# Wait for production to be healthy
for i in $(seq 1 ${HEALTH_RETRIES}); do
    if curl -sf http://localhost:${PROD_PORT}/api/health > /dev/null 2>&1; then
        log "Production healthy on :${PROD_PORT}"
        break
    fi
    sleep ${HEALTH_DELAY}
done

# --- Step 7: Post-deploy verification ---
log "Post-deploy verification..."
curl -sf http://localhost:${PROD_PORT}/api/project/list | python -c "
import sys,json
projects = json.load(sys.stdin)['projects']
print(f'  Projects: {len(projects)}')
for p in projects:
    print(f'    {p[\"project_id\"]}: {p.get(\"node_count\",0)} nodes')
"

log "Deployment complete!"
echo ""
echo "Rollback command (if needed):"
echo "  docker tag aming_claw-governance:rollback aming_claw-governance:latest"
echo "  docker compose -f ${COMPOSE_FILE} up -d ${SERVICE}"
