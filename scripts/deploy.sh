#!/bin/bash
# deploy.sh — amingClaw 生产部署入口
#
# 执行权限: chmod +x scripts/deploy.sh
#
# 部署步骤:
#   1. git pull          — 拉取最新代码
#   2. pip install       — 安装/更新依赖
#   3. migration         — 执行数据库迁移（如有）
#   4. restart service   — 重启服务 (systemd / supervisor / 直接进程)
#   5. e2e-verify        — 验证部署成功
#
# 质量门禁（设置 GOV_COORDINATOR_TOKEN 时启用）:
#   - pre-deploy-check   — 节点覆盖 / 文档 / 配置一致性
#   - release-gate       — gatekeeper 最终放行
#
# Usage:
#   ./scripts/deploy.sh
#   SERVICE_MANAGER=systemd SERVICE_NAME=amingclaw ./scripts/deploy.sh
#   GOV_COORDINATOR_TOKEN=gov-xxx ./scripts/deploy.sh
#   ./scripts/deploy.sh --skip-e2e      # 紧急绕过（需记录原因）
#   ./scripts/deploy.sh --rollback      # 回滚到上一版本

set -euo pipefail
cd "$(dirname "$0")/.."

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
err()  { echo -e "${RED}[deploy]${NC} $*" >&2; }
step() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# ── 错误处理 ──
die() {
    err "❌ 部署失败: $*"
    err ""
    err "   请检查日志后重新部署。"
    exit 1
}

trap 'die "第 $LINENO 行发生意外错误 (exit $?)"' ERR

# ── 配置（支持环境变量覆盖，CI/CD 无交互）──
COORD="${GOV_COORDINATOR_TOKEN:-}"
PROJECT="${GOV_PROJECT_ID:-amingClaw}"
NGINX_PORT="${NGINX_PORT:-40000}"
MODE="${1:-}"
SKIP_E2E="${SKIP_E2E:-}"

# 服务管理器: systemd | supervisor | process | none
SERVICE_MANAGER="${SERVICE_MANAGER:-auto}"
SERVICE_NAME="${SERVICE_NAME:-amingclaw}"
PROCESS_PID_FILE="${PROCESS_PID_FILE:-}"
PYTHON="${PYTHON:-python3}"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   amingClaw Deployment Script            ║"
echo "║   $(date '+%Y-%m-%d %H:%M:%S')                    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 回滚模式 ──
if [ "$MODE" = "--rollback" ]; then
    warn "ROLLBACK MODE: 回滚到上一版本..."
    if command -v git &>/dev/null; then
        PREV_COMMIT=$(git rev-parse HEAD~1 2>/dev/null || die "无法获取上一个 commit")
        warn "回滚至: $PREV_COMMIT"
        git reset --hard "$PREV_COMMIT" || die "git reset --hard 失败"
        log "✅ 代码已回滚"
    else
        die "git 不可用，无法执行回滚"
    fi
    # 回滚后重启服务
    MODE="--restart-only"
fi

# ══════════════════════════════════════════════
# 质量门禁（仅在设置 GOV_COORDINATOR_TOKEN 时）
# ══════════════════════════════════════════════
if [ -n "$COORD" ] && [ "$MODE" != "--restart-only" ]; then
    step "质量门禁: Pre-Deploy Check"

    if bash scripts/pre-deploy-check.sh --skip-staging 2>/dev/null; then
        log "✅ Pre-Deploy Check: PASSED"
    else
        die "Pre-Deploy Check 未通过。请确保所有节点状态为 qa_pass 后重试。"
    fi

    step "质量门禁: Release Gate"
    RELEASE=$(curl -sf -X POST "http://localhost:${NGINX_PORT}/api/wf/${PROJECT}/release-gate" \
        -H "Content-Type: application/json" \
        -H "X-Gov-Token: ${COORD}" \
        -d '{}' \
        --max-time 15 2>/dev/null || true)

    if [ -z "$RELEASE" ]; then
        warn "⚠️  Release Gate 无响应（governance 服务未运行），跳过门禁"
    else
        RELEASE_OK=$(echo "$RELEASE" | python3 -c \
            "import sys,json;print(json.load(sys.stdin).get('release',False))" 2>/dev/null || echo "False")
        if [ "$RELEASE_OK" = "True" ]; then
            log "✅ Release Gate: PASSED"
        else
            err "❌ Release Gate: BLOCKED"
            echo "$RELEASE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for issue in d.get('blocking_issues', []):
    print(f'   阻断原因: {issue}')
" 2>/dev/null || true
            die "Release Gate 阻断部署，请解决上述问题后重试。"
        fi
    fi
fi

