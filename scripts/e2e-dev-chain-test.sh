#!/bin/bash
# E2E Dev Chain Test — Full v6 dev task lifecycle
#
# Tests: Coordinator → dev_task → Dev AI (branch) → evidence → eval → reply
#
# Usage:
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/e2e-dev-chain-test.sh

set -e
cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

PASS=0
FAIL=0

pass() { echo -e "  ${GREEN}✅ $1${NC}"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌ $1${NC}"; FAIL=$((FAIL+1)); }

echo ""
echo "=========================================="
echo "  E2E Dev Chain Test (v6)"
echo "=========================================="

# 1. Executor API available
echo ""
echo "📡 Executor API:"
HEALTH=$(curl -sf http://localhost:40100/health 2>/dev/null)
ORCH=$(echo "$HEALTH" | python -c "import sys,json;print(json.load(sys.stdin).get('orchestrator',False))" 2>/dev/null)
if [ "$ORCH" = "True" ]; then
    pass "Executor API + Orchestrator ready"
else
    fail "Orchestrator not initialized"
    exit 1
fi

# 2. Send coordinator message that should create dev task
echo ""
echo "💬 Coordinator Chat (should create dev_task):"
RESULT=$(curl -sf -X POST http://localhost:40100/coordinator/chat \
    -H "Content-Type: application/json" \
    -d '{"message":"请创建一个dev task：在README.md末尾添加一行注释 # e2e-test-marker","project_id":"amingClaw"}' \
    --max-time 120 2>/dev/null)

STATUS=$(echo "$RESULT" | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
EXECUTED=$(echo "$RESULT" | python -c "import sys,json;print(json.load(sys.stdin).get('actions_executed',0))" 2>/dev/null)

if [ "$STATUS" = "success" ] && [ "$EXECUTED" -ge 1 ]; then
    pass "Coordinator created dev task (actions_executed=$EXECUTED)"
else
    fail "Coordinator did not create dev task (status=$STATUS, executed=$EXECUTED)"
fi

# 3. Check task file created
echo ""
echo "📁 Task File:"
sleep 2
PENDING=$(ls shared-volume/codex-tasks/pending/*.json 2>/dev/null | wc -l)
PROCESSING=$(ls shared-volume/codex-tasks/processing/*.json 2>/dev/null | wc -l)
TOTAL=$((PENDING + PROCESSING))

if [ "$TOTAL" -ge 1 ]; then
    pass "Task file exists (pending=$PENDING, processing=$PROCESSING)"
else
    fail "No task file found"
fi

# 4. Wait for executor to pick up (max 15s)
echo ""
echo "⚡ Executor Pickup:"
for i in $(seq 1 15); do
    PENDING=$(ls shared-volume/codex-tasks/pending/*.json 2>/dev/null | wc -l)
    if [ "$PENDING" -eq 0 ]; then
        pass "Executor picked up task ($i seconds)"
        break
    fi
    if [ $i -eq 15 ]; then
        fail "Executor did not pick up task within 15s"
    fi
    sleep 1
done

# 5. Check git branch (v6 should create dev/ branch)
echo ""
echo "🌿 Git Branch:"
DEV_BRANCHES=$(git branch --list 'dev/*' 2>/dev/null | wc -l)
if [ "$DEV_BRANCHES" -ge 1 ]; then
    BRANCH_NAME=$(git branch --list 'dev/*' 2>/dev/null | tail -1 | tr -d ' *')
    pass "Dev branch created: $BRANCH_NAME"
else
    fail "No dev/ branch found (v6 should auto-create)"
fi

# Summary
echo ""
echo "=========================================="
echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
    exit 1
fi
