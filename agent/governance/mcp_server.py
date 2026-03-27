"""MCP (Model Context Protocol) server for the governance service.

Implements JSON-RPC 2.0 over stdio transport (per MCP spec).

Capabilities:
  - initialize / initialized handshake
  - tools/list  → returns registered governance tools
  - tools/call  → dispatches to governance API
  - Subscribes to Redis Pub/Sub and forwards events as MCP notifications

Usage:
    python -m agent.governance.mcp_server
  or
    python agent/governance/mcp_server.py

Environment variables:
    REDIS_URL          Redis connection URL (default: redis://localhost:6379/0)
    GOVERNANCE_URL     Governance HTTP base URL (default: http://localhost:40006)
    GOV_TOKEN          Bearer token for governance API calls
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the agent package root is on sys.path so relative imports work when
# the file is executed directly (python mcp_server.py).
# ---------------------------------------------------------------------------
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw-governance"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "name": "gov_node_list",
        "description": "List all workflow nodes in a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "gov_node_status_update",
        "description": "Update the verify status of a workflow node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
                },
            },
            "required": ["project_id", "node_id", "status"],
        },
    },
    {
        "name": "gov_gate_check",
        "description": "Check whether all gates for a node are satisfied.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
            },
            "required": ["project_id", "node_id"],
        },
    },
    {
        "name": "gov_memory_write",
        "description": "Append a memory entry (decision, pitfall, workaround…) to the project knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern", "api", "stub"],
                },
                "content": {"type": "string"},
                "author": {"type": "string"},
            },
            "required": ["project_id", "node_id", "kind", "content"],
        },
    },
]

# ---------------------------------------------------------------------------
# Governance HTTP client helpers
# ---------------------------------------------------------------------------

def _gov_url() -> str:
    return os.environ.get("GOVERNANCE_URL", "http://localhost:40006").rstrip("/")


def _gov_token() -> str:
    return os.environ.get("GOV_TOKEN", "")


def _http(method: str, path: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the governance service."""
    url = f"{_gov_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Gov-Token": _gov_token(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": str(exc), "body": raw}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict) -> Any:
    """Dispatch a tools/call to the governance HTTP API."""
    if name == "gov_node_list":
        pid = args["project_id"]
        return _http("GET", f"/api/wf/{pid}/nodes")

    if name == "gov_node_status_update":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("POST", f"/api/wf/{pid}/nodes/{nid}/status", {"status": args["status"]})

    if name == "gov_gate_check":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("GET", f"/api/wf/{pid}/gates/{nid}")

    if name == "gov_memory_write":
        pid = args["project_id"]
        return _http("POST", f"/api/wf/{pid}/memory", args)

    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Stdio transport — thread-safe output
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()


def _write(msg: dict) -> None:
    """Serialize *msg* as a single JSON line and write to stdout."""
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _response(req_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _write({"jsonrpc": "2.0", "id": req_id, "error": err})


def _notification(method: str, params: dict) -> None:
    """Send a server-initiated notification (no id field)."""
    _write({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _handle(raw: str) -> None:
    """Parse and handle one JSON-RPC message."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
        return

    req_id = msg.get("id")  # None for notifications from client
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # -----------------------------------------------------------------------
    # initialize
    # -----------------------------------------------------------------------
    if method == "initialize":
        _response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })
        return

    # -----------------------------------------------------------------------
    # notifications/initialized  (client acknowledges initialize)
    # -----------------------------------------------------------------------
    if method == "notifications/initialized":
        # No response required for notifications
        return

    # -----------------------------------------------------------------------
    # tools/list
    # -----------------------------------------------------------------------
    if method == "tools/list":
        _response(req_id, {"tools": TOOLS})
        return

    # -----------------------------------------------------------------------
    # tools/call
    # -----------------------------------------------------------------------
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            result = _dispatch_tool(tool_name, tool_args)
            _response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                ],
            })
        except ValueError as exc:
            _error_response(req_id, METHOD_NOT_FOUND, str(exc))
        except Exception as exc:
            log.exception("Tool dispatch error: %s", tool_name)
            _error_response(req_id, INTERNAL_ERROR, str(exc))
        return

    # -----------------------------------------------------------------------
    # ping
    # -----------------------------------------------------------------------
    if method == "ping":
        _response(req_id, {})
        return

    # -----------------------------------------------------------------------
    # Unknown method
    # -----------------------------------------------------------------------
    if req_id is not None:
        _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# Redis event subscriber → MCP notifications
# ---------------------------------------------------------------------------

def _redis_subscriber_thread() -> None:
    """Subscribe to Redis governance events and emit MCP notifications."""
    try:
        from .redis_client import get_redis
        from .event_bus import REDIS_CHANNEL_PREFIX
    except ImportError:
        try:
            # fallback when run as __main__
            from governance.redis_client import get_redis
            from governance.event_bus import REDIS_CHANNEL_PREFIX
        except ImportError:
            log.warning("Cannot import redis_client; Redis notifications disabled.")
            return

    # Retry loop — Redis may not be available at startup
    while True:
        try:
            r = get_redis()
            if not r.available or r._client is None:
                log.debug("Redis not available, retrying in 5s…")
                time.sleep(5)
                continue

            pubsub = r._client.pubsub()
            # Subscribe to the global channel and all project channels (wildcard)
            pubsub.psubscribe(f"{REDIS_CHANNEL_PREFIX}:*")
            log.info("MCP server subscribed to Redis pattern %s:*", REDIS_CHANNEL_PREFIX)

            for raw_msg in pubsub.listen():
                if raw_msg.get("type") not in ("pmessage", "message"):
                    continue
                data = raw_msg.get("data", "")
                if not data:
                    continue
                try:
                    payload = json.loads(data) if isinstance(data, str) else data
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": str(data)}

                _notification("governance/event", {
                    "channel": raw_msg.get("channel", ""),
                    "event": payload.get("event", "unknown"),
                    "payload": payload.get("payload", payload),
                })

        except Exception as exc:
            log.warning("Redis subscriber error (%s), reconnecting in 5s…", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Start the MCP server: read stdin, dispatch, emit notifications."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # Start Redis subscriber in background daemon thread
    t = threading.Thread(target=_redis_subscriber_thread, daemon=True, name="redis-sub")
    t.start()

    log.info("MCP governance server started (PID %d)", os.getpid())

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _handle(raw)
        except Exception:
            log.exception("Unhandled error processing message: %s", raw[:200])


if __name__ == "__main__":
    run()
