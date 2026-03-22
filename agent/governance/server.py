"""HTTP server for the governance service.

Uses stdlib http.server (Starlette upgrade deferred to when dependencies are added).
Provides routing, middleware (auth, idempotency, request_id, audit), and JSON handling.
"""

import json
import sys
import uuid
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .errors import GovernanceError
from .db import get_connection, DBContext
from . import role_service
from . import state_service
from . import project_service
from . import memory_service
from . import audit_service
from .idempotency import check_idempotency, store_idempotency
from .redis_client import get_redis
from .models import Evidence, MemoryEntry, NodeDef
from .enums import VerifyStatus
from .impact_analyzer import ImpactAnalyzer
from .models import ImpactAnalysisRequest, FileHitPolicy

import os
PORT = int(os.environ.get("GOVERNANCE_PORT", "40006"))

# --- Route Registry ---
ROUTES = []


def route(method: str, path: str):
    def decorator(fn):
        ROUTES.append((method, path, fn))
        return fn
    return decorator


class GovernanceHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing and middleware."""

    def _find_handler(self, method: str):
        path = urlparse(self.path).path.rstrip("/")
        for m, prefix, handler in ROUTES:
            if m != method:
                continue
            # Exact match or parameterized match
            if path == prefix:
                return handler, {}, ""
            # Simple path parameter matching: /api/wf/{project_id}/...
            parts_route = prefix.split("/")
            parts_path = path.split("/")
            if len(parts_route) != len(parts_path):
                continue
            params = {}
            match = True
            for rp, pp in zip(parts_route, parts_path):
                if rp.startswith("{") and rp.endswith("}"):
                    params[rp[1:-1]] = pp
                elif rp != pp:
                    match = False
                    break
            if match:
                return handler, params, ""
        return None, {}, ""

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _query_params(self) -> dict:
        parsed = urlparse(self.path)
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}

    def _respond(self, code: int, body: dict):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method: str):
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        handler, path_params, _ = self._find_handler(method)
        if not handler:
            self._respond(404, {"error": "not_found", "message": "Endpoint not found"})
            return
        try:
            ctx = RequestContext(
                handler=self,
                method=method,
                path_params=path_params,
                query=self._query_params(),
                body=self._read_body() if method == "POST" else {},
                request_id=request_id,
                token=self.headers.get("X-Gov-Token", ""),
                idem_key=self.headers.get("Idempotency-Key", ""),
            )
            result = handler(ctx)
            if isinstance(result, tuple):
                code, body = result
            else:
                code, body = 200, result
            body["request_id"] = request_id
            self._respond(code, body)
        except GovernanceError as e:
            body = e.to_dict()
            body["request_id"] = request_id
            self._respond(e.status, body)
        except Exception as e:
            traceback.print_exc()
            self._respond(500, {
                "error": "internal_error",
                "message": str(e),
                "request_id": request_id,
            })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, format, *args):
        pass  # Suppress default logging


class RequestContext:
    """Encapsulates a single request's state."""
    def __init__(self, handler, method, path_params, query, body, request_id, token, idem_key):
        self.handler = handler
        self.method = method
        self.path_params = path_params
        self.query = query
        self.body = body
        self.request_id = request_id
        self.token = token
        self.idem_key = idem_key
        self._session = None
        self._conn = None

    def get_project_id(self) -> str:
        return self.path_params.get("project_id", self.body.get("project_id", ""))

    def require_auth(self, conn) -> dict:
        """Authenticate and return session. Caches result."""
        if self._session is None:
            self._session = role_service.authenticate(conn, self.token)
        return self._session


# ============================================================
# ROUTES
# ============================================================

# --- Init (one-time project initialization) ---

@route("POST", "/api/init")
def handle_init(ctx: RequestContext):
    """Human calls this once to create project + get coordinator token.
    Repeat call without password → 403.
    Repeat call with correct password → reset coordinator token.
    """
    result = project_service.init_project(
        project_id=ctx.body.get("project_id", ctx.body.get("project", "")),
        password=ctx.body.get("password", ""),
        project_name=ctx.body.get("project_name", ctx.body.get("name", "")),
        workspace_path=ctx.body.get("workspace_path", ""),
    )
    return 201, result


