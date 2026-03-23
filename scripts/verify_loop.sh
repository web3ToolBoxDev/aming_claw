#!/bin/bash
# Post-verification self-check loop
# Runs after each verify-update or qa_pass to catch process violations
#
# Checks:
#   1. Docs: every node with @route in primary has api_docs section
#   2. Coverage: all changed files have graph nodes
#   3. Nodes: no code changes without corresponding nodes
#   4. Gatekeeper: release-gate passes with gatekeeper
#
# Usage: ./scripts/verify_loop.sh [token] [project_id]

set -e

TOKEN="${1:-$GOV_COORDINATOR_TOKEN}"
PROJECT="${2:-amingClaw}"
BASE="http://localhost:40000"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check_pass() { echo -e "  ${GREEN}✅ $1${NC}"; PASS=$((PASS+1)); }
check_fail() { echo -e "  ${RED}❌ $1${NC}"; FAIL=$((FAIL+1)); }
check_warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; WARN=$((WARN+1)); }

echo "=========================================="
echo "  Post-Verification Self-Check"
echo "  Project: $PROJECT"
echo "=========================================="
echo ""

# --- 1. Summary ---
echo "📊 Node Status:"
SUMMARY=$(curl -sf "$BASE/api/wf/$PROJECT/summary" -H "X-Gov-Token: $TOKEN" 2>/dev/null)
if [ -z "$SUMMARY" ]; then
    check_fail "Cannot reach governance API"
    exit 1
fi

TOTAL=$(echo "$SUMMARY" | python -c "import sys,json;print(json.load(sys.stdin)['total_nodes'])")
QA_PASS=$(echo "$SUMMARY" | python -c "import sys,json;print(json.load(sys.stdin).get('by_status',{}).get('qa_pass',0))")
PENDING=$(echo "$SUMMARY" | python -c "import sys,json;d=json.load(sys.stdin).get('by_status',{});print(sum(v for k,v in d.items() if k!='qa_pass'))")

echo "  Total: $TOTAL, qa_pass: $QA_PASS, other: $PENDING"
if [ "$PENDING" -gt 0 ]; then
    check_warn "$PENDING nodes not yet qa_pass"
else
    check_pass "All $TOTAL nodes qa_pass"
fi
echo ""

# --- 2. Coverage Check ---
echo "📁 Coverage Check:"
CHANGED=$(git diff --name-only HEAD~3 2>/dev/null | grep -v "^docs/" | grep -v "^scripts/" | grep -v "README" | grep -v "\.md$" | grep -v "\.env" | grep -v "\.gitignore" | head -50)
if [ -z "$CHANGED" ]; then
    check_pass "No code changes to check"
