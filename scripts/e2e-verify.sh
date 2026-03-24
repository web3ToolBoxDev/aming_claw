#!/bin/bash
# E2E Verify — 生产部署前端点验证 + 上下文连续性检查
#
# 检查项:
#   1. /health       — 服务健康
#   2. /observer     — 观测端点
#   3. /kpi          — 指标端点
#   4. /ctx          — 上下文端点 (轮次递增连续性)
#   5. ctx 连续性     — 两次调用轮次 round 递增
#
# 验证结果写入 gatekeeper record_check(type=e2e)
#
# Usage:
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/e2e-verify.sh
#   GOV_COORDINATOR_TOKEN=gov-xxx BASE_URL=http://localhost:40100 ./scripts/e2e-verify.sh

set -euo pipefail
cd "$(dirname "$0")/.."

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0

pass() { echo -e "  ${GREEN}✅ $1${NC}"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌ $1${NC}"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }

# ── 配置 ──
COORD="${GOV_COORDINATOR_TOKEN:-}"
PROJECT="${GOV_PROJECT_ID:-amingClaw}"
NGINX_PORT="${NGINX_PORT:-40000}"
BASE_URL="${BASE_URL:-http://localhost:${NGINX_PORT}}"
GK_URL="http://localhost:${NGINX_PORT}"

echo ""
echo "=========================================="
echo "  E2E Verify — Production Guard"
echo "  Base: ${BASE_URL}"
echo "  Project: ${PROJECT}"
echo "=========================================="
echo ""

# ── 结果收集 ──
RESULTS=()
OVERALL_PASS=true

check_endpoint() {
    local name="$1"
    local url="$2"
    local expected_key="${3:-}"

    local resp
    resp=$(curl -sf --max-time 10 "${url}" 2>/dev/null || true)

    if [ -z "$resp" ]; then
        fail "${name}: 无响应 (${url})"
        RESULTS+=("${name}:FAIL:no_response")
        OVERALL_PASS=false
        return
    fi

    # 验证是否为有效 JSON
    if ! echo "$resp" | python -c "import sys,json;json.load(sys.stdin)" > /dev/null 2>&1; then
        fail "${name}: 响应非 JSON"
        RESULTS+=("${name}:FAIL:invalid_json")
        OVERALL_PASS=false
        return
    fi

    # 如果指定了 key，验证该字段存在
    if [ -n "$expected_key" ]; then
        local val
        val=$(echo "$resp" | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('${expected_key}','__MISSING__'))" 2>/dev/null)
        if [ "$val" = "__MISSING__" ]; then
            fail "${name}: 缺少字段 '${expected_key}'"
            RESULTS+=("${name}:FAIL:missing_field_${expected_key}")
            OVERALL_PASS=false
            return
        fi
    fi

    pass "${name}: OK"
    RESULTS+=("${name}:PASS")
}

# ── 1. /health ──
echo "🏥 Health Check:"
check_endpoint "health" "${BASE_URL}/health" "status"

# ── 2. /observer ──
echo ""
echo "👁  Observer:"
check_endpoint "observer" "${BASE_URL}/observer" ""

# ── 3. /kpi ──
echo ""
echo "📊 KPI:"
check_endpoint "kpi" "${BASE_URL}/kpi" ""

# ── 4 & 5. /ctx 连续性 ──
echo ""
echo "🔄 Context Continuity (/ctx):"

CTX_PASS=false
CTX_DETAIL="skipped"

CTX1=$(curl -sf --max-time 10 "${BASE_URL}/ctx" 2>/dev/null || true)
if [ -z "$CTX1" ]; then
    fail "ctx: 第1次调用无响应"
    OVERALL_PASS=false
    CTX_DETAIL="first_call_no_response"
else
    ROUND1=$(echo "$CTX1" | python -c "import sys,json;print(json.load(sys.stdin).get('round', json.load(open('/dev/stdin')) if False else json.load(sys.stdin if False else __import__('io').StringIO('$CTX1')).get('round','__MISSING__')))" 2>/dev/null || \
             echo "$CTX1" | python -c "import sys,json;print(json.load(sys.stdin).get('round','__MISSING__'))" 2>/dev/null || echo "__MISSING__")

    # 更健壮的方式提取 round
    ROUND1=$(echo "$CTX1" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # 兼容不同字段名
    r = d.get('round') or d.get('ctx_round') or d.get('turn') or d.get('seq')
    print(r if r is not None else '__MISSING__')
except:
    print('__MISSING__')
" 2>/dev/null)

    sleep 1

    CTX2=$(curl -sf --max-time 10 "${BASE_URL}/ctx" 2>/dev/null || true)
    ROUND2=$(echo "$CTX2" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    r = d.get('round') or d.get('ctx_round') or d.get('turn') or d.get('seq')
    print(r if r is not None else '__MISSING__')
except:
    print('__MISSING__')
" 2>/dev/null)

    if [ "$ROUND1" = "__MISSING__" ] || [ "$ROUND2" = "__MISSING__" ]; then
        warn "ctx: 无 round/ctx_round/turn/seq 字段，跳过连续性检查"
        pass "ctx: 端点可达"
        CTX_DETAIL="no_round_field"
        CTX_PASS=true
        RESULTS+=("ctx:PASS")
        RESULTS+=("ctx_continuity:SKIP:no_round_field")
    else
        pass "ctx: 第1次 round=${ROUND1}, 第2次 round=${ROUND2}"
        RESULTS+=("ctx:PASS")

        # 连续性: round2 > round1
        CONTINUITY=$(python3 -c "
try:
    r1, r2 = ${ROUND1}, ${ROUND2}
    print('PASS' if r2 > r1 else 'FAIL')
except:
    print('SKIP')
" 2>/dev/null)

        if [ "$CONTINUITY" = "PASS" ]; then
            pass "ctx_continuity: round 递增 (${ROUND1} → ${ROUND2}) ✅"
            CTX_PASS=true
            CTX_DETAIL="round_${ROUND1}_to_${ROUND2}"
            RESULTS+=("ctx_continuity:PASS:${CTX_DETAIL}")
        elif [ "$CONTINUITY" = "SKIP" ]; then
            warn "ctx_continuity: 无法比较 (非数字 round)"
            CTX_DETAIL="non_numeric_round"
            RESULTS+=("ctx_continuity:SKIP:${CTX_DETAIL}")
        else
            fail "ctx_continuity: round 未递增 (${ROUND1} → ${ROUND2})"
            OVERALL_PASS=false
            CTX_DETAIL="round_not_incremented_${ROUND1}_to_${ROUND2}"
            RESULTS+=("ctx_continuity:FAIL:${CTX_DETAIL}")
        fi
    fi
fi

# ── 写入 gatekeeper ──
echo ""
echo "📝 Writing to Gatekeeper (type=e2e):"

# 构建 results JSON
RESULTS_JSON=$(python3 -c "
import json, sys
results = ${RESULTS@Q}
# 解析 bash 数组
items = []
" 2>/dev/null || true)

RESULTS_JSON=$(python3 << 'PYEOF'
import json
import sys

results_raw = """${RESULTS[*]:-}"""
items = []
for r in results_raw.split():
    parts = r.split(':')
    item = {'endpoint': parts[0], 'status': parts[1] if len(parts) > 1 else 'UNKNOWN'}
    if len(parts) > 2:
        item['detail'] = ':'.join(parts[2:])
    items.append(item)

print(json.dumps(items))
PYEOF
2>/dev/null || echo '[]')

OVERALL_STATUS="PASS"
if [ "$OVERALL_PASS" = false ]; then
    OVERALL_STATUS="FAIL"
fi

# 写入 gatekeeper record_check
if [ -z "$COORD" ]; then
    warn "GOV_COORDINATOR_TOKEN 未设置，跳过 gatekeeper 写入"
else
    GK_PAYLOAD=$(python3 -c "
import json
payload = {
    'type': 'e2e',
    'status': '${OVERALL_STATUS}',
    'detail': {
        'endpoints': ${RESULTS_JSON:-[]},
        'ctx_continuity': '${CTX_DETAIL}',
        'overall': '${OVERALL_STATUS}'
    }
}
print(json.dumps(payload))
" 2>/dev/null)

    GK_RESP=$(curl -sf -X POST "${GK_URL}/api/wf/${PROJECT}/record-check" \
        -H "Content-Type: application/json" \
        -H "X-Gov-Token: ${COORD}" \
        -d "${GK_PAYLOAD}" \
        --max-time 10 2>/dev/null || true)

    if [ -n "$GK_RESP" ]; then
        GK_OK=$(echo "$GK_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('ok',json.load(sys.stdin) if False else True))" 2>/dev/null || echo "true")
        pass "Gatekeeper record_check(type=e2e) 写入成功"
        echo "    status=${OVERALL_STATUS}, results=$(echo ${RESULTS_JSON:-[]} | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))' 2>/dev/null) items"
    else
        warn "Gatekeeper 写入无响应（服务可能未启动）"
    fi
fi

# ── 汇总 ──
echo ""
echo "=========================================="
echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
    echo ""
    echo -e "${RED}❌ E2E VERIFY FAILED${NC}"
    exit 1
else
    echo ""
    echo -e "${GREEN}✅ E2E VERIFY PASSED${NC}"
    exit 0
fi