# --- Project ---


@route("GET", "/api/project/list")
def handle_project_list(ctx: RequestContext):
    return {"projects": project_service.list_projects()}


# --- Role (coordinator assigns roles to other agents) ---

@route("POST", "/api/role/assign")
def handle_role_assign(ctx: RequestContext):
    """Coordinator assigns a role+token to another agent."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.assign_role(
            conn, project_id, session,
            principal_id=ctx.body.get("principal_id", ""),
            role=ctx.body.get("role", ""),
            scope=ctx.body.get("scope"),
        )
    return 201, result


@route("POST", "/api/role/revoke")
def handle_role_revoke(ctx: RequestContext):
    """Coordinator revokes an agent's session."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.revoke_role(
            conn, project_id, session,
            session_id=ctx.body.get("session_id", ""),
        )
    return result


@route("POST", "/api/role/heartbeat")
def handle_heartbeat(ctx: RequestContext):
    # Need to find which project this session belongs to
    # First authenticate to get session
    # We check all projects (or the session tells us)
    # For simplicity, authenticate against a known project
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        # Try to find from token
        rc = get_redis()
        from .role_service import _hash_token
        token_hash = _hash_token(ctx.token)
        session_id = rc.get_session_by_token(token_hash)
        if session_id:
            cached = rc.get_cached_session(session_id)
            if cached:
                project_id = cached.get("project_id", "")

    if not project_id:
        from .errors import AuthError
        raise AuthError("Cannot determine project. Provide project_id or use a valid token.")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = role_service.heartbeat(
            conn, session["session_id"],
            ctx.body.get("status", "idle"),
        )
    return result


@route("GET", "/api/role/verify")
def handle_role_verify(ctx: RequestContext):
    """Verify a token and return session info. Used by Gateway for auth."""
    if not ctx.token:
        from .errors import AuthError
        raise AuthError("Missing token")

    # Try to find session from token across all projects
    rc = get_redis()
    from .role_service import _hash_token
    th = _hash_token(ctx.token)
    session_id = rc.get_session_by_token(th) if rc else None
    project_id = ""

    if session_id:
        cached = rc.get_cached_session(session_id)
        if cached:
            project_id = cached.get("project_id", "")

    if not project_id:
        # Fallback: scan projects
        for p in project_service.list_projects():
            try:
                with DBContext(p["project_id"]) as conn:
                    session = role_service.authenticate(conn, ctx.token)
                    return {
                        "valid": True,
                        "session_id": session["session_id"],
                        "principal_id": session.get("principal_id", ""),
                        "role": session.get("role", ""),
                        "project_id": p["project_id"],
                    }
            except Exception:
                continue
        from .errors import AuthError
        raise AuthError("Invalid token")

    with DBContext(project_id) as conn:
        session = role_service.authenticate(conn, ctx.token)
        return {
            "valid": True,
            "session_id": session["session_id"],
            "principal_id": session.get("principal_id", ""),
            "role": session.get("role", ""),
            "project_id": project_id,
        }