else
    FILES_JSON=$(echo "$CHANGED" | python -c "import sys,json;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
    COVERAGE=$(curl -sf -X POST "$BASE/api/wf/$PROJECT/coverage-check" \
        -H "Content-Type: application/json" \
        -H "X-Gov-Token: $TOKEN" \
        -d "{\"files\":$FILES_JSON}" 2>/dev/null)

    COV_PASS=$(echo "$COVERAGE" | python -c "import sys,json;print(json.load(sys.stdin).get('pass',False))" 2>/dev/null)
    COV_PCT=$(echo "$COVERAGE" | python -c "import sys,json;print(json.load(sys.stdin).get('coverage_pct',0))" 2>/dev/null)
    UNCOVERED=$(echo "$COVERAGE" | python -c "import sys,json;[print(f'    {u[\"file\"]}') for u in json.load(sys.stdin).get('uncovered',[])]" 2>/dev/null)

    if [ "$COV_PASS" = "True" ]; then
        check_pass "Coverage: ${COV_PCT}% (all files tracked)"
    else
        check_fail "Coverage: ${COV_PCT}% — uncovered files:"
        echo "$UNCOVERED"
    fi
fi
echo ""

# --- 3. Docs Check ---
echo "📝 Docs Check:"
DOCS_INDEX=$(curl -sf "$BASE/api/docs" 2>/dev/null)
DOC_COUNT=$(echo "$DOCS_INDEX" | python -c "import sys,json;print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null)
check_pass "$DOC_COUNT doc sections available"

# Check artifacts for all pending + recently passed nodes
ARTIFACTS_RESULT=$(curl -sf -X POST "$BASE/api/wf/$PROJECT/artifacts-check" \
    -H "Content-Type: application/json" \
    -H "X-Gov-Token: $TOKEN" \
    -d '{"nodes":["L9.3","L9.4","L9.5","L9.6","L9.7"]}' 2>/dev/null)

ART_PASS=$(echo "$ARTIFACTS_RESULT" | python -c "import sys,json;print(json.load(sys.stdin).get('pass',False))" 2>/dev/null)
if [ "$ART_PASS" = "True" ]; then
    check_pass "Artifacts check passed for L9 nodes"
else
    check_fail "Artifacts check failed:"
    echo "$ARTIFACTS_RESULT" | python -c "
import sys,json
d=json.load(sys.stdin)
for nid, r in d.get('nodes',{}).items():
    for m in r.get('missing',[]):
        print(f'    {nid}: {m[\"type\"]} — {m[\"reason\"][:60]}')
" 2>/dev/null
fi
echo ""

# --- 4. Memory Write Check ---
echo "🧠 Memory Check:"
# Count recent memories (last 1 hour)
RECENT_MEMORIES=$(curl -sf -X POST "http://localhost:40002/knowledge/find" \
    -H "Content-Type: application/json" \
    -d "{\"scope\":\"$PROJECT\"}" 2>/dev/null)

TOTAL_MEMORIES=$(echo "$RECENT_MEMORIES" | python -c "import sys,json;print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null)

# Count code changes in recent commits
CODE_CHANGES=$(git diff --name-only HEAD~3 2>/dev/null | grep -v "^docs/" | grep -v "^scripts/" | grep -v "README" | grep -v "\.md$" | grep -v "\.env" | wc -l)

if [ "$CODE_CHANGES" -gt 5 ] && [ "$TOTAL_MEMORIES" -lt 5 ]; then
    check_fail "Memory gap: $CODE_CHANGES code files changed but only $TOTAL_MEMORIES memories in dbservice. Write decisions/pitfalls/architecture!"
elif [ "$CODE_CHANGES" -gt 10 ] && [ "$TOTAL_MEMORIES" -lt 10 ]; then
    check_warn "Low memory coverage: $CODE_CHANGES files changed, $TOTAL_MEMORIES memories. Consider documenting key decisions."
else
    check_pass "Memory coverage: $TOTAL_MEMORIES entries for $CODE_CHANGES changed files"
fi

# Check if any new nodes were added but no corresponding memories
PENDING_NODES=$(echo "$SUMMARY" | python -c "import sys,json;d=json.load(sys.stdin).get('by_status',{});print(sum(v for k,v in d.items() if k!='qa_pass'))" 2>/dev/null)
if [ "$PENDING_NODES" -gt 0 ]; then
    check_warn "$PENDING_NODES nodes still pending — verify and write memories for completed work"
fi
echo ""

# --- 4.5 Docs Update Check ---
echo "📖 Docs Update Check:"
# Get list of nodes that have @route in primary files (need api_docs)
NODES_NEEDING_DOCS=$(curl -sf -X POST "$BASE/api/wf/$PROJECT/artifacts-check" \
    -H "Content-Type: application/json" \
    -H "X-Gov-Token: $TOKEN" \
    -d '{"nodes":["L5.3","L5.4","L7.1","L7.4","L8.4","L8.5","L9.1","L9.3","L9.5"]}' 2>/dev/null)

DOCS_PASS=$(echo "$NODES_NEEDING_DOCS" | python -c "import sys,json;print(json.load(sys.stdin).get('pass',False))" 2>/dev/null)
if [ "$DOCS_PASS" = "True" ]; then
    check_pass "All API nodes have documentation"
else
    check_fail "Missing API documentation:"
    echo "$NODES_NEEDING_DOCS" | python -c "
import sys,json
d=json.load(sys.stdin)
for nid, r in d.get('nodes',{}).items():
    for m in r.get('missing',[]):
        inf = ' (auto-inferred)' if m.get('inferred') else ''
        print(f'    {nid}: {m[\"type\"]}{inf} — {m[\"reason\"][:60]}')
" 2>/dev/null
fi
echo ""

# --- 5. Gatekeeper ---
echo "🔒 Gatekeeper:"
RELEASE=$(curl -sf -X POST "$BASE/api/wf/$PROJECT/release-gate" \
    -H "Content-Type: application/json" \
    -H "X-Gov-Token: $TOKEN" \
    -d '{"profile":"full"}' 2>/dev/null)

REL_PASS=$(echo "$RELEASE" | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('release',False))" 2>/dev/null)
GK_PASS=$(echo "$RELEASE" | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('gatekeeper',{}).get('pass','n/a'))" 2>/dev/null)

if [ "$REL_PASS" = "True" ]; then
    check_pass "Release gate: PASS (gatekeeper: $GK_PASS)"
else
    # Parse blockers
    BLOCKERS=$(echo "$RELEASE" | python -c "
import sys,json
d=json.load(sys.stdin)
blockers=d.get('details',{}).get('blockers',[])
gk=[b for b in blockers if b.get('node_id')=='_gatekeeper']
nodes=[b for b in blockers if b.get('node_id')!='_gatekeeper']
if nodes:
    print(f'  Nodes: {len(nodes)} blocking')
    for b in nodes[:5]:
        print(f'    {b[\"node_id\"]}: {b[\"status\"]} (need {b.get(\"required\",\"?\")})')
if gk:
    print(f'  Gatekeeper: {gk[0].get(\"details\",[])}')
" 2>/dev/null)
    check_fail "Release gate: BLOCKED"
    echo "$BLOCKERS"
fi
echo ""

# --- Summary ---
echo "=========================================="
echo -e "  Results: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}, ${YELLOW}${WARN} warn${NC}"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo -e "${RED}ACTION REQUIRED: Fix failures before proceeding${NC}"
    exit 1
fi
