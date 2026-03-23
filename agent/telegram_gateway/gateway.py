"""Telegram Gateway - message relay between Telegram and Coordinators.

Architecture:
  1. HTTP API for coordinators: /gateway/bind, /gateway/reply, /gateway/unbind
  2. Telegram long-polling for user messages
  3. Redis Pub/Sub for governance events → Telegram notifications
  4. Redis Pub/Sub for user messages → coordinator callback channels

Flow:
  Coordinator binds:  POST /gateway/bind {token, chat_id}
  User sends message: Telegram → Gateway → Redis chat:inbox:{token_hash}
  Coordinator replies: POST /gateway/reply {token, chat_id, text}
  Gateway sends:      Telegram API sendMessage
"""

import json
import hashlib
import os
import sys
import logging
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gateway")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://governance:40006")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "40010"))

# --- Redis connection ---

_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=5, socket_timeout=3,
            )
            _redis_client.ping()
            log.info("Redis connected: %s", REDIS_URL)
        except Exception as e:
            log.warning("Redis unavailable: %s", e)
            _redis_client = None
    return _redis_client


# --- Telegram helpers ---

def tg_api(method: str, data: dict = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = requests.post(url, json=data or {}, timeout=40)
    return resp.json()


def send_text(chat_id, text: str, **kwargs) -> None:
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"
    tg_api("sendMessage", {"chat_id": chat_id, "text": text, **kwargs})


def poll_updates(offset: int) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={
        "timeout": 30,
        "offset": offset,
        "allowed_updates": '["message","callback_query"]',
    }, timeout=40)
    return resp.json()


# --- Governance API helpers ---

def gov_api(method: str, path: str, data: dict = None, token: str = None) -> dict:
    """Call governance API. If no token provided, auto-uses the bound project_token from route table."""
    url = f"{GOVERNANCE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gov-Token"] = token
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, json=data or {}, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def gov_api_for_chat(chat_id: int, method: str, path: str, data: dict = None) -> dict:
    """Call governance API using the project_token bound to this chat.

    v5: Gateway acts as token proxy — CLI sessions don't need their own token.
    """
    route = get_route(chat_id)
    if not route:
        return {"error": "No project bound to this chat. Use /bind first."}
    token = route.get("token", "")
    if not token:
        return {"error": "No token in route. Re-bind with /bind <token>."}
    return gov_api(method, path, data=data, token=token)


def verify_token(token: str) -> dict | None:
    """Verify coordinator token with governance service. Returns session info or None."""
    result = gov_api("GET", "/api/role/verify", token=token)
    if result.get("error"):
        return None
    return result


