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
    """Push user message to coordinator's inbox via Redis Stream (XADD)."""
    r = get_redis()
    if not r:
        send_text(chat_id, "Redis 不可用，无法转发消息")
        return

    th = route.get("token_hash", "")
    stream_key = f"chat:inbox:{th}"

    # Ensure consumer group exists
    ensure_consumer_group(r, stream_key)

    # XADD to stream
    entry = {
        "chat_id": str(chat_id),
        "text": text,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message_id": str((msg or {}).get("message_id", "")),
    }
    try:
        msg_id = r.xadd(stream_key, entry, maxlen=1000)
        log.info("XADD %s → %s: %s", stream_key, msg_id, text[:50])
    except Exception as e:
        log.error("XADD failed: %s", e)
        send_text(chat_id, "消息入队失败，请重试")


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
    """Build main menu with current status."""
    route = get_route(chat_id)
    routes = list_all_routes()

    if route:
        project = route.get("project_id", "unknown")
        th = route.get("token_hash", "")[:8]
        status_line = f"当前: {project} (coordinator: {th}...)"
    else:
        status_line = "当前: 未绑定任何 Coordinator"

    lines = [
        "Aming Claw Gateway",
        "",
        status_line,
        f"已注册 Coordinator: {len(routes)}",
    ]

    buttons = []

    # Switch coordinator buttons
    if routes:
        for r in routes:
            proj = r.get("project_id", "?")
            th = r.get("token_hash", "")[:8]
            is_active = route and route.get("token_hash") == r.get("token_hash")
            label = f"{'>> ' if is_active else ''}{proj} ({th})"
            buttons.append([{"text": label, "callback_data": f"switch:{r.get('token_hash', '')}"}])

    # Action buttons
    buttons.append([
        {"text": "项目状态", "callback_data": "action:status"},
        {"text": "项目列表", "callback_data": "action:projects"},
    ])
    buttons.append([
        {"text": "服务健康", "callback_data": "action:health"},
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
        project_id = session.get("project_id", "")
        role = session.get("role", "")
        bind_route(chat_id, token, project_id)
        send_text(chat_id,
            f"已绑定 Coordinator\n"
            f"  项目: {project_id}\n"
            f"  角色: {role}\n"
            f"  Token: {token[:20]}...\n\n"
            f"现在发送的消息将转发给 Coordinator。")
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

    # Not a command → forward to coordinator if bound
    if text.startswith("/"):
        send_text(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助")
        return

    route = get_route(chat_id)
    if not route:
        # Show menu with hint
        text_body, kb = build_main_menu(chat_id)
        send_menu(chat_id, text_body, kb)
        return

    forward_to_coordinator(chat_id, text, route, msg)


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

        except KeyboardInterrupt:
            log.info("Stopped")
            api_server.shutdown()
            return
        except Exception as e:
            log.error("Poll error: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    run()
