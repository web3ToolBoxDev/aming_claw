#!/bin/bash
# Aming Claw - Full Stack Startup
# Starts all services + registers domain pack + starts executor

set -e
cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
log() { echo -e "${GREEN}[startup]${NC} $1"; }
err() { echo -e "${RED}[startup]${NC} $1"; }

# 1. Start Docker services
log "Starting Docker services..."
docker compose -f docker-compose.governance.yml up -d

# 2. Wait for all services healthy
log "Waiting for services to be healthy..."
for i in $(seq 1 30); do
    GOV=$(curl -sf http://localhost:40000/api/health 2>/dev/null | python -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    DB=$(curl -sf http://localhost:40002/health 2>/dev/null | python -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    REDIS=$(docker exec aming_claw-redis-1 redis-cli ping 2>/dev/null)

    if [ "$GOV" = "ok" ] && [ "$REDIS" = "PONG" ]; then
        log "Core services healthy (governance=$GOV, redis=$REDIS, dbservice=$DB)"
        break
    fi

    if [ $i -eq 30 ]; then
        err "Services failed to start after 30 attempts"
        docker compose -f docker-compose.governance.yml ps
        exit 1
    fi
    sleep 2
done

# 3. Restart nginx (resolve upstream after governance starts)
log "Restarting nginx..."
docker compose -f docker-compose.governance.yml restart nginx 2>&1 | tail -1

# 4. Register dbservice domain pack
log "Registering dbservice domain pack..."
PACK_RESULT=$(curl -sf -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain":"development","types":{"architecture":{"durability":"permanent","conflictPolicy":"replace","description":"Architecture decisions"},"pitfall":{"durability":"permanent","conflictPolicy":"append","description":"Known pitfalls"},"pattern":{"durability":"permanent","conflictPolicy":"replace","description":"Code patterns"},"workaround":{"durability":"durable","conflictPolicy":"replace","description":"Workarounds"},"session_summary":{"durability":"durable","conflictPolicy":"replace","description":"Session summaries"},"verify_decision":{"durability":"permanent","conflictPolicy":"append","description":"Verify decisions"}}}' 2>/dev/null)
echo "  $PACK_RESULT"

# 5. Start Executor (background)
log "Starting Executor..."
cd agent
if netstat -ano 2>/dev/null | grep -q 39101; then
    log "Executor already running on port 39101"
else
    nohup python -m executor > ../shared-volume/codex-tasks/logs/executor.log 2>&1 &
    EXEC_PID=$!
    log "Executor started (PID=$EXEC_PID)"
fi
cd ..

# 5.5 Verify workspace registry (project_id routing)
log "Verifying workspace registry..."
sleep 2  # Wait for executor API to start
WS_COUNT=$(curl -sf http://localhost:40100/workspaces 2>/dev/null | python -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0")
if [ "$WS_COUNT" -gt "0" ]; then
    log "Workspace registry: $WS_COUNT workspace(s) registered"
    # Show workspace-to-project mappings
    curl -sf http://localhost:40100/workspaces 2>/dev/null | python -c "
import sys,json
data=json.load(sys.stdin)
for ws in data.get('workspaces',[]):
    pid=ws.get('project_id','(none)')
    label=ws.get('label','?')
    path=ws.get('path','?')
    print(f'  {label} → project_id={pid} → {path}')
" 2>/dev/null || true
else
    err "WARNING: No workspaces registered. Tasks may route to wrong workspace."
    err "Register workspaces manually or check executor startup logs."
fi

# 6. Final status
echo ""
log "=========================================="
log "  All services started"
log "=========================================="
echo ""
echo "  Nginx:      http://localhost:40000"
echo "  Governance:  http://localhost:40000/api/health"
echo "  Gateway:     Telegram polling active"
echo "  dbservice:   http://localhost:40002/health"
echo "  Redis:       localhost:40079"
echo "  Executor:    宿主机 (port 39101)"
echo ""
echo "  Telegram: /bind <coordinator_token>"
echo ""