# --- Route table (Redis-backed) ---

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def bind_route(chat_id: int, token: str, project_id: str = "") -> None:
    """Bind a chat_id to a coordinator token."""
    r = get_redis()
    if not r:
        return
    th = token_hash(token)
    route_data = json.dumps({
        "token": token,
        "token_hash": th,
        "project_id": project_id,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    # chat_id → coordinator mapping
    r.set(f"chat:route:{chat_id}", route_data)
    # coordinator → chat_id reverse mapping
    r.set(f"chat:reverse:{th}", str(chat_id))
    log.info("Route bound: chat %s → coordinator %s (project: %s)", chat_id, th, project_id)


def unbind_route(chat_id: int) -> bool:
    """Unbind a chat_id."""
    r = get_redis()
    if not r:
        return False
    raw = r.get(f"chat:route:{chat_id}")
    if raw:
        route = json.loads(raw)
        r.delete(f"chat:reverse:{route.get('token_hash', '')}")
    r.delete(f"chat:route:{chat_id}")
    log.info("Route unbound: chat %s", chat_id)
    return True


def get_route(chat_id: int) -> dict | None:
    """Get the coordinator route for a chat_id."""
    r = get_redis()
    if not r:
        return None
    raw = r.get(f"chat:route:{chat_id}")
    if not raw:
        return None
    return json.loads(raw)


def get_chat_id_for_token(token: str) -> int | None:
    """Get the chat_id bound to a token."""
    r = get_redis()
    if not r:
        return None
    th = token_hash(token)
    raw = r.get(f"chat:reverse:{th}")
    if raw:
        return int(raw)
    return None


# --- Forward user message to coordinator ---

def ensure_consumer_group(r, stream_key: str, group: str = "coordinator-group") -> None:
    """Create consumer group if it doesn't exist."""
    try:
        r.xgroup_create(stream_key, group, id="0", mkstream=True)
    except Exception as e:
        # BUSYGROUP = group already exists, that's fine
        if "BUSYGROUP" not in str(e):
            log.warning("Failed to create consumer group: %s", e)


def forward_to_coordinator(chat_id: int, text: str, route: dict, msg: dict = None) -> None:
    """Forward message to Coordinator via task file → Executor → Claude CLI.

    v5.1: Gateway 写 coordinator_chat task 文件到 shared-volume。
    宿主机 Executor 消费并启动 Claude CLI session。
    Executor 完成后直接通过 Gateway API 回复用户。
    """
    project_id = route.get("project_id", "")
    token = route.get("token", "")

    # 1. Assemble context for Coordinator
    context_parts = []

    summary = gov_api("GET", f"/api/wf/{project_id}/summary", token=token)
    total = summary.get("total_nodes", 0)
    by_status = summary.get("by_status", {})
    context_parts.append(f"项目: {project_id}, {total} 节点, 状态: {by_status}")

    runtime = gov_api("GET", f"/api/runtime/{project_id}", token=token)
    active = runtime.get("active_tasks", [])
    queued = runtime.get("queued_tasks", [])
    if active:
        context_parts.append(f"运行中任务: {len(active)}")
    if queued:
        context_parts.append(f"排队任务: {len(queued)}")

    ctx = gov_api("GET", f"/api/context/{project_id}/load", token=token)
    ctx_data = ctx.get("context")
    if ctx_data and ctx_data.get("current_focus"):
        context_parts.append(f"当前焦点: {ctx_data['current_focus']}")

    # Memories from dbservice
    try:
        import requests as _req
        dbservice_url = os.environ.get("DBSERVICE_URL", "http://dbservice:40002")
        mem_resp = _req.post(f"{dbservice_url}/knowledge/search",
            json={"query": text[:100], "scope": project_id, "limit": 3},
            timeout=3)
        memories = mem_resp.json().get("results", [])
        if memories:
            mem_texts = [m["doc"]["content"][:100] for m in memories[:3]]
            context_parts.append(f"相关记忆: {'; '.join(mem_texts)}")
    except Exception:
        pass

    context = "\n".join(context_parts)

    # 2. Write coordinator_chat task file (atomic)
    from datetime import datetime, timezone
    import uuid as _uuid

    task_id = f"coord-{int(datetime.now(timezone.utc).timestamp())}-{_uuid.uuid4().hex[:6]}"
    task_data = {
        "task_id": task_id,
        "chat_id": chat_id,
        "project_id": project_id,
        "text": text,
        "prompt": text,
        "action": "coordinator_chat",
        "type": "coordinator_chat",
        "_gov_token": token,
        "_coordinator_context": context,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    shared_vol = os.environ.get("SHARED_VOLUME_PATH", "/app/shared-volume")
    pending_dir = os.path.join(shared_vol, "codex-tasks", "pending")
    os.makedirs(pending_dir, exist_ok=True)

    tmp_path = os.path.join(pending_dir, f"{task_id}.tmp.json")
    final_path = os.path.join(pending_dir, f"{task_id}.json")

    with open(tmp_path, "w") as f:
        json.dump(task_data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, final_path)

    send_text(chat_id, "正在处理...")
    log.info("Coordinator task created: %s for chat %d", task_id, chat_id)


# --- Inline Keyboard helpers ---

def make_inline_keyboard(buttons: list[list[dict]]) -> dict:
    """Build Telegram InlineKeyboardMarkup."""
    return {"inline_keyboard": buttons}


def send_menu(chat_id: int, text: str, keyboard: dict) -> None:
    """Send message with inline keyboard."""
    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard,
    })


def answer_callback(callback_query_id: str, text: str = "") -> None:
    tg_api("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


# --- Get all registered coordinators from Redis ---

def list_all_routes() -> list[dict]:
    """List all bound coordinator routes."""
    r = get_redis()
    if not r:
        return []
    routes = []
    for key in r.scan_iter("chat:route:*"):
        raw = r.get(key)
        if raw:
            route = json.loads(raw)
            cid = key.split(":")[-1]
            routes.append({"chat_id": cid, **route})
    return routes


# --- Menu builders ---

def build_main_menu(chat_id: int) -> tuple[str, dict]:
    """Build main menu with runtime status."""
    route = get_route(chat_id)
    routes = list_all_routes()

    if route:
        project = route.get("project_id", "unknown")
        token = route.get("token", "")
        # Get runtime status
        runtime = gov_api("GET", f"/api/runtime/{project}", token=token)
        summary = runtime.get("summary", {})
        active = summary.get("active", 0)
        queued = summary.get("queued", 0)
        pending = summary.get("pending_notify", 0)
        runtime_line = ""
        if active: runtime_line += f" {active} 运行中"
        if queued: runtime_line += f" {queued} 排队"
        if pending: runtime_line += f" {pending} 未读"
        status_line = f"当前: {project}" + (f" [{runtime_line.strip()}]" if runtime_line else " [空闲]")
    else:
        status_line = "当前: 未绑定任何 Coordinator"

    lines = [
        "Aming Claw Gateway",
        "",
        status_line,
        f"已注册 Coordinator: {len(routes)}",
    ]

    buttons = []

    # Project buttons with status
    if routes:
        for r in routes:
            proj = r.get("project_id", "?")
            th = r.get("token_hash", "")[:8]
            is_active = route and route.get("token_hash") == r.get("token_hash")
            # Get node stats for each project
            proj_summary = gov_api("GET", f"/api/wf/{proj}/summary")
            total = proj_summary.get("total_nodes", 0)
            passed = proj_summary.get("by_status", {}).get("qa_pass", 0)
            pct = int(passed / total * 100) if total else 0
            prefix = ">> " if is_active else ""
            label = f"{prefix}{proj} ({total}节点 {pct}%)"
            buttons.append([{"text": label, "callback_data": f"switch:{r.get('token_hash', '')}"}])

    buttons.append([
        {"text": "项目状态", "callback_data": "action:status"},
        {"text": "运行时", "callback_data": "action:runtime"},
    ])
    buttons.append([
        {"text": "项目列表", "callback_data": "action:projects"},
        {"text": "解绑", "callback_data": "action:unbind"},
    ])

    if not routes:
        lines.append("")
        lines.append("请在电脑上启动 Claude session 并绑定:")
        lines.append("  /bind <coordinator_token>")

    return "\n".join(lines), make_inline_keyboard(buttons)


# --- Callback query handler ---

def handle_callback_query(callback: dict) -> None:
    """Handle inline keyboard button presses."""
    cb_id = callback.get("id", "")
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat_id = (msg.get("chat") or {}).get("id")

    if not chat_id or not data:
        answer_callback(cb_id)
        return

    if data.startswith("switch:"):
        th = data[7:]
        # Find route with this token_hash
        routes = list_all_routes()
        target = None
        for r in routes:
            if r.get("token_hash", "") == th:
                target = r
                break

        if target:
            # Rebind this chat to the selected coordinator
            bind_route(int(chat_id), target.get("token", ""), target.get("project_id", ""))
            answer_callback(cb_id, f"已切换到 {target.get('project_id', '?')}")
            # Refresh menu
            text, kb = build_main_menu(int(chat_id))
            send_menu(int(chat_id), text, kb)
        else:
            answer_callback(cb_id, "Coordinator 不存在")
        return

    if data == "action:status":
        route = get_route(int(chat_id))
        project_id = route.get("project_id", "amingClaw") if route else "amingClaw"
        result = gov_api("GET", f"/api/wf/{project_id}/summary")
        if "error" in result:
            answer_callback(cb_id, f"Error: {result['error']}")
            return
        by_status = result.get("by_status", {})
        total = result.get("total_nodes", 0)
        lines = [f"{project_id} ({total} 节点):"]
        for status, count in by_status.items():
            lines.append(f"  {status}: {count}")
        send_text(int(chat_id), "\n".join(lines))
        answer_callback(cb_id)
        return

    if data == "action:projects":
        result = gov_api("GET", "/api/project/list")
        projects = result.get("projects", [])
        if not projects:
            send_text(int(chat_id), "暂无项目")
        else:
            lines = ["项目列表:"]
            for p in projects:
                lines.append(f"  {p['project_id']} ({p.get('node_count', 0)} 节点)")
            send_text(int(chat_id), "\n".join(lines))
        answer_callback(cb_id)
        return

    if data == "action:health":
        result = gov_api("GET", "/api/health")
        send_text(int(chat_id), json.dumps(result, indent=2))
        answer_callback(cb_id)
        return

    if data == "action:runtime":
        route = get_route(int(chat_id))
        if route:
            pid = route.get("project_id", "")
            result = gov_api("GET", f"/api/runtime/{pid}", token=route.get("token", ""))
            s = result.get("summary", {})
            lines = [f"{pid} 运行时:"]
            lines.append(f"  运行中: {s.get('active', 0)}")
            lines.append(f"  排队中: {s.get('queued', 0)}")
            lines.append(f"  待通知: {s.get('pending_notify', 0)}")
            for t in result.get("active_tasks", [])[:3]:
                meta = json.loads(t.get("metadata_json", "{}")) if isinstance(t.get("metadata_json"), str) else t.get("metadata_json", {})
                phase = meta.get("progress_phase", "")
                pct = meta.get("progress_percent", "")
                lines.append(f"  > {t.get('task_id','')} {phase} {pct}%")
            send_text(int(chat_id), "\n".join(lines))
        else:
            send_text(int(chat_id), "未绑定项目")
        answer_callback(cb_id)
        return

    if data == "action:unbind":
        if unbind_route(int(chat_id)):
            answer_callback(cb_id, "已解绑")
            text, kb = build_main_menu(int(chat_id))
            send_menu(int(chat_id), text, kb)
        else:
            answer_callback(cb_id, "当前没有绑定")
        return

    answer_callback(cb_id)


# --- Command handlers ---

HELP_TEXT = """Aming Claw Gateway

/menu - 交互式菜单
/bind <token> - 绑定 Coordinator
/unbind - 解绑当前 Coordinator
/status [project] - 查看项目状态
/projects - 列出所有项目
/health - 服务健康检查
/help - 显示帮助

绑定后直接发送文本将转发给 Coordinator。"""


def handle_message(chat_id: int, text: str, msg: dict = None) -> None:
    """Route incoming message."""
    if not text:
        return

    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        send_text(chat_id, HELP_TEXT)
        return

    if cmd == "/menu":
        text_body, kb = build_main_menu(chat_id)
        send_menu(chat_id, text_body, kb)
        return

    if cmd == "/bind":
        if not args:
            send_text(chat_id, "用法: /bind <coordinator_token>")
            return
        token = args.strip()
        session = verify_token(token)
        if not session:
            send_text(chat_id, "Token 验证失败，请检查 token 是否正确")
            return

        # Auto-save old project context before switching
        old_route = get_route(chat_id)
        if old_route and old_route.get("project_id"):
            old_pid = old_route["project_id"]
            old_token = old_route.get("token", "")
            try:
                gov_api("POST", f"/api/context/{old_pid}/save",
                    data={"context": {"saved_reason": "project_switch", "switched_to": session.get("project_id", "")}},
                    token=old_token)
            except Exception:
                pass

        project_id = session.get("project_id", "")
        role = session.get("role", "")
        bind_route(chat_id, token, project_id)

        # Load new project context
        ctx_result = gov_api("GET", f"/api/context/{project_id}/load", token=token)
        ctx = ctx_result.get("context")
        ctx_info = ""
        if ctx and ctx.get("current_focus"):
            ctx_info = f"\n  上次焦点: {ctx['current_focus']}"

        send_text(chat_id,
            f"已绑定 {project_id}\n"
            f"  角色: {role}{ctx_info}\n\n"
            f"发送消息即可操作。")
        return

    if cmd == "/unbind":
        if unbind_route(chat_id):
            send_text(chat_id, "已解绑 Coordinator")
        else:
            send_text(chat_id, "当前没有绑定")
        return

    if cmd == "/health":
        result = gov_api("GET", "/api/health")
        send_text(chat_id, json.dumps(result, indent=2))
        return

    if cmd == "/projects":
        result = gov_api("GET", "/api/project/list")
        projects = result.get("projects", [])
        if not projects:
            send_text(chat_id, "暂无项目")
            return
        lines = ["项目列表:"]
        for p in projects:
            lines.append(f"  {p['project_id']} ({p.get('node_count', 0)} 节点)")
        send_text(chat_id, "\n".join(lines))
        return

    if cmd == "/status":
        project_id = args or "amingClaw"
        route = get_route(chat_id)
        if route and not args:
            project_id = route.get("project_id") or project_id
        result = gov_api("GET", f"/api/wf/{project_id}/summary")
        if "error" in result:
            send_text(chat_id, f"Error: {result['error']}")
            return
        by_status = result.get("by_status", {})
        total = result.get("total_nodes", 0)
        lines = [f"{project_id} ({total} 节点):"]
        for status, count in by_status.items():
            lines.append(f"  {status}: {count}")
        send_text(chat_id, "\n".join(lines))
        return

    # Not a command → forward to Coordinator
    # v5.1: Gateway 不做决策，所有非命令消息全部转给 Coordinator
    if text.startswith("/"):
        send_text(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助")
        return

    route = get_route(chat_id)
    if not route:
        text_body, kb = build_main_menu(chat_id)
        send_menu(chat_id, text_body, kb)
        return

    # 所有非命令消息 → Coordinator 处理
    forward_to_coordinator(chat_id, text, route, msg)


# --- Message Classifier (two-stage) ---

import re

def classify_message(text: str) -> str:
    """Two-stage classifier: rules first, keyword fallback."""
    # Stage 1: Rules
    danger_kw = ["rollback", "delete", "revoke", "release", "deploy",
                 "回滚", "删除", "发布", "撤销", "rm -rf"]
    if any(kw in text.lower() for kw in danger_kw):
        return "dangerous"

    query_patterns = [
        r"(状态|status|进度|progress)\s*(怎么样|是什么|查|看|？|\?)",
        r"(多少|几个|有没有)\s*(节点|node|任务|task|pending)",
        r"(列表|list|列出|查看|显示|show)",
        r"什么情况|当前|目前|现在.*怎么",
    ]
    for p in query_patterns:
        if re.search(p, text, re.I):
            return "query"

    # Stage 2: Keyword fallback
    task_kw = ["帮我", "写", "改", "修", "创建", "实现", "优化", "添加",
               "测试", "fix", "add", "create", "implement", "update", "build"]
    if any(kw in text.lower() for kw in task_kw):
        return "task"

    return "chat"


def handle_query(chat_id: int, text: str, route: dict) -> None:
    """Handle query-type messages: call API and reply directly."""
    project_id = route.get("project_id", "")
    token = route.get("token", "")

    # Try to detect what they're asking about
    if any(kw in text for kw in ["运行", "任务", "task", "进度", "runtime"]):
        result = gov_api("GET", f"/api/runtime/{project_id}", token=token)
        summary = result.get("summary", {})
        lines = [f"{project_id} 运行时:"]
        lines.append(f"  运行中: {summary.get('active', 0)}")
        lines.append(f"  排队中: {summary.get('queued', 0)}")
        lines.append(f"  待通知: {summary.get('pending_notify', 0)}")
        send_text(chat_id, "\n".join(lines))
    else:
        result = gov_api("GET", f"/api/wf/{project_id}/summary", token=token)
        if "error" in result:
            send_text(chat_id, f"Error: {result['error']}")
            return
        by_status = result.get("by_status", {})
        total = result.get("total_nodes", 0)
        lines = [f"{project_id} ({total} 节点):"]
        for status, count in by_status.items():
            lines.append(f"  {status}: {count}")
        send_text(chat_id, "\n".join(lines))


def handle_task_dispatch(chat_id: int, text: str, route: dict) -> None:
    """Handle task-type messages: create task in registry + write task file atomically."""
    import uuid as _uuid
    project_id = route.get("project_id", "")
    token = route.get("token", "")

    # 1. Create task in registry (DB first — source of truth)
    result = gov_api("POST", f"/api/task/{project_id}/create",
        data={
            "prompt": text,
            "type": "dev_task",
            "metadata": {"chat_id": chat_id, "source": "telegram"},
        },
        token=token,
    )

    if "error" in result:
        send_text(chat_id, f"任务创建失败: {result['error']}")
        return

    task_id = result.get("task_id", f"task-{_uuid.uuid4().hex[:12]}")

    # 2. Write task file atomically (.tmp → fsync → rename)
    from datetime import datetime, timezone
    task_data = {
        "task_id": task_id,
        "chat_id": chat_id,
        "project_id": project_id,
        "text": text,
        "prompt": text,
        "backend": "claude",
        "action": "claude",
        "type": "dev_task",
        "_gov_token": token,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    shared_vol = os.environ.get("SHARED_VOLUME_PATH", "/app/shared-volume")
    pending_dir = os.path.join(shared_vol, "codex-tasks", "pending")
    os.makedirs(pending_dir, exist_ok=True)

    tmp_path = os.path.join(pending_dir, f"{task_id}.tmp.json")
    final_path = os.path.join(pending_dir, f"{task_id}.json")

    with open(tmp_path, "w") as f:
        json.dump(task_data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, final_path)

    send_text(chat_id, f"已创建任务 {task_id[-8:]}\n等待执行...")


# --- HTTP API for coordinators ---

class GatewayAPIHandler(BaseHTTPRequestHandler):
    """HTTP API for coordinators to reply and manage bindings."""

    def log_message(self, format, *args):
        log.info("HTTP %s", format % args)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _json_response(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.rstrip("/")
        body = self._read_body()

        if path == "/gateway/reply":
            return self._handle_reply(body)
        elif path == "/gateway/bind":
            return self._handle_bind(body)
        elif path == "/gateway/unbind":
            return self._handle_unbind(body)
        else:
            self._json_response(404, {"error": "not_found"})

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/gateway/health":
            self._json_response(200, {"status": "ok", "service": "telegram-gateway"})
        elif path == "/gateway/status":
            self._handle_status()
        else:
            self._json_response(404, {"error": "not_found"})

    def _handle_reply(self, body: dict) -> None:
        """Coordinator sends a reply to Telegram user."""
        token = body.get("token") or self.headers.get("X-Gov-Token", "")
        chat_id = body.get("chat_id")
        text = body.get("text", "")

        if not token:
            self._json_response(401, {"error": "missing token"})
            return
        if not text:
            self._json_response(400, {"error": "missing text"})
            return

        # If no chat_id, look up from route table
        if not chat_id:
            chat_id = get_chat_id_for_token(token)
        if not chat_id:
            self._json_response(400, {"error": "no chat_id bound to this token"})
            return

        try:
            send_text(int(chat_id), text)
            self._json_response(200, {"ok": True, "chat_id": chat_id})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_bind(self, body: dict) -> None:
        """Coordinator binds to a chat_id."""
        token = body.get("token") or self.headers.get("X-Gov-Token", "")
        chat_id = body.get("chat_id")
        project_id = body.get("project_id", "")

        if not token or not chat_id:
            self._json_response(400, {"error": "missing token or chat_id"})
            return

        bind_route(int(chat_id), token, project_id)
        self._json_response(200, {"ok": True, "chat_id": chat_id})

    def _handle_unbind(self, body: dict) -> None:
        """Coordinator unbinds from a chat_id."""
        chat_id = body.get("chat_id")
        if not chat_id:
            self._json_response(400, {"error": "missing chat_id"})
            return
        unbind_route(int(chat_id))
        self._json_response(200, {"ok": True})

    def _handle_status(self) -> None:
        """Return gateway status."""
        r = get_redis()
        routes = []
        if r:
            for key in r.scan_iter("chat:route:*"):
                raw = r.get(key)
                if raw:
                    route = json.loads(raw)
                    chat_id = key.split(":")[-1]
                    routes.append({"chat_id": chat_id, **route})
        self._json_response(200, {
            "status": "ok",
            "active_routes": len(routes),
            "routes": routes,
        })


# --- Event listener (governance events → Telegram) ---

def check_pending_notifications() -> None:
    """Check all projects for completed tasks needing notification."""
    routes = list_all_routes()
    seen_projects = set()
    for route in routes:
        pid = route.get("project_id", "")
        if not pid or pid in seen_projects:
            continue
        seen_projects.add(pid)
        token = route.get("token", "")

        # Query pending notifications
        result = gov_api("GET", f"/api/runtime/{pid}", token=token)
        pending = result.get("pending_notifications", [])

        for task in pending:
            chat_id = None
            meta = task.get("metadata_json", "{}")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            chat_id = meta.get("chat_id")

            if not chat_id:
                continue

            # Build notification
            exec_status = task.get("execution_status", "")
            task_id = task.get("task_id", "")
            if exec_status == "succeeded":
                result_json = task.get("result_json", "{}")
                send_text(int(chat_id), f"任务完成: {task_id[-8:]}\n{str(result_json)[:200]}")
            elif exec_status == "failed":
                err = task.get("error_message", "")
                send_text(int(chat_id), f"任务失败: {task_id[-8:]}\n{err[:200]}")
            else:
                send_text(int(chat_id), f"任务 {task_id[-8:]} 状态: {exec_status}")

            # Mark notified
            gov_api("POST", f"/api/task/{pid}/notify",
                data={"task_id": task_id}, token=token)


def send_notification(text: str) -> None:
    if ADMIN_CHAT_ID:
        try:
            send_text(int(ADMIN_CHAT_ID), text)
        except Exception as e:
            log.error("Notification failed: %s", e)


def start_event_listener() -> None:
    from telegram_gateway.gov_event_listener import GovEventListener
    listener = GovEventListener(REDIS_URL, send_notification)
    if listener.start():
        log.info("Event listener started")
    else:
        log.warning("Event listener disabled (Redis unavailable)")


# --- Response listener (coordinator replies via Redis) ---

def start_response_listener() -> None:
    """Listen for coordinator replies via Redis Pub/Sub."""
    r = get_redis()
    if not r:
        log.warning("Response listener disabled (Redis unavailable)")
        return

    def _listen():
        import redis as redis_lib
        client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
        pubsub = client.pubsub()
        pubsub.psubscribe("chat:outbox:*")
        log.info("Response listener: subscribed to chat:outbox:*")

        for msg in pubsub.listen():
            if msg["type"] not in ("pmessage", "message"):
                continue
            try:
                data = json.loads(msg["data"])
                chat_id = data.get("chat_id")
                text = data.get("text", "")
                if chat_id and text:
                    send_text(int(chat_id), text)
                    log.info("Response sent to chat %s: %s", chat_id, text[:50])
            except Exception as e:
                log.exception("Response listener error: %s", e)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()


# --- Main ---

def run() -> None:
    log.info("Gateway starting...")
    log.info("  GOVERNANCE_URL: %s", GOVERNANCE_URL)
    log.info("  REDIS_URL: %s", REDIS_URL)
    log.info("  ADMIN_CHAT_ID: %s", ADMIN_CHAT_ID)
    log.info("  GATEWAY_PORT: %s", GATEWAY_PORT)

    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set, exiting")
        sys.exit(1)

    # Register bot commands
    tg_api("setMyCommands", {"commands": [
        {"command": "menu", "description": "交互式菜单"},
        {"command": "bind", "description": "绑定 Coordinator"},
        {"command": "unbind", "description": "解绑 Coordinator"},
        {"command": "status", "description": "项目状态"},
        {"command": "projects", "description": "列出项目"},
        {"command": "health", "description": "服务健康"},
        {"command": "help", "description": "显示帮助"},
    ]})
    log.info("Bot commands registered")

    # Start background listeners
    start_event_listener()
    start_response_listener()

    # Start HTTP API server in background thread
    api_server = HTTPServer(("0.0.0.0", GATEWAY_PORT), GatewayAPIHandler)
    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    log.info("HTTP API listening on port %d", GATEWAY_PORT)

    # Main thread: Telegram polling
    offset = 0
    log.info("Telegram polling started")

    while True:
        try:
            data = poll_updates(offset)
            if not data.get("ok"):
                log.error("getUpdates failed: %s", data)
                time.sleep(5)
                continue

            for upd in data.get("result", []):
                update_id = upd.get("update_id", 0)
                if update_id >= offset:
                    offset = update_id + 1

                # Handle callback queries (inline keyboard)
                cb = upd.get("callback_query")
                if cb:
                    try:
                        handle_callback_query(cb)
                    except Exception as e:
                        log.exception("Callback error: %s", e)
                    continue

                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")

                if not chat_id or not text:
                    continue

                try:
                    handle_message(chat_id, text, msg)
                except Exception as e:
                    log.exception("Error handling message: %s", e)
                    send_text(chat_id, f"处理失败: {str(e)[:200]}")

            # Check pending notifications (task completions)
            try:
                check_pending_notifications()
            except Exception:
                pass

        except KeyboardInterrupt:
            log.info("Stopped")
            api_server.shutdown()
            return
        except Exception as e:
            log.error("Poll error: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    run()
