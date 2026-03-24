#!/bin/bash
# Setup production deploy guard for aming-claw
# Installs Claude Code hooks to prevent direct docker compose commands
# Usage: bash scripts/setup-guard.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "  Aming Claw — Deploy Guard Setup"
echo "=========================================="

# 1. Create hook script
HOOK_DIR="$PROJECT_DIR/scripts"
HOOK_FILE="$HOOK_DIR/check-deploy-guard.sh"

cat > "$HOOK_FILE" << 'HOOKEOF'
#!/bin/bash
# Pre-tool-use hook: block direct docker compose commands
# Called by Claude Code before executing Bash commands

INPUT=$(cat)
CMD=$(echo "$INPUT" | grep -o '"command":"[^"]*"' | head -1 | sed 's/"command":"//;s/"//')

if echo "$CMD" | grep -qiE "docker\s+compose\s+(up|down|restart|build)"; then
    if ! echo "$CMD" | grep -q "deploy.sh"; then
        echo '{"decision":"block","reason":"Direct docker compose commands are blocked. Use: bash scripts/deploy.sh"}' >&2
        exit 2
    fi
fi
HOOKEOF
chmod +x "$HOOK_FILE"
echo "[+] Hook script created: $HOOK_FILE"

# 2. Update .claude/settings.json
SETTINGS_DIR="$PROJECT_DIR/.claude"
SETTINGS_FILE="$SETTINGS_DIR/settings.json"
mkdir -p "$SETTINGS_DIR"

if [ -f "$SETTINGS_FILE" ]; then
    # Backup existing
    cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"
    echo "[i] Backed up existing settings to $SETTINGS_FILE.bak"
fi

cat > "$SETTINGS_FILE" << SETTINGSEOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hook": "$HOOK_FILE"
      }
    ]
  }
}
SETTINGSEOF
echo "[+] Settings updated: $SETTINGS_FILE"

# 3. Update CLAUDE.md
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
if [ -f "$CLAUDE_MD" ]; then
    if ! grep -q "deploy guard" "$CLAUDE_MD"; then
        cat >> "$CLAUDE_MD" << 'CLAUDEEOF'

## Deploy Guard (Production)

CRITICAL: Direct docker compose up/down/restart/build is PROHIBITED.
Must use: bash scripts/deploy.sh
Reason: E2E/gatekeeper/pre-deploy checks must pass before deployment.
CLAUDEEOF
        echo "[+] CLAUDE.md updated with deploy guard notice"
    else
        echo "[i] CLAUDE.md already has deploy guard notice"
    fi
else
    echo "[i] No CLAUDE.md found, skipping"
fi

echo ""
echo "=========================================="
echo "  Deploy Guard Installed"
echo "=========================================="
echo ""
echo "  Hook: $HOOK_FILE"
echo "  Settings: $SETTINGS_FILE"
echo "  Deploy command: bash scripts/deploy.sh"
echo ""
echo "  To uninstall: rm $HOOK_FILE $SETTINGS_FILE"
echo "=========================================="