@route("GET", "/api/role/{project_id}/sessions")
def handle_list_sessions(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sessions = role_service.list_sessions(conn, project_id)
    return {"sessions": sessions}


# --- Token (dual-token model) ---

@route("POST", "/api/token/refresh")
def handle_token_refresh(ctx: RequestContext):
    """Exchange refresh_token for short-lived access_token."""
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    # Find project from token
    from . import token_service
    from .role_service import _hash_token
    th = _hash_token(refresh_token)
    rc = get_redis()
    session_id = rc.get_session_by_token(th) if rc else None
    project_id = ""

    if session_id:
        cached = rc.get_cached_session(session_id)
        if cached:
            project_id = cached.get("project_id", "")

    if not project_id:
        for p in project_service.list_projects():
            try:
                with DBContext(p["project_id"]) as conn:
                    result = token_service.issue_access_token(conn, refresh_token)
                    return result
            except Exception:
                continue
        from .errors import AuthError
        raise AuthError("Invalid refresh token")

    with DBContext(project_id) as conn:
        return token_service.issue_access_token(conn, refresh_token)


@route("POST", "/api/token/revoke")
def handle_token_revoke(ctx: RequestContext):
    """Revoke a refresh token."""
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                return token_service.revoke_refresh_token(conn, refresh_token)
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


@route("POST", "/api/token/rotate")
def handle_token_rotate(ctx: RequestContext):
    """Rotate: issue new refresh token, invalidate old."""
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                return token_service.rotate_refresh_token(conn, refresh_token)
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


# --- Agent Lifecycle ---

@route("POST", "/api/agent/register")
def handle_agent_register(ctx: RequestContext):
    """Register an agent and get a lease."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    from . import agent_lifecycle
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        return agent_lifecycle.register_agent(
            conn, project_id, session,
            expected_duration_sec=int(ctx.body.get("expected_duration_sec", 0)),
        )


@route("POST", "/api/agent/heartbeat")
def handle_agent_heartbeat(ctx: RequestContext):
    """Renew agent lease."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.heartbeat(
        lease_id, status=ctx.body.get("status", "idle"),
    )


@route("POST", "/api/agent/deregister")
def handle_agent_deregister(ctx: RequestContext):
    """Deregister an agent."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.deregister(lease_id)


@route("GET", "/api/agent/orphans")
def handle_agent_orphans(ctx: RequestContext):
    """List orphaned agents (expired leases)."""
    project_id = ctx.query.get("project_id", "")
    from . import agent_lifecycle
    orphans = agent_lifecycle.find_orphans(project_id or None)
    return {"orphans": orphans, "count": len(orphans)}


@route("POST", "/api/agent/cleanup")
def handle_agent_cleanup(ctx: RequestContext):
    """Clean up orphaned agents. Coordinator only."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "agent.cleanup",
                                        {"detail": "Only coordinator can cleanup orphans"})

    from . import agent_lifecycle
    return agent_lifecycle.cleanup_orphans(project_id)


# --- Session Context ---

@route("POST", "/api/context/{project_id}/save")
def handle_context_save(ctx: RequestContext):
    """Save session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.save_snapshot(
        project_id, ctx.body.get("context", ctx.body),
        expected_version=ctx.body.get("expected_version"),
    )


@route("GET", "/api/context/{project_id}/load")
def handle_context_load(ctx: RequestContext):
    """Load session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    data = session_context.load_snapshot(project_id)
    if data is None:
        return {"context": None, "exists": False}
    return {"context": data, "exists": True}


@route("POST", "/api/context/{project_id}/log")
def handle_context_log_append(ctx: RequestContext):
    """Append entry to session log."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.append_log(
        project_id,
        entry_type=ctx.body.get("type", "action"),
        content=ctx.body.get("content", {}),
    )


@route("GET", "/api/context/{project_id}/log")
def handle_context_log_read(ctx: RequestContext):
    """Read session log entries."""
    project_id = ctx.get_project_id()
    from . import session_context
    entries = session_context.read_log(project_id, limit=int(ctx.query.get("limit", "50")))
    return {"entries": entries, "count": len(entries)}


@route("POST", "/api/context/{project_id}/assemble")
def handle_context_assemble(ctx: RequestContext):
    """Assemble context from dbservice for a task type."""
    project_id = ctx.get_project_id()
    task_type = ctx.body.get("task_type", "dev_general")
    token_budget = int(ctx.body.get("token_budget", 5000))

    import requests as http_requests
    dbservice_url = os.environ.get("DBSERVICE_URL", "")
    if not dbservice_url:
        return {"context": [], "degraded": True, "reason": "DBSERVICE_URL not set"}

    try:
        resp = http_requests.post(
            f"{dbservice_url}/assemble-context",
            json={"taskType": task_type, "scope": project_id, "tokenBudget": token_budget},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"context": [], "degraded": True, "reason": f"dbservice returned {resp.status_code}"}
    except Exception as e:
        return {"context": [], "degraded": True, "reason": str(e)}


@route("POST", "/api/context/{project_id}/archive")
def handle_context_archive(ctx: RequestContext):
    """Archive context to long-term memory and clear."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.archive_context(project_id)