# ══════════════════════════════════════════════
# 步骤 1: git pull — 拉取最新代码
# ══════════════════════════════════════════════
if [ "$MODE" != "--restart-only" ]; then
    step "步骤 1/5: git pull"

    if ! command -v git &>/dev/null; then
        die "git 不可用，请先安装 git"
    fi

    # 检查是否在 git 仓库中
    if ! git rev-parse --git-dir &>/dev/null; then
        die "当前目录不是 git 仓库: $(pwd)"
    fi

    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    log "当前分支: ${BRANCH}"

    # CI/CD 无交互：使用 --ff-only 避免合并冲突静默失败
    if ! git pull --ff-only 2>&1; then
        die "git pull 失败（可能存在未提交的本地修改或非快进合并）。\n   请先执行 git stash 或解决冲突后重试。"
    fi

    NEW_COMMIT=$(git rev-parse HEAD)
    log "✅ git pull 完成 → ${NEW_COMMIT:0:8}"
fi

# ══════════════════════════════════════════════
# 步骤 2: pip install — 安装依赖
# ══════════════════════════════════════════════
step "步骤 2/5: pip install -r requirements.txt"

if [ ! -f "requirements.txt" ]; then
    warn "requirements.txt 不存在，跳过依赖安装"
else
    if ! command -v "$PYTHON" &>/dev/null; then
        die "Python 解释器 '$PYTHON' 不可用"
    fi

    # CI/CD 无交互：--no-input，不使用 sudo
    if ! "$PYTHON" -m pip install -r requirements.txt \
            --no-input \
            --quiet \
            --disable-pip-version-check \
            2>&1; then
        die "pip install 失败。\n   提示: 如果是权限问题，请使用虚拟环境 (venv) 而非 sudo pip。"
    fi
    log "✅ 依赖安装完成"
fi

# ══════════════════════════════════════════════
# 步骤 3: migration — 数据库迁移（如有）
# ══════════════════════════════════════════════
step "步骤 3/5: Migration"

MIGRATION_DONE=false

# 优先级: alembic > django manage.py migrate > flask db upgrade > 自定义脚本
if command -v alembic &>/dev/null && [ -f "alembic.ini" ]; then
    log "检测到 alembic，执行 alembic upgrade head..."
    if ! alembic upgrade head 2>&1; then
        die "alembic upgrade head 失败"
    fi
    log "✅ alembic migration 完成"
    MIGRATION_DONE=true

elif [ -f "manage.py" ] && "$PYTHON" manage.py help migrate &>/dev/null 2>&1; then
    log "检测到 Django manage.py，执行 migrate..."
    if ! "$PYTHON" manage.py migrate --noinput 2>&1; then
        die "Django migrate 失败"
    fi
    log "✅ Django migration 完成"
    MIGRATION_DONE=true

elif command -v flask &>/dev/null && flask db --help &>/dev/null 2>&1; then
    log "检测到 Flask-Migrate，执行 flask db upgrade..."
    if ! flask db upgrade 2>&1; then
        die "flask db upgrade 失败"
    fi
    log "✅ Flask migration 完成"
    MIGRATION_DONE=true

elif [ -f "scripts/migrate.sh" ]; then
    log "检测到 scripts/migrate.sh，执行自定义迁移..."
    if ! bash scripts/migrate.sh 2>&1; then
        die "scripts/migrate.sh 迁移失败"
    fi
    log "✅ 自定义 migration 完成"
    MIGRATION_DONE=true
fi

if [ "$MIGRATION_DONE" = false ]; then
    log "未检测到迁移工具，跳过 migration 步骤"
fi

# ══════════════════════════════════════════════
# 步骤 4: 重启服务
# ══════════════════════════════════════════════
step "步骤 4/5: 重启服务 (${SERVICE_MANAGER})"

# 自动探测服务管理器
if [ "$SERVICE_MANAGER" = "auto" ]; then
    if command -v systemctl &>/dev/null && systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        SERVICE_MANAGER="systemd"
    elif command -v supervisorctl &>/dev/null && supervisorctl status "${SERVICE_NAME}" &>/dev/null 2>&1; then
        SERVICE_MANAGER="supervisor"
    elif [ -f "${PROCESS_PID_FILE:-}" ]; then
        SERVICE_MANAGER="process"
    elif [ -f "scripts/startup.sh" ]; then
        SERVICE_MANAGER="startup_sh"
    else
        SERVICE_MANAGER="none"
        warn "未检测到服务管理器，跳过服务重启"
    fi
    log "探测到服务管理器: ${SERVICE_MANAGER}"
fi

