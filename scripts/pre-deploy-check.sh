#!/bin/bash
# Pre-Deploy Check — 部署前自动检测
#
# 检测项:
#   1. verify_loop 全绿
#   2. coverage-check pass
#   3. 所有新节点 qa_pass
#   4. 文档已更新
#   5. 记忆已写入
#   6. staging 容器验证
#   7. dev/prod 配置一致性
#   8. Gateway 消息通道验证
#
# Usage:
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/pre-deploy-check.sh
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/pre-deploy-check.sh --skip-staging

set -e
cd "$(dirname "$0")/.."

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

pass() { echo -e "  ${GREEN}✅ $1${NC}"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌ $1${NC}"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; WARN=$((WARN+1)); }

COORD="${GOV_COORDINATOR_TOKEN:-}"
PROJECT="${GOV_PROJECT_ID:-amingClaw}"
NGINX_PORT=40000
STAGING_PORT=40007
CHAT_ID="${TELEGRAM_ADMIN_CHAT_ID:-7848961760}"
SKIP_STAGING="${1:-}"

if [ -z "$COORD" ]; then
    echo -e "${RED}Error: GOV_COORDINATOR_TOKEN not set${NC}"
    echo "Usage: GOV_COORDINATOR_TOKEN=gov-xxx $0"
    exit 1
fi

echo ""
echo "=========================================="
echo "  Pre-Deploy Check"
echo "  Project: ${PROJECT}"
echo "=========================================="
echo ""

# ── 1. Node Status Check ──
echo "📊 Node Status:"
SUMMARY=$(curl -sf "http://localhost:${NGINX_PORT}/api/wf/${PROJECT}/summary" \
    -H "X-Gov-Token: ${COORD}" 2>/dev/null)

if [ -z "$SUMMARY" ]; then
    fail "Cannot reach governance API"
else
    TOTAL=$(echo "$SUMMARY" | python -c "import sys,json;print(json.load(sys.stdin).get('total_nodes',0))" 2>/dev/null)
    QA_PASS=$(echo "$SUMMARY" | python -c "import sys,json;print(json.load(sys.stdin).get('by_status',{}).get('qa_pass',0))" 2>/dev/null)
    OTHER=$((TOTAL - QA_PASS))

    if [ "$OTHER" -eq 0 ]; then
        pass "All ${TOTAL} nodes qa_pass"
    else
        fail "${OTHER} nodes NOT qa_pass (${QA_PASS}/${TOTAL})"
    fi
fi

# ── 2. Coverage Check ──
echo ""
echo "📁 Coverage Check:"
CHANGED_FILES=$(git diff --name-only HEAD~1 2>/dev/null | python -c "
import sys, json
EXCLUDE = {'docs/', 'scripts/', '.env', '.gitignore', 'README.md', 'LICENSE', 'docker-compose', 'Dockerfile', 'nginx/', 'dbservice/', 'requirements.txt'}
files = []
for l in sys.stdin:
    f = l.strip()
    if f and not any(f.startswith(e) or f == e for e in EXCLUDE):
        files.append(f)
print(json.dumps(files))
" 2>/dev/null)
if [ -z "$CHANGED_FILES" ] || [ "$CHANGED_FILES" = "[]" ]; then
    CHANGED_FILES='[]'
fi

if [ "$CHANGED_FILES" = "[]" ]; then
    pass "No changed files"
else
    COVERAGE=$(curl -sf -X POST "http://localhost:${NGINX_PORT}/api/wf/${PROJECT}/coverage-check" \
        -H "Content-Type: application/json" -H "X-Gov-Token: ${COORD}" \
        -d "{\"files\":${CHANGED_FILES}}" 2>/dev/null)

    COV_PASS=$(echo "$COVERAGE" | python -c "import sys,json;print(json.load(sys.stdin).get('pass',False))" 2>/dev/null)
    if [ "$COV_PASS" = "True" ]; then
        pass "All changed files covered by nodes"
    else
        fail "Uncovered files detected"
        echo "$COVERAGE" | python -c "
import sys,json
d=json.load(sys.stdin)
for u in d.get('uncovered',[]):
    print(f'    {u[\"file\"]}')
" 2>/dev/null
    fi
fi

# ── 3. Docs Check ──
echo ""
echo "📝 Docs Check:"
DOCS=$(curl -sf "http://localhost:${NGINX_PORT}/api/docs" -H "X-Gov-Token: ${COORD}" 2>/dev/null)
DOC_COUNT=$(echo "$DOCS" | python -c "import sys,json;print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null)

if [ -n "$DOC_COUNT" ] && [ "$DOC_COUNT" -ge 10 ]; then
    pass "${DOC_COUNT} doc sections available"
else
    fail "Only ${DOC_COUNT} doc sections (expected >= 10)"
fi

# ── 4. Memory Check ──
echo ""
echo "🧠 Memory Check:"
MEM_COUNT=$(curl -sf "http://localhost:40002/knowledge/search" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"architecture\",\"scope\":\"${PROJECT}\",\"limit\":100}" 2>/dev/null \
    | python -c "import sys,json;print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null)

if [ -n "$MEM_COUNT" ] && [ "$MEM_COUNT" -ge 5 ]; then
    pass "${MEM_COUNT} memory entries for ${PROJECT}"
else
    warn "Only ${MEM_COUNT} memory entries (expected >= 5)"
fi

# ── 5. Gatekeeper / Release Gate ──
echo ""
echo "🔒 Gatekeeper:"
RELEASE=$(curl -sf -X POST "http://localhost:${NGINX_PORT}/api/wf/${PROJECT}/release-gate" \
    -H "Content-Type: application/json" -H "X-Gov-Token: ${COORD}" \
    -d '{}' 2>/dev/null)

RELEASE_OK=$(echo "$RELEASE" | python -c "import sys,json;print(json.load(sys.stdin).get('release',False))" 2>/dev/null)
GK_OK=$(echo "$RELEASE" | python -c "import sys,json;print(json.load(sys.stdin).get('gatekeeper_pass',True))" 2>/dev/null)

if [ "$RELEASE_OK" = "True" ]; then
    pass "Release gate: PASS"
else
    fail "Release gate: BLOCKED"
fi

if [ "$GK_OK" = "True" ]; then
    pass "Gatekeeper: PASS"
else
    fail "Gatekeeper: BLOCKED"
fi

# ── 6. Dev/Prod Config Consistency ──
echo ""
echo "⚙️  Config Consistency:"
# Check required env vars are set
REQUIRED_VARS="GOVERNANCE_PORT REDIS_URL SHARED_VOLUME_PATH"
CONFIG_OK=true
for var in $REQUIRED_VARS; do
    # Check if governance container has this var
    VAL=$(docker exec aming_claw-governance-1 printenv "$var" 2>/dev/null)
    if [ -z "$VAL" ]; then
        fail "Missing env var in governance: ${var}"
        CONFIG_OK=false
    fi
done

# Check port consistency
GOV_PORT=$(docker exec aming_claw-governance-1 printenv GOVERNANCE_PORT 2>/dev/null)
if [ "$GOV_PORT" = "40006" ]; then
    pass "Governance port consistent (40006)"
else
    fail "Governance port mismatch: expected 40006, got ${GOV_PORT}"
fi

# Check volume mounts
GOV_DATA=$(docker inspect aming_claw-governance-1 --format '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null)
if echo "$GOV_DATA" | grep -q "governance"; then
    pass "Governance data volume mounted"
else
    fail "Governance data volume NOT mounted"
fi

# Check Gateway task volume is bind mount (not Docker volume)
GW_MOUNT_TYPE=$(docker inspect aming_claw-telegram-gateway-1 --format '{{range .Mounts}}{{if eq .Destination "/app/shared-volume/codex-tasks"}}{{.Type}}{{end}}{{end}}' 2>/dev/null)
if [ "$GW_MOUNT_TYPE" = "bind" ]; then
    pass "Gateway task volume: bind mount (host-visible)"
else
    fail "Gateway task volume: ${GW_MOUNT_TYPE:-missing} (should be bind mount, not Docker volume)"
fi

# ── 7. Staging Container Verification ──
echo ""
echo "🔄 Staging Verification:"
if [ "$SKIP_STAGING" = "--skip-staging" ]; then
    warn "Staging check skipped (--skip-staging)"
else
    # Start staging container
    docker compose -f docker-compose.governance.yml --profile dev up -d governance-dev 2>/dev/null

    # Wait for healthy
    STAGING_OK=false
    for i in $(seq 1 15); do
        if curl -sf "http://localhost:${STAGING_PORT}/api/health" > /dev/null 2>&1; then
            STAGING_OK=true
            break
        fi
        sleep 2
    done

    if [ "$STAGING_OK" = "true" ]; then
        # Smoke test
        STAGE_PROJECTS=$(curl -sf "http://localhost:${STAGING_PORT}/api/project/list" 2>/dev/null)
        if echo "$STAGE_PROJECTS" | python -c "import sys,json;json.load(sys.stdin)" > /dev/null 2>&1; then
            pass "Staging container healthy + smoke test pass"
        else
            fail "Staging smoke test failed"
        fi
    else
        fail "Staging container failed to start"
    fi

    # Stop staging
    docker compose -f docker-compose.governance.yml --profile dev stop governance-dev 2>/dev/null
fi

# ── 8. Gateway Message Channel ──
echo ""
echo "📨 Gateway Channel:"
GW_RESULT=$(curl -sf -X POST "http://localhost:${NGINX_PORT}/gateway/reply" \
    -H "Content-Type: application/json" \
    -H "X-Gov-Token: ${COORD}" \
    -d "{\"chat_id\": ${CHAT_ID}, \"text\": \"[pre-deploy] 消息通道检测\"}" 2>/dev/null)

GW_OK=$(echo "$GW_RESULT" | python -c "import sys,json;print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
if [ "$GW_OK" = "True" ]; then
    pass "Gateway message channel OK"
else
    fail "Gateway message channel FAILED"
fi

# ── 9. E2E Task Execution Test ──
echo ""
echo "🔗 E2E Task Test:"
if [ -f "scripts/e2e-task-test.sh" ]; then
    if bash scripts/e2e-task-test.sh 2>/dev/null | grep -q "fail"; then
        fail "E2E task execution test failed"
    else
        pass "E2E task execution test passed"
    fi
else
    warn "E2E test script not found"
fi

# ── Summary ──
echo ""
echo "=========================================="
echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}, ${YELLOW}${WARN} warn${NC}"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
    echo ""
    echo -e "${RED}❌ PRE-DEPLOY CHECK FAILED — DO NOT DEPLOY${NC}"
    exit 1
else
    echo ""
    echo -e "${GREEN}✅ PRE-DEPLOY CHECK PASSED — SAFE TO DEPLOY${NC}"
    exit 0
fi
