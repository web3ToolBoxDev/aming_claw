"""Aming Claw MCP Server — Worker Pool + Event Push.

Single entry point that manages:
  - Worker pool (Claude CLI task execution)
  - Event bridge (Redis → MCP notifications)
  - MCP tools (task/workflow/executor management)

Talks to existing governance HTTP API. Does NOT replace it.

Usage:
    python -m agent.mcp.server --project aming-claw
    python -m agent.mcp.server --project aming-claw --workers 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# Ensure agent package is importable
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from mcp.tools import TOOLS, ToolDispatcher
from mcp.executor import WorkerPool
from mcp.events import EventBridge

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw"
SERVER_VERSION = "1.1.0"

# JSON-RPC error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# Thread-safe stdio transport
# ---------------------------------------------------------------------------
_stdout_lock = threading.Lock()


def _write(msg: dict) -> None:
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
    _write({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class AmingClawMCP:
    """MCP Server main class."""

    def __init__(self, project_id: str, governance_url: str, workspace: str,
                 redis_url: str, max_workers: int = 1):
        self.project_id = project_id
        self.gov_url = governance_url.rstrip("/")

        # Worker pool
        self.worker_pool = WorkerPool(
            governance_url=governance_url,
            project_id=project_id,
            workspace=workspace,
            max_workers=max_workers,
            on_event=self._on_worker_event,
        )

        # Event bridge (Redis → MCP notifications)
        self.event_bridge = EventBridge(
            redis_url=redis_url,
            notify_fn=self._on_redis_event,
        )

        # Tool dispatcher
        self.dispatcher = ToolDispatcher(
            api_fn=self._http,
            worker_pool=self.worker_pool,
        )

    def run(self) -> None:
        """Start services, enter stdin read loop, shutdown on EOF."""
        log.info("Starting Aming Claw MCP Server (project=%s, gov=%s)",
                 self.project_id, self.gov_url)

        # Start subsystems
        self.worker_pool.start()
        self.event_bridge.start()

        # Read stdin (JSON-RPC messages from Claude Code)
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self._handle(raw)
                except Exception:
                    log.exception("Error handling message: %s", raw[:200])
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        log.info("Shutting down MCP server...")
        self.event_bridge.stop()
        self.worker_pool.stop(timeout=30)
        log.info("MCP server stopped")

    # -----------------------------------------------------------------------
    # HTTP helper (governance API)
    # -----------------------------------------------------------------------

    def _http(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.gov_url}{path}"
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(url, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
            else:
                req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() if exc.fp else ""
            try:
                return json.loads(raw)
            except Exception:
                return {"error": str(exc), "body": raw}
        except Exception as exc:
            return {"error": str(exc)}

    # -----------------------------------------------------------------------
    # Event callbacks
    # -----------------------------------------------------------------------

    def _on_worker_event(self, event_name: str, payload: dict) -> None:
        """Worker pool emits events (gate.blocked, task.created, etc.)."""
        _notification(f"aming-claw/{event_name}", payload)

    def _on_redis_event(self, event_name: str, payload: dict) -> None:
        """Redis bridge forwards governance events as MCP notifications."""
        _notification(f"aming-claw/{event_name}", payload)

    # -----------------------------------------------------------------------
    # JSON-RPC request handler
    # -----------------------------------------------------------------------

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
            return

        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # --- initialize ---
        if method == "initialize":
            _response(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            })
            return

        # --- notifications/initialized ---
        if method == "notifications/initialized":
            return

        # --- tools/list ---
        if method == "tools/list":
            _response(req_id, {"tools": TOOLS})
            return

        # --- tools/call ---
        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            try:
                result = self.dispatcher.dispatch(tool_name, tool_args)
                _response(req_id, {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                    ],
                })
            except ValueError as exc:
                _error_response(req_id, METHOD_NOT_FOUND, str(exc))
            except Exception as exc:
                log.exception("Tool error: %s", tool_name)
                _error_response(req_id, INTERNAL_ERROR, str(exc))
            return

        # --- ping ---
        if method == "ping":
            _response(req_id, {})
            return

        # --- unknown ---
        if req_id is not None:
            _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aming Claw MCP Server")
    parser.add_argument("--project", default="aming-claw", help="Project ID")
    parser.add_argument("--governance-url", default=os.getenv("GOVERNANCE_URL", "http://localhost:40006"))
    parser.add_argument("--workspace", default=os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parents[2])))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:40079/0"))
    parser.add_argument("--workers", type=int, default=int(os.getenv("MCP_WORKERS", "1")))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,  # MCP protocol uses stdout, logs go to stderr
    )

    server = AmingClawMCP(
        project_id=args.project,
        governance_url=args.governance_url,
        workspace=args.workspace,
        redis_url=args.redis_url,
        max_workers=args.workers,
    )
    server.run()


if __name__ == "__main__":
    main()