# --- Workflow ---

@route("POST", "/api/wf/{project_id}/import-graph")
def handle_import_graph(ctx: RequestContext):
    """Import acceptance graph from a markdown file. Coordinator only."""
    project_id = ctx.get_project_id()
    md_path = ctx.body.get("md_path", ctx.body.get("graph_source", ""))
    if not md_path:
        from .errors import ValidationError
        raise ValidationError("md_path is required")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "import-graph",
                                        {"detail": "Only coordinator can import graphs"})
    result = project_service.import_graph(project_id, md_path)
    return result


@route("POST", "/api/wf/{project_id}/verify-update")
def handle_verify_update(ctx: RequestContext):
    project_id = ctx.get_project_id()

    # Input validation with helpful messages
    nodes = ctx.body.get("nodes", [])
    status = ctx.body.get("status", "")
    evidence = ctx.body.get("evidence")

    if not nodes:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "nodes" field. Example: {"nodes": ["L1.3"], "status": "testing", '
            '"evidence": {"type": "test_report", "producer": "tester-001"}}'
        )
    if not isinstance(nodes, list):
        from .errors import ValidationError
        raise ValidationError(f'"nodes" must be a list, got {type(nodes).__name__}')
    if not status:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "status" field. Valid values: pending, testing, t2_pass, qa_pass, failed, waived, skipped'
        )
    if evidence is not None and not isinstance(evidence, dict):
        from .errors import ValidationError
        raise ValidationError(
            f'"evidence" must be a dict, got {type(evidence).__name__}. '
            'Example: {"type": "test_report", "producer": "tester-001", "tool": "pytest", '
            '"summary": {"passed": 42, "failed": 0}}'
        )

    with DBContext(project_id) as conn:
        # Idempotency check
        rc = get_redis()
        if ctx.idem_key:
            cached = rc.check_idempotency(ctx.idem_key)
            if cached:
                return cached

        session = ctx.require_auth(conn)
        graph = project_service.load_project_graph(project_id)

        result = state_service.verify_update(
            conn, project_id, graph,
            node_ids=nodes,
            target_status=status,
            session=session,
            evidence_dict=evidence,
        )

        # Store idempotency
        if ctx.idem_key:
            rc.store_idempotency(ctx.idem_key, result)

    return result