case "$SERVICE_MANAGER" in
    systemd)
        log "使用 systemd 重启 ${SERVICE_NAME}..."
        # CI/CD: systemd 操作通常需要 sudo，用户应提前配置 sudoers NOPASSWD
        if ! systemctl restart "${SERVICE_NAME}" 2>&1; then
            die "systemctl restart ${SERVICE_NAME} 失败。\n   提示: 确保已配置 sudoers 或以具备权限的用户运行。"
        fi
        # 等待服务就绪
        sleep 2
        if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
            die "服务 ${SERVICE_NAME} 重启后未能正常运行，请检查 journalctl -u ${SERVICE_NAME}"
        fi
        log "✅ systemd 服务 ${SERVICE_NAME} 重启成功"
        ;;

    supervisor)
        log "使用 supervisor 重启 ${SERVICE_NAME}..."
        if ! supervisorctl restart "${SERVICE_NAME}" 2>&1; then
            die "supervisorctl restart ${SERVICE_NAME} 失败"
        fi
        sleep 2
        STATUS=$(supervisorctl status "${SERVICE_NAME}" 2>/dev/null | awk '{print $2}' || echo "UNKNOWN")
        if [ "$STATUS" != "RUNNING" ]; then
            die "Supervisor 服务 ${SERVICE_NAME} 重启后状态异常: ${STATUS}"
        fi
        log "✅ supervisor 服务 ${SERVICE_NAME} 重启成功 (${STATUS})"
        ;;

    process)
        log "使用 PID 文件重启进程..."
        if [ -z "${PROCESS_PID_FILE}" ]; then
            die "SERVICE_MANAGER=process 但 PROCESS_PID_FILE 未设置"
        fi
        if [ -f "${PROCESS_PID_FILE}" ]; then
            OLD_PID=$(cat "${PROCESS_PID_FILE}")
            if kill -0 "$OLD_PID" 2>/dev/null; then
                log "停止进程 PID=${OLD_PID}..."
                kill "$OLD_PID" 2>/dev/null || true
                sleep 2
                kill -9 "$OLD_PID" 2>/dev/null || true
            fi
            rm -f "${PROCESS_PID_FILE}"
        fi
        if [ -f "scripts/startup.sh" ]; then
            bash scripts/startup.sh &
            NEW_PID=$!
            echo "$NEW_PID" > "${PROCESS_PID_FILE}"
            sleep 2
            if ! kill -0 "$NEW_PID" 2>/dev/null; then
                die "新进程启动后立即退出，请检查 scripts/startup.sh"
            fi
            log "✅ 进程重启成功 PID=${NEW_PID}"
        else
            die "SERVICE_MANAGER=process 但 scripts/startup.sh 不存在"
        fi
        ;;

    startup_sh)
        log "调用 scripts/startup.sh 重启服务..."
        if ! bash scripts/startup.sh 2>&1; then
            die "scripts/startup.sh 执行失败"
        fi
        sleep 2
        log "✅ 服务通过 startup.sh 重启"
        ;;

    none)
        warn "SERVICE_MANAGER=none，跳过服务重启"
        warn "如需自动重启，请设置环境变量: SERVICE_MANAGER=systemd|supervisor|process"
        ;;

    *)
        die "未知的 SERVICE_MANAGER: ${SERVICE_MANAGER}（支持: auto/systemd/supervisor/process/none）"
        ;;
esac

# ══════════════════════════════════════════════
# 步骤 5: e2e-verify — 验证部署成功
# ══════════════════════════════════════════════
step "步骤 5/5: E2E Verify"

E2E_SCRIPT="scripts/e2e-verify.sh"

if [ ! -f "$E2E_SCRIPT" ]; then
    die "验证脚本不存在: ${E2E_SCRIPT}"
fi

if [ -n "$SKIP_E2E" ] || [ "$MODE" = "--skip-e2e" ]; then
    warn "⚠️  SKIP_E2E 已设置，跳过 E2E 验证（非推荐）"
    warn "   CI/CD 中建议记录跳过原因。"
else
    # 传递环境变量给 e2e-verify.sh
    if ! GOV_COORDINATOR_TOKEN="${COORD}" \
         GOV_PROJECT_ID="${PROJECT}" \
         NGINX_PORT="${NGINX_PORT}" \
         bash "${E2E_SCRIPT}" 2>&1; then
        die "E2E 验证失败。\n   - 检查 API 端点是否可达\n   - 检查服务日志\n   - 运行: bash ${E2E_SCRIPT} 查看详情"
    fi
    log "✅ E2E Verify: PASSED"
fi

# ══════════════════════════════════════════════
# 部署完成
# ══════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✅ amingClaw 部署成功                  ║${NC}"
echo -e "${GREEN}║   $(date '+%Y-%m-%d %H:%M:%S')                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

exit 0
