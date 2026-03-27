"""MCP Tool definitions and dispatch for Aming Claw.

All tools proxy to the governance HTTP API or the in-process worker pool.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema definitions (per MCP spec)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Task Management ---
    {
        "name": "task_create",
        "description": "Create a new task in the governance queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
                "prompt": {"type": "string", "description": "Task description/instructions"},
                "type": {"type": "string", "enum": ["pm", "dev", "test", "qa", "merge", "task"],
                         "description": "Task type (determines role and chain stage)"},
                "priority": {"type": "integer", "description": "Priority (1=highest)", "default": 5},
                "metadata": {"type": "object", "description": "Additional metadata (target_files, etc.)"},
            },
            "required": ["project_id", "prompt", "type"],
        },
    },
    {
        "name": "task_list",
        "description": "List tasks in a project, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Filter: queued, claimed, succeeded, failed"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_claim",
        "description": "Manually claim the next queued task (Observer takeover).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "worker_id": {"type": "string", "default": "observer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_complete",
        "description": "Mark a task as complete (triggers auto-chain to next stage).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["succeeded", "failed"]},
                "result": {"type": "object", "description": "Task result (changed_files, test_report, etc.)"},
            },
            "required": ["project_id", "task_id", "status"],
        },
    },
    # --- Workflow / Nodes ---
    {
        "name": "wf_summary",
        "description": "Get workflow node status summary (pending/testing/t2_pass/qa_pass/waived counts).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "wf_impact",
        "description": "Analyze impact of file changes on workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "files": {"type": "string", "description": "Comma-separated file paths"},
            },
            "required": ["project_id", "files"],
        },
    },
    {
        "name": "node_update",
        "description": "Update verification status of workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "Node IDs"},
                "status": {"type": "string", "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived"]},
                "evidence": {"type": "object", "description": "Evidence for the status change"},
            },
            "required": ["project_id", "nodes", "status"],
        },
    },
    # --- Executor ---
    {
        "name": "executor_status",
        "description": "Get worker pool status (workers, active tasks, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "executor_scale",
        "description": "Set the number of worker threads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workers": {"type": "integer", "description": "Target worker count", "minimum": 0, "maximum": 10},
            },
            "required": ["workers"],
        },
    },
    # --- System ---
    {
        "name": "health",
        "description": "Check governance service health and version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """Routes MCP tool calls to governance API or in-process worker pool."""

    def __init__(self, api_fn, worker_pool):
        """
        Args:
            api_fn: Callable(method, path, data) → dict (HTTP to governance)
            worker_pool: WorkerPool instance for executor tools
        """
        self._api = api_fn
        self._pool = worker_pool

    def dispatch(self, name: str, args: dict) -> Any:
        # --- Task tools ---
        if name == "task_create":
            pid = args["project_id"]
            body = {"prompt": args["prompt"], "type": args["type"]}
            if args.get("priority"):
                body["priority"] = args["priority"]
            if args.get("metadata"):
                body["metadata"] = args["metadata"]
            return self._api("POST", f"/api/task/{pid}/create", body)

        if name == "task_list":
            pid = args["project_id"]
            qs = f"?status={args['status']}" if args.get("status") else ""
            return self._api("GET", f"/api/task/{pid}/list{qs}")

        if name == "task_claim":
            pid = args["project_id"]
            wid = args.get("worker_id", "observer")
            return self._api("POST", f"/api/task/{pid}/claim", {"worker_id": wid})

        if name == "task_complete":
            pid = args["project_id"]
            body = {"task_id": args["task_id"], "status": args["status"]}
            if args.get("result"):
                body["result"] = args["result"]
            return self._api("POST", f"/api/task/{pid}/complete", body)

        # --- Workflow tools ---
        if name == "wf_summary":
            return self._api("GET", f"/api/wf/{args['project_id']}/summary")

        if name == "wf_impact":
            return self._api("GET", f"/api/wf/{args['project_id']}/impact?files={args['files']}")

        if name == "node_update":
            pid = args["project_id"]
            body = {"nodes": args["nodes"], "status": args["status"]}
            if args.get("evidence"):
                body["evidence"] = args["evidence"]
            return self._api("POST", f"/api/wf/{pid}/verify-update", body)

        # --- Executor tools (in-process) ---
        if name == "executor_status":
            return self._pool.status()

        if name == "executor_scale":
            return self._pool.scale(args["workers"])

        # --- System ---
        if name == "health":
            return self._api("GET", "/api/health")

        raise ValueError(f"Unknown tool: {name!r}")
