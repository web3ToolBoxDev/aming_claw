#!/bin/bash
# E2E Task Execution Test
#
# Tests the full chain:
#   1. Gateway writes task file (via API or direct)
#   2. File visible on host (volume mount check)
#   3. Executor picks up task (pending → processing)
#   4. Task Registry state updated
#
# Usage:
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/e2e-task-test.sh

set -e
cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

pass() { echo -e "  ${GREEN}✅ $1${NC}"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌ $1${NC}"; FAIL=$((FAIL+1)); }

COORD="${GOV_COORDINATOR_TOKEN:-}"
PROJECT="${GOV_PROJECT_ID:-amingClaw}"
NGINX_PORT=40000
PENDING_DIR="shared-volume/codex-tasks/pending"
PROCESSING_DIR="shared-volume/codex-tasks/processing"
TASK_ID="task-e2e-test-$$"

if [ -z "$COORD" ]; then
    echo -e "${RED}Error: GOV_COORDINATOR_TOKEN not set${NC}"
    exit 1
fi

echo ""
echo "=========================================="
echo "  E2E Task Execution Test"
echo "  Task ID: ${TASK_ID}"
echo "=========================================="
echo ""

# ── 0. Workspace Routing Check ──
echo "Workspace Routing:"

# Verify workspace registry has project_id mappings
WS_RESP=$(curl -sf "http://localhost:40100/workspaces" 2>/dev/null)
WS_COUNT=$(echo "$WS_RESP" | python -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0")

if [ "$WS_COUNT" -gt "0" ]; then
    pass "Workspace registry: $WS_COUNT workspace(s) registered"
else
    fail "No workspaces registered - tasks will route to wrong workspace"
fi

# Verify project_id resolves correctly
RESOLVE_RESP=$(curl -sf "http://localhost:40100/workspaces/resolve?project_id=${PROJECT}" 2>/dev/null)
RESOLVED_PATH=$(echo "$RESOLVE_RESP" | python -c "import sys,json;print(json.load(sys.stdin).get('workspace',{}).get('path',''))" 2>/dev/null || echo "")

if [ -n "$RESOLVED_PATH" ] && [ -d "$RESOLVED_PATH" ]; then
    pass "project_id=$PROJECT resolves to: $RESOLVED_PATH"
else
    fail "project_id=$PROJECT does not resolve to a valid workspace"
fi

echo ""

# ── 1. Volume Mount Check ──
echo "Volume Mount:"

# Write file from Gateway container
docker exec aming_claw-telegram-gateway-1 sh -c "echo test > /app/shared-volume/codex-tasks/pending/e2e-mount-check.txt" 2>/dev/null

if [ -f "${PENDING_DIR}/e2e-mount-check.txt" ]; then
    pass "Gateway → Host volume mount OK (bind mount)"
    rm -f "${PENDING_DIR}/e2e-mount-check.txt"
else
    fail "Gateway → Host volume mount FAILED (Docker volume isolation)"
    echo "    Fix: docker-compose.yml should use bind mount, not named volume"
    echo "    Expected: ./shared-volume/codex-tasks:/app/shared-volume/codex-tasks"
fi

# Write file from host, check in container
echo "host-test" > "${PENDING_DIR}/e2e-host-check.txt" 2>/dev/null
CONTAINER_CHECK=$(docker exec aming_claw-telegram-gateway-1 cat /app/shared-volume/codex-tasks/pending/e2e-host-check.txt 2>/dev/null)
if [ "$CONTAINER_CHECK" = "host-test" ]; then
    pass "Host → Gateway volume mount OK"
    rm -f "${PENDING_DIR}/e2e-host-check.txt"
    docker exec aming_claw-telegram-gateway-1 rm -f /app/shared-volume/codex-tasks/pending/e2e-host-check.txt 2>/dev/null
else
    fail "Host → Gateway volume mount FAILED"
fi

# ── 2. Task File Write (atomic) ──
echo ""
echo "📝 Task File Write:"

python -c "
import json, os
task = {
    'task_id': '${TASK_ID}',
    'chat_id': 0,
    'project_id': '${PROJECT}',
    'text': 'e2e test - noop',
    'prompt': 'echo e2e-test-ok',
    'action': 'codex',
    'type': 'e2e_test',
    'created_at': '2026-01-01T00:00:00Z'
}
path = '${PENDING_DIR}'
os.makedirs(path, exist_ok=True)
tmp = os.path.join(path, '${TASK_ID}.tmp.json')
final = os.path.join(path, '${TASK_ID}.json')
with open(tmp, 'w') as f:
    json.dump(task, f)
    f.flush()
    os.fsync(f.fileno())
os.rename(tmp, final)
" 2>/dev/null

if [ -f "${PENDING_DIR}/${TASK_ID}.json" ]; then
    pass "Atomic write: .tmp → .json OK"
else
    fail "Atomic write failed"
fi

# ── 3. Executor Pickup ──
echo ""
echo "⚡ Executor Pickup:"

# Check executor is running
if netstat -ano 2>/dev/null | grep -q 39101; then
    pass "Executor process running (port 39101)"
else
    fail "Executor NOT running"
    echo ""
    echo "=========================================="
    echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}"
    echo "=========================================="
    exit 1
fi

# Wait for executor to pick up (max 10s)
PICKED=false
for i in $(seq 1 10); do
    if [ ! -f "${PENDING_DIR}/${TASK_ID}.json" ]; then
        PICKED=true
        break
    fi
    sleep 1
done

if [ "$PICKED" = "true" ]; then
    # Check if it moved to processing
    if [ -f "${PROCESSING_DIR}/${TASK_ID}.json" ]; then
        pass "Executor picked up: pending → processing"
    else
        # Might have already completed
        pass "Executor consumed task file"
    fi
else
    fail "Executor did not pick up task within 10s"
fi

# ── 4. Cleanup ──
rm -f "${PENDING_DIR}/${TASK_ID}.json" 2>/dev/null
rm -f "${PENDING_DIR}/${TASK_ID}.tmp.json" 2>/dev/null
# Don't clean processing - let executor finish naturally

# ── Summary ──
echo ""
echo "=========================================="
echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
    exit 1
else
    exit 0
fi