@route("POST", "/api/wf/{project_id}/baseline")
def handle_baseline(ctx: RequestContext):
    """Coordinator batch-sets historical node states, bypassing checks."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.set_baseline(
            conn, project_id,
            node_statuses=ctx.body.get("nodes", {}),
            session=session,
            reason=ctx.body.get("reason", ""),
        )
    return result


@route("POST", "/api/wf/{project_id}/release-gate")
def handle_release_gate(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        graph = project_service.load_project_graph(project_id)
        result = state_service.release_gate(
            conn, project_id, graph,
            scope=ctx.body.get("scope"),
            profile=ctx.body.get("profile"),
        )
    return result


@route("GET", "/api/wf/{project_id}/summary")
def handle_summary(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        return state_service.get_summary(conn, project_id)


@route("GET", "/api/wf/{project_id}/node/{node_id}")
def handle_get_node(ctx: RequestContext):
    project_id = ctx.get_project_id()
    node_id = ctx.path_params.get("node_id", "")
    with DBContext(project_id) as conn:
        state = state_service.get_node_status(conn, project_id, node_id)
        if state is None:
            from .errors import NodeNotFoundError
            raise NodeNotFoundError(node_id)
    graph = project_service.load_project_graph(project_id)
    node_def = graph.get_node(node_id)
    return {**state, "definition": node_def}


@route("GET", "/api/wf/{project_id}/impact")
def handle_impact(ctx: RequestContext):
    project_id = ctx.get_project_id()
    files_str = ctx.query.get("files", "")
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    include_secondary = ctx.query.get("file_policy", "") == "primary+secondary"

    graph = project_service.load_project_graph(project_id)

    with DBContext(project_id) as conn:
        def get_status(nid):
            row = conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, nid),
            ).fetchone()
            return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING

        analyzer = ImpactAnalyzer(graph, get_status)
        request = ImpactAnalysisRequest(
            changed_files=files,
            file_policy=FileHitPolicy(match_primary=True, match_secondary=include_secondary),
        )
        return analyzer.analyze(request)


@route("GET", "/api/wf/{project_id}/export")
def handle_export(ctx: RequestContext):
    project_id = ctx.get_project_id()
    fmt = ctx.query.get("format", "json")
    graph = project_service.load_project_graph(project_id)

    if fmt == "mermaid":
        with DBContext(project_id) as conn:
            rows = conn.execute(
                "SELECT node_id, verify_status FROM node_state WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            statuses = {r["node_id"]: r["verify_status"] for r in rows}
        return {"mermaid": graph.export_mermaid(statuses), "node_count": graph.node_count()}
    elif fmt == "json":
        return {"nodes": {nid: graph.get_node(nid) for nid in graph.list_nodes()}}
    else:
        from .errors import ValidationError
        raise ValidationError(f"Unknown export format: {fmt}")


@route("POST", "/api/wf/{project_id}/rollback")
def handle_rollback(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.rollback(
            conn, project_id,
            target_version=ctx.body.get("target_version", 0),
            session=session,
        )
    return result


# --- Memory ---

@route("POST", "/api/mem/{project_id}/write")
def handle_mem_write(ctx: RequestContext):
    project_id = ctx.get_project_id()
    entry = MemoryEntry.from_dict(ctx.body)
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.write_memory(conn, project_id, entry, session)
    return 201, result


@route("GET", "/api/mem/{project_id}/query")
def handle_mem_query(ctx: RequestContext):
    project_id = ctx.get_project_id()
    module = ctx.query.get("module")
    kind = ctx.query.get("kind")
    node = ctx.query.get("node")

    if node:
        entries = memory_service.query_by_related_node(project_id, node)
    elif kind:
        entries = memory_service.query_by_kind(project_id, kind, module)
    elif module:
        entries = memory_service.query_by_module(project_id, module)
    else:
        entries = memory_service.query_all(project_id)
    return {"entries": entries, "count": len(entries)}


# --- Audit ---

@route("GET", "/api/audit/{project_id}/log")
def handle_audit_log(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_log(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            event_filter=ctx.query.get("event"),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


@route("GET", "/api/audit/{project_id}/violations")
def handle_audit_violations(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_violations(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


# --- Task Registry ---

@route("POST", "/api/task/{project_id}/create")
def handle_task_create(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        return task_registry.create_task(
            conn, project_id,
            prompt=ctx.body.get("prompt", ""),
            task_type=ctx.body.get("type", "task"),
            related_nodes=ctx.body.get("related_nodes"),
            created_by=session.get("principal_id", ""),
            priority=int(ctx.body.get("priority", 0)),
            max_attempts=int(ctx.body.get("max_attempts", 3)),
            metadata=ctx.body.get("metadata"),
        )


@route("POST", "/api/task/{project_id}/claim")
def handle_task_claim(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        task = task_registry.claim_task(conn, project_id, session.get("principal_id", ""))
        if task is None:
            return {"task": None, "message": "No tasks available"}
        return {"task": task}


@route("POST", "/api/task/{project_id}/complete")
def handle_task_complete(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        ctx.require_auth(conn)
        return task_registry.complete_task(
            conn, ctx.body.get("task_id", ""),
            status=ctx.body.get("status", "succeeded"),
            result=ctx.body.get("result"),
            error_message=ctx.body.get("error_message", ""),
        )


@route("GET", "/api/task/{project_id}/list")
def handle_task_list(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        tasks = task_registry.list_tasks(
            conn, project_id,
            status=ctx.query.get("status"),
            limit=int(ctx.query.get("limit", "50")),
        )
    return {"tasks": tasks, "count": len(tasks)}


# --- Health ---

@route("GET", "/api/health")
def handle_health(ctx: RequestContext):
    return {"status": "ok", "service": "governance", "port": PORT}


@route("GET", "/api/metrics")
def handle_metrics(ctx: RequestContext):
    """Return in-memory metrics snapshot."""
    from .observability import get_metrics
    return get_metrics()


@route("GET", "/api/health/deep")
def handle_deep_health(ctx: RequestContext):
    """Deep health check: Redis, SQLite, outbox, queues."""
    from .observability import check_outbox_health
    checks = {"governance": "ok", "port": PORT}

    # Redis
    rc = get_redis()
    checks["redis"] = "ok" if rc.available else "degraded"

    # Outbox alerts
    alerts = []
    for p in project_service.list_projects():
        alerts.extend(check_outbox_health(p["project_id"]))
    checks["alerts"] = alerts
    checks["alert_count"] = len(alerts)

    return checks


# --- Documentation ---

_DOCS = {
    "overview": {
        "title": "Governance Service Overview",
        "description": "Workflow governance service for multi-agent coordination. Manages project initialization, role assignment, node verification, release gating, memory, and audit.",
        "base_url": "http://localhost:40000",
        "api_prefix": "/api",
        "gateway_prefix": "/gateway",
        "auth": "All API calls (except /api/init, /api/health, /api/project/list, /api/docs) require X-Gov-Token header.",
    },
    "quickstart": {
        "title": "Coordinator Quickstart",
        "steps": [
            {
                "step": 1,
                "action": "Init project (human does this once)",
                "method": "POST /api/init",
                "body": {"project_id": "myProject", "password": "secret"},
                "returns": "coordinator_token (save this!)",
            },
            {
                "step": 2,
                "action": "Import acceptance graph",
                "method": "POST /api/wf/{project_id}/import-graph",
                "headers": {"X-Gov-Token": "gov-xxx"},
                "body": {"markdown": "# Acceptance Graph\\n## L0 Foundation\\n- L0.1 ..."},
            },
            {
                "step": 3,
                "action": "Bind to Telegram (for message relay)",
                "method": "POST /gateway/bind",
                "body": {"token": "gov-xxx", "chat_id": 123456, "project_id": "myProject"},
            },
            {
                "step": 4,
                "action": "Assign roles to other agents",
                "method": "POST /api/role/assign",
                "headers": {"X-Gov-Token": "gov-xxx"},
                "body": {"project_id": "myProject", "principal_id": "tester-001", "role": "tester"},
                "returns": "agent_token for the tester",
            },
            {
                "step": 5,
                "action": "Start working - verify nodes, check gates, release",
                "see": "workflow_rules",
            },
        ],
    },
    "endpoints": {
        "title": "API Endpoints",
        "groups": {
            "init": {
                "POST /api/init": "Create project + get coordinator token. Repeat with password to reset token.",
            },
            "project": {
                "GET /api/project/list": "List all projects with node counts.",
            },
            "role": {
                "POST /api/role/assign": "Coordinator assigns role+token to agent. Body: {project_id, principal_id, role}",
                "POST /api/role/revoke": "Revoke agent session. Body: {project_id, session_id}",
                "POST /api/role/heartbeat": "Agent keepalive. Body: {project_id?, status?}",
                "GET /api/role/verify": "Verify token, returns session info. Used by Gateway.",
                "GET /api/role/{project_id}/sessions": "List active sessions for a project.",
            },
            "workflow": {
                "POST /api/wf/{project_id}/import-graph": "Import acceptance graph from markdown.",
                "POST /api/wf/{project_id}/verify-update": "Update node verification status. Body: {nodes, status, evidence}",
                "POST /api/wf/{project_id}/baseline": "Batch set historical state (coordinator only). Body: {nodes: {id: status}, reason}",
                "POST /api/wf/{project_id}/release-gate": "Check if all nodes pass for release.",
                "POST /api/wf/{project_id}/rollback": "Rollback node state to a version.",
                "GET /api/wf/{project_id}/summary": "Status summary (counts by status).",
                "GET /api/wf/{project_id}/node/{node_id}": "Single node details.",
                "GET /api/wf/{project_id}/export": "Export graph as JSON or Mermaid. Query: format=json|mermaid",
                "GET /api/wf/{project_id}/impact": "File change impact analysis. Query: files=a.py,b.py",
            },
            "memory": {
                "POST /api/mem/{project_id}/write": "Write memory entry. Body: {module, kind, content, related_nodes?, supersedes?}",
                "GET /api/mem/{project_id}/query": "Query memory. Query: module=, kind=, node=",
            },
            "audit": {
                "GET /api/audit/{project_id}/log": "Query audit log. Query: limit=, event=, since=",
                "GET /api/audit/{project_id}/violations": "Query violations. Query: limit=, since=",
            },
        },
    },
    "workflow_rules": {
        "title": "Workflow Verification Rules",
        "status_flow": {
            "states": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
            "transitions": {
                "pending": ["testing"],
                "testing": ["t2_pass", "failed"],
                "t2_pass": ["qa_pass", "failed"],
                "qa_pass": "(terminal - verified)",
                "failed": ["testing"],
            },
        },
        "role_permissions": {
            "coordinator": "Can do everything: baseline, assign roles, rollback, import graph, verify-update.",
            "tester": "Can transition: pending->testing, testing->t2_pass/failed.",
            "qa": "Can transition: t2_pass->qa_pass/failed.",
            "dev": "Can transition: pending->testing, testing->t2_pass/failed (same as tester).",
            "observer": "Read-only. Can query status, summary, export.",
        },
        "evidence_format": {
            "description": "Evidence must be a dict, not a string.",
            "required_fields": ["type", "producer"],
            "optional_fields": ["tool", "summary", "artifact_uri", "checksum", "created_at"],
            "example": {
                "type": "test_report",
                "producer": "tester-001",
                "tool": "pytest",
                "summary": {"passed": 42, "failed": 0},
            },
        },
        "verify_update_example": {
            "method": "POST /api/wf/{project_id}/verify-update",
            "headers": {"X-Gov-Token": "agent-token"},
            "body": {
                "nodes": ["L1.3"],
                "status": "t2_pass",
                "evidence": {
                    "type": "test_report",
                    "producer": "tester-001",
                    "tool": "pytest",
                    "summary": {"passed": 10, "failed": 0},
                },
            },
        },
        "gate_rules": "Nodes with dependencies (gates) cannot advance until upstream nodes satisfy their gate policy. Use GET /api/wf/{project_id}/node/{node_id} to check gate status.",
        "release_gate": "POST /api/wf/{project_id}/release-gate checks if all nodes in scope are qa_pass. Returns {release: true/false, blocking_nodes: [...]}.",
    },
    "memory_guide": {
        "title": "Memory Service Guide",
        "description": "Store and query development knowledge (patterns, pitfalls, decisions, workarounds) per project.",
        "kinds": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern"],
        "write_example": {
            "method": "POST /api/mem/{project_id}/write",
            "headers": {"X-Gov-Token": "token"},
            "body": {
                "module": "auth",
                "kind": "pitfall",
                "content": "Never store session tokens in localStorage - use httpOnly cookies.",
                "related_nodes": ["L2.3"],
                "applies_when": "Implementing any auth-related feature",
            },
        },
        "query_examples": [
            "GET /api/mem/{project_id}/query?module=auth",
            "GET /api/mem/{project_id}/query?kind=pitfall",
            "GET /api/mem/{project_id}/query?node=L2.3",
        ],
    },
    "telegram_integration": {
        "title": "Telegram Gateway Integration",
        "description": "The Gateway relays messages between Telegram users and Coordinators via Redis Pub/Sub.",
        "architecture": "Telegram <-> Gateway (Docker) <-> Redis <-> Coordinator (host)",
        "coordinator_setup": {
            "step_1": "Import ChatProxy: from telegram_gateway.chat_proxy import ChatProxy",
            "step_2": "Create proxy: proxy = ChatProxy(token='gov-xxx', gateway_url='http://localhost:40000', redis_url='redis://localhost:40079/0')",
            "step_3": "Bind to chat: proxy.bind(chat_id=YOUR_CHAT_ID, project_id='myProject')",
            "step_4_blocking": "proxy.listen(on_message=lambda msg: proxy.reply(process(msg['text'])))",
            "step_4_background": "proxy.start(on_message=handler)  # non-blocking",
        },
        "gateway_api": {
            "POST /gateway/bind": "Bind coordinator token to chat_id. Body: {token, chat_id, project_id}",
            "POST /gateway/reply": "Send message to Telegram. Body: {token, chat_id?, text}. If no chat_id, uses bound chat.",
            "POST /gateway/unbind": "Unbind chat_id. Body: {chat_id}",
            "GET /gateway/health": "Gateway health check.",
            "GET /gateway/status": "List all active routes (bound coordinators).",
        },
        "message_flow": {
            "user_to_coordinator": "User sends text in Telegram -> Gateway publishes to Redis chat:inbox:{token_hash} -> Coordinator receives via ChatProxy",
            "coordinator_to_user": "Coordinator calls POST /gateway/reply -> Gateway calls Telegram sendMessage",
            "governance_events": "Governance publishes events to Redis gov:events:{project_id} -> Gateway formats and sends to admin chat",
        },
        "telegram_commands": {
            "/menu": "Interactive menu showing registered coordinators with switch buttons",
            "/bind <token>": "Bind coordinator to current chat",
            "/unbind": "Unbind current coordinator",
            "/status [project]": "Show project verification status",
            "/projects": "List all projects",
            "/health": "Service health check",
        },
    },
}


@route("GET", "/api/docs")
def handle_docs_index(ctx: RequestContext):
    """Return available documentation sections."""
    sections = []
    for key, doc in _DOCS.items():
        sections.append({
            "section": key,
            "title": doc.get("title", key),
            "url": f"/api/docs/{key}",
        })
    return {"sections": sections}


@route("GET", "/api/docs/{section}")
def handle_docs_section(ctx: RequestContext):
    """Return a specific documentation section."""
    section = ctx.path_params.get("section", "")
    if section not in _DOCS:
        from .errors import GovernanceError
        raise GovernanceError(f"Unknown doc section: {section}. Available: {list(_DOCS.keys())}", 404)
    return _DOCS[section]


# ============================================================
# Server Entry Point
# ============================================================

def create_server(port: int = None) -> HTTPServer:
    p = port or PORT
    server = HTTPServer(("0.0.0.0", p), GovernanceHandler)
    return server


def main():
    # Enable Redis Pub/Sub bridge for EventBus
    from .event_bus import get_event_bus
    redis = get_redis()
    if redis.available:
        get_event_bus().enable_redis_bridge()
        print("EventBus: Redis Pub/Sub bridge enabled")
    else:
        print("EventBus: Redis unavailable, in-process only")

    # Start outbox worker for reliable event delivery
    try:
        from .outbox import OutboxWorker
        outbox_worker = OutboxWorker()
        outbox_worker.start()
        print("OutboxWorker: started")
    except Exception as e:
        print(f"OutboxWorker: failed to start ({e})")

    server = create_server()
    print(f"Governance service listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
