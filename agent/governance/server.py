"""HTTP server for the governance service.

Uses stdlib http.server (Starlette upgrade deferred to when dependencies are added).
Provides routing, middleware (auth, idempotency, request_id, audit), and JSON handling.
"""

import json
import sys
import uuid
import traceback
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .errors import GovernanceError
import sqlite3
import time
from .db import get_connection, DBContext, independent_connection
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
import signal
import subprocess
PORT = int(os.environ.get("GOVERNANCE_PORT", "40006"))

# --- Server Version (git commit hash at startup) ---
def _get_git_version():
    """Read current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

SERVER_VERSION = _get_git_version()
SERVER_PID = os.getpid()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# SQLite BUSY retry helper
# ---------------------------------------------------------------------------
_BUSY_RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds between attempts 1→2, 2→3


def _retry_on_busy(fn, *args, **kwargs):
    """Call *fn* up to 3 times, retrying on SQLITE_BUSY / 'database is locked'.

    Uses an exponential-style back-off: 0.5 s → 1 s → 2 s between attempts.
    Intended for short write transactions (version-update, version-sync).

    Args:
        fn: Callable that performs the SQLite operation.  It must be
            idempotent or use INSERT OR REPLACE semantics so retries are safe.
        *args / **kwargs: Forwarded verbatim to *fn*.

    Returns:
        The return value of *fn* on success.

    Raises:
        sqlite3.OperationalError: Re-raised after all 3 attempts are exhausted.
    """
    last_exc = None
    for attempt, delay in enumerate(_BUSY_RETRY_DELAYS, start=1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                last_exc = exc
                time.sleep(delay)
            else:
                raise
    # Final attempt (no sleep after this one)
    try:
        return fn(*args, **kwargs)
    except sqlite3.OperationalError:
        raise last_exc


def _acquire_pid_lock():
    """Write PID lockfile. Kill old process if still alive."""
    lock_dir = os.path.join(
        os.environ.get("SHARED_VOLUME_PATH",
                        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
        "codex-tasks", "state")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, "governance.pid")

    # Check old PID
    if os.path.exists(lock_path):
        try:
            old_pid = int(open(lock_path).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                import logging
                logging.getLogger(__name__).info("Killed old governance process PID %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # Old process already dead

    # Write new PID
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))

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

    def _respond(self, code: int, body: dict, extra_headers: dict | None = None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
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
            if isinstance(result, tuple) and len(result) == 3:
                code, body, extra_headers = result
            elif isinstance(result, tuple):
                code, body = result
                extra_headers = None
            else:
                code, body = 200, result
                extra_headers = None
            body["request_id"] = request_id
            self._respond(code, body, extra_headers)
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
        raw = self.path_params.get("project_id", self.body.get("project_id", ""))
        return project_service._normalize_project_id(raw) if raw else raw

    def require_auth(self, conn) -> dict:
        """Authenticate and return session. Caches result.

        Token-free mode: when no token is provided, returns a default
        coordinator session so all APIs work without authentication.
        Tokens still work if provided (for backward compatibility).
        """
        if self._session is None:
            if not self.token:
                # Anonymous access — full coordinator permissions
                project_id = self.get_project_id()
                self._session = {
                    "session_id": "anonymous",
                    "principal_id": "anonymous",
                    "project_id": project_id,
                    "role": "coordinator",
                    "scope": [],
                    "token": "",
                    "permissions": ["*"],
                }
            else:
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


@route("POST", "/api/projects/register")
def handle_project_register(ctx: RequestContext):
    """Register a project workspace with config validation.

    Body: {"workspace_path": "/path/to/project"}
    Returns: {"project_id", "config_hash", "registered": true}
    """
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    workspace_path = ctx.body.get("workspace_path", "").strip()
    if not workspace_path:
        return 400, {"error": "workspace_path is required"}

    from pathlib import Path
    ws = Path(workspace_path)

    # In Docker, host paths are not accessible — skip path validation
    # but still validate config if accessible
    try:
        from project_config import load_project_config, validate_commands
        config = load_project_config(ws)
    except (ValueError, FileNotFoundError) as e:
        # Path not accessible (Docker) or no config — try /workspace mount
        workspace_mount = Path("/workspace")
        if workspace_mount.exists():
            try:
                config = load_project_config(workspace_mount)
            except (ValueError, FileNotFoundError) as e2:
                return 400, {"error": f"config not found: {e2}"}
        else:
            return 400, {"error": f"config not found: {e}"}

    # Command safety
    cmd_violations = validate_commands(config)
    if cmd_violations:
        return 400, {"error": "unsafe commands", "violations": cmd_violations}

    # Check uniqueness
    existing = project_service.get_project(config.project_id)
    if existing and existing.get("workspace_path") and existing["workspace_path"] != str(ws):
        return 409, {"error": f"project_id '{config.project_id}' already registered to different workspace"}

    # Register in governance
    project_id = config.project_id
    try:
        if not existing:
            project_service.init_project(
                project_id=project_id,
                password="auto-registered",
                project_name=config.project_id,
                workspace_path=str(ws),
            )
    except Exception as e:
        # May already exist with different password — that's OK
        if "already exists" not in str(e).lower():
            return 500, {"error": f"registration failed: {e}"}

    # workspace_registry removed — workspace info stored in governance projects.json

    return 201, {
        "project_id": project_id,
        "config_hash": str(hash(str(config))),
        "registered": True,
        "language": config.language,
        "test_command": config.testing.unit_command,
        "deploy_strategy": config.deploy.strategy,
    }


@route("GET", "/api/projects/{project_id}/config")
def handle_project_config(ctx: RequestContext):
    """Return resolved project config."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    try:
        from project_config import load_project_config
        from pathlib import Path
        # Try governance project workspace_path, then /workspace fallback
        proj_data = project_service.list_projects()
        ws_path = None
        for p in proj_data:
            if p.get("project_id") == project_id:
                ws_path = p.get("workspace_path", "")
                break
        if ws_path:
            config = load_project_config(Path(ws_path))
        elif Path('/workspace').exists():
            config = load_project_config(Path('/workspace'))
        else:
            return 404, {'error': f'no workspace registered for {project_id}'}
        return {
            "project_id": config.project_id,
            "language": config.language,
            "testing": {"unit_command": config.testing.unit_command, "e2e_command": config.testing.e2e_command},
            "build": {"command": config.build.command, "release_checks": config.build.release_checks},
            "deploy": {"strategy": config.deploy.strategy, "service_rules_count": len(config.deploy.service_rules)},
            "governance": {"enabled": config.governance.enabled, "test_tool_label": config.governance.test_tool_label},
        }
    except Exception as e:
        return 404, {"error": f"config not found: {e}"}


@route("POST", "/api/projects/{project_id}/explain")
def handle_project_explain(ctx: RequestContext):
    """Dry-run: explain what would happen for given changed files."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("changed_files", [])
    try:
        from project_config import explain_config, load_project_config
        from pathlib import Path
        # Resolve workspace from governance project data
        proj_data = project_service.list_projects()
        ws_entry = None
        for p in proj_data:
            if p.get("project_id") == project_id and p.get("workspace_path"):
                ws_entry = {"path": p["workspace_path"]}
                break
        if ws_entry:
            config = load_project_config(Path(ws_entry['path']))
            # Build explain manually since explain_config uses registry
            from deploy_chain import detect_affected_services
            affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
            return {
                "project_id": config.project_id,
                "test_command": config.testing.unit_command,
                "deploy_strategy": config.deploy.strategy,
                "affected_services": affected,
                "changed_files": changed_files,
            }
        else:
            ws = Path('/workspace')
            if ws.exists():
                config = load_project_config(ws)
                from deploy_chain import detect_affected_services
                affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
                return {
                    "project_id": config.project_id,
                    "test_command": config.testing.unit_command,
                    "deploy_strategy": config.deploy.strategy,
                    "affected_services": affected,
                    "changed_files": changed_files,
                }
            else:
                return 404, {'error': f'no workspace registered for {project_id}'}
        return explain_config(project_id, changed_files=changed_files)
    except Exception as e:
        return 404, {"error": f"explain failed: {e}"}


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


# --- Token ---

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
    """DEPRECATED (v5): Use revoke + re-init instead.
    Removal timeline: deprecated since v5, scheduled for removal in v8.
    """
    # Deprecation headers: deprecated since v5, removal planned for v8
    _deprecation_headers = {
        "X-Deprecated-Since": "v5",
        "X-Removal-Date": "v8",
    }
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                result = token_service.rotate_refresh_token(conn, refresh_token)
                return 200, result, _deprecation_headers
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


@route("POST", "/api/wf/{project_id}/node-create")
def handle_node_create(ctx: RequestContext):
    """Create a single node. System allocates display_id.

    AI provides: parent_layer (int) + title + deps + primary
    System provides: display_id (L{layer}.{next_index})

    Body: {
        "parent_layer": 22,          // required: which layer
        "title": "ContextStore",     // required
        "node": {                    // optional extras
            "deps": ["L15.1"],
            "primary": ["agent/context_store.py"],
            "description": "..."
        }
    }
    """
    project_id = ctx.get_project_id()
    parent_layer = ctx.body.get("parent_layer")
    title = ctx.body.get("title", "")
    node = ctx.body.get("node", {})

    if not parent_layer and not title:
        # Fallback: try to read from node.id (legacy)
        node_id = node.get("id", "")
        if node_id:
            parent_layer = int(node_id.split(".")[0][1:]) if "." in node_id else None
            title = node.get("title", node_id)

    if parent_layer is None:
        from .errors import ValidationError
        raise ValidationError("parent_layer is required (e.g., 22 for L22.x)")

    if not title:
        from .errors import ValidationError
        raise ValidationError("title is required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") not in ("coordinator", "pm"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-create",
                                        {"detail": "Only coordinator or PM can create nodes"})

        # System allocates display_id: find max index in this layer
        prefix = f"L{parent_layer}."
        existing = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id = ? AND node_id LIKE ?",
            (project_id, f"{prefix}%")
        ).fetchall()

        max_index = 0
        for row in existing:
            try:
                idx = int(row["node_id"].split(".")[1])
                max_index = max(max_index, idx)
            except (ValueError, IndexError):
                pass

        new_index = max_index + 1
        display_id = f"L{parent_layer}.{new_index}"

        # Insert node state
        now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        conn.execute(
            """INSERT OR IGNORE INTO node_state
               (project_id, node_id, verify_status, build_status, updated_at, version)
               VALUES (?, ?, 'pending', 'unknown', ?, 1)""",
            (project_id, display_id, now)
        )

        # Record in history (use role field which exists in all schema versions)
        try:
            conn.execute(
                """INSERT INTO node_history (project_id, node_id, from_status, to_status, role, evidence_json, created_at)
                   VALUES (?, ?, 'none', 'pending', ?, ?, ?)""",
                (project_id, display_id, session.get("role", "coordinator"),
                 json.dumps({"title": title, "deps": node.get("deps", []), "primary": node.get("primary", [])}),
                 now)
            )
        except Exception:
            pass  # History is nice-to-have, don't block node creation

        # P0-2 fix: also add node to in-memory graph + persist graph.json
        try:
            from .models import NodeDef
            from .db import _resolve_project_dir
            graph = project_service.load_project_graph(project_id)
            node_def = NodeDef(
                id=display_id,
                title=title,
                layer=f"L{parent_layer}",
                primary=node.get("primary", []),
            )
            deps = node.get("deps", [])
            # Filter deps to only existing graph nodes
            valid_deps = [d for d in deps if graph.has_node(d)]
            graph.add_node(node_def, deps=valid_deps)
            graph.save(_resolve_project_dir(project_id) / "graph.json")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("node-create graph update failed: %s", e)

    return {
        "node_id": display_id,
        "parent_layer": parent_layer,
        "title": title,
        "created": True,
    }


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
            min_status=ctx.body.get("min_status", "qa_pass"),
        )
    return result


@route("POST", "/api/wf/{project_id}/artifacts-check")
def handle_artifacts_check(ctx: RequestContext):
    """Check artifacts for nodes before qa_pass."""
    project_id = ctx.get_project_id()
    node_ids = ctx.body.get("nodes", [])
    if not node_ids:
        from .errors import ValidationError
        raise ValidationError('Missing "nodes" field.')

    graph = project_service.load_project_graph(project_id)
    from .artifacts import check_artifacts_for_qa_pass
    return check_artifacts_for_qa_pass(node_ids, graph, project_id)


@route("POST", "/api/wf/{project_id}/coverage-check")
def handle_coverage_check(ctx: RequestContext):
    """Check if changed files are covered by acceptance graph nodes. Records result for gatekeeper."""
    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("files", [])
    if not changed_files:
        from .errors import ValidationError
        raise ValidationError('Missing "files" field. Provide list of changed file paths.')

    graph = project_service.load_project_graph(project_id)
    from .coverage_check import check_feature_coverage
    result = check_feature_coverage(graph, changed_files)

    # Record result for gatekeeper
    try:
        from . import gatekeeper
        with DBContext(project_id) as conn:
            session = None
            try:
                session = ctx.require_auth(conn)
            except Exception:
                pass
            gatekeeper.record_check(
                conn, project_id, "coverage_check",
                passed=result.get("pass", False),
                result=result,
                created_by=session.get("principal_id", "") if session else "",
            )
    except Exception:
        pass  # Non-critical

    return result


@route("GET", "/api/wf/{project_id}/summary")
def handle_summary(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        return state_service.get_summary(conn, project_id)


@route("GET", "/api/wf/{project_id}/preflight-check")
def handle_preflight_check(ctx: RequestContext):
    project_id = ctx.get_project_id()
    auto_fix = ctx.query_params.get("auto_fix", ["false"])[0].lower() == "true"
    from .preflight import run_preflight
    with DBContext(project_id) as conn:
        return run_preflight(conn, project_id, auto_fix=auto_fix)


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


@route("POST", "/api/wf/{project_id}/node-update")
def handle_node_update(ctx: RequestContext):
    """Update node attributes (e.g. secondary doc bindings). Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        ctx.require_auth(conn)
    node_id = ctx.body.get("node_id")
    attrs = ctx.body.get("attrs", {})
    if not node_id or not attrs:
        from .errors import GovernanceError
        raise GovernanceError("missing node_id or attrs", "invalid_request")
    # Only allow safe attributes to be updated
    ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
    rejected = set(attrs.keys()) - ALLOWED_ATTRS
    if rejected:
        from .errors import GovernanceError
        raise GovernanceError(f"Cannot update attrs: {rejected}. Allowed: {ALLOWED_ATTRS}", "forbidden_attr")
    graph = project_service.load_project_graph(project_id)
    graph.update_node_attrs(node_id, attrs)
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"node_id": node_id, "updated_attrs": list(attrs.keys())}


@route("POST", "/api/wf/{project_id}/node-batch-update")
def handle_node_batch_update(ctx: RequestContext):
    """Batch update secondary doc bindings for multiple nodes. Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        ctx.require_auth(conn)
    updates = ctx.body.get("updates", [])
    if not updates:
        from .errors import GovernanceError
        raise GovernanceError("missing updates array", "invalid_request")
    graph = project_service.load_project_graph(project_id)
    results = []
    for upd in updates:
        node_id = upd.get("node_id")
        attrs = upd.get("attrs", {})
        try:
            ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
            safe_attrs = {k: v for k, v in attrs.items() if k in ALLOWED_ATTRS}
            graph.update_node_attrs(node_id, safe_attrs)
            results.append({"node_id": node_id, "status": "updated"})
        except Exception as e:
            results.append({"node_id": node_id, "status": "error", "error": str(e)})
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"updated": len([r for r in results if r["status"] == "updated"]), "results": results}


@route("POST", "/api/wf/{project_id}/node-delete")
def handle_node_delete(ctx: RequestContext):
    """Delete nodes from graph and node_state. Coordinator only.

    Body: {"nodes": ["L1.1", "L1.2", ...], "reason": "..."}
    """
    project_id = ctx.get_project_id()
    nodes = ctx.body.get("nodes", [])
    reason = ctx.body.get("reason", "")
    if not nodes:
        from .errors import GovernanceError
        raise GovernanceError("missing nodes array", "invalid_request")

    graph = project_service.load_project_graph(project_id)
    deleted = []
    skipped = []
    for nid in nodes:
        try:
            graph.remove_node(nid)
            deleted.append(nid)
        except Exception:
            skipped.append({"node_id": nid, "reason": "not in graph"})

    # Save graph
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")

    # Remove from node_state DB + audit
    with DBContext(project_id) as conn:
        for nid in deleted:
            conn.execute("DELETE FROM node_state WHERE project_id = ? AND node_id = ?",
                         (project_id, nid))
        audit_service.record(conn, project_id, "node.batch_delete",
                             node_ids=deleted, reason=reason)

    return {"deleted": len(deleted), "skipped": skipped, "reason": reason}


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


@route("GET", "/api/mem/{project_id}/search")
def handle_mem_search(ctx: RequestContext):
    """Full-text search across memories (FTS5 or semantic depending on backend)."""
    project_id = ctx.get_project_id()
    q = ctx.query.get("q", "")
    top_k = int(ctx.query.get("top_k", "5"))
    if not q:
        return {"error": "MISSING_QUERY", "message": "q parameter required"}, 400
    with DBContext(project_id) as conn:
        results = memory_service.search_memories(conn, project_id, q, top_k)
    return {"results": results, "count": len(results), "query": q}


@route("POST", "/api/mem/{project_id}/relate")
def handle_mem_relate(ctx: RequestContext):
    """Create a relation between two ref_ids."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    from_ref = body.get("from_ref_id", "")
    relation = body.get("relation", "")
    to_ref = body.get("to_ref_id", "")
    if not from_ref or not relation or not to_ref:
        return {"error": "MISSING_FIELDS", "message": "from_ref_id, relation, to_ref_id required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        result = get_backend().relate(conn, project_id, from_ref, relation, to_ref, body.get("metadata"))
    return 201, result


@route("GET", "/api/mem/{project_id}/expand")
def handle_mem_expand(ctx: RequestContext):
    """Traverse relation graph from a ref_id."""
    project_id = ctx.get_project_id()
    ref_id = ctx.query.get("ref_id", "")
    depth = int(ctx.query.get("depth", "2"))
    if not ref_id:
        return {"error": "MISSING_REF_ID", "message": "ref_id parameter required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        results = get_backend().expand(conn, project_id, ref_id, depth)
    return {"results": results, "count": len(results), "ref_id": ref_id, "depth": depth}


@route("POST", "/api/mem/{project_id}/promote")
def handle_mem_promote(ctx: RequestContext):
    """Promote a memory to global scope (creates a cross-project copy)."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    memory_id = body.get("memory_id", "")
    target_scope = body.get("target_scope", "global")
    reason = body.get("reason", "")
    if not memory_id:
        return {"error": "MISSING_FIELD", "message": "memory_id required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.promote_memory(
            conn, project_id, memory_id,
            target_scope=target_scope, reason=reason,
            actor_id=session.get("principal_id", ""),
        )
    return result


@route("POST", "/api/mem/{project_id}/register-pack")
def handle_mem_register_pack(ctx: RequestContext):
    """Register a domain pack (kind definitions) for a project."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    domain = body.get("domain", "development")
    types = body.get("types", {})
    if not types:
        return {"error": "MISSING_FIELD", "message": "types dict required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.register_domain_pack(
            conn, project_id, domain, types,
            actor_id=session.get("principal_id", ""),
        )
    return result


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
    """Create a task. Auth optional — uses principal_id if token provided, else 'anonymous'.

    Phase 4: Auto-enriches metadata with operation_type, intent_hash, and
    runs conflict rules for non-system task types.
    """
    project_id = ctx.get_project_id()
    from . import task_registry
    from .conflict_rules import extract_operation_type, compute_intent_hash, check_conflicts
    created_by = "anonymous"
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                created_by = session.get("principal_id", "anonymous")
        except Exception:
            pass

    prompt = ctx.body.get("prompt", "")
    task_type = ctx.body.get("type", "task")
    metadata = ctx.body.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}

    # Auto-enrich metadata
    if "operation_type" not in metadata:
        metadata["operation_type"] = extract_operation_type(prompt)
    if "intent_hash" not in metadata:
        metadata["intent_hash"] = compute_intent_hash(prompt)
    if "intent_summary" not in metadata:
        metadata["intent_summary"] = prompt[:200]

    # Run conflict rules for user-facing task types (not auto-chain internal)
    rule_decision = None
    if task_type in ("pm", "dev", "coordinator") and created_by not in ("auto-chain", "auto-chain-retry"):
        with DBContext(project_id) as conn:
            rule_decision = check_conflicts(
                conn, project_id,
                target_files=metadata.get("target_files", []),
                operation_type=metadata["operation_type"],
                intent_hash=metadata["intent_hash"],
                prompt=prompt,
                depends_on=metadata.get("depends_on"),
            )
        metadata["rule_decision"] = rule_decision["decision"]
        metadata["rule_reason"] = rule_decision["reason"]

    with DBContext(project_id) as conn:
        result = task_registry.create_task(
            conn, project_id,
            prompt=prompt,
            task_type=task_type,
            related_nodes=ctx.body.get("related_nodes"),
            created_by=created_by,
            priority=int(ctx.body.get("priority", 0)),
            max_attempts=int(ctx.body.get("max_attempts", 3)),
            metadata=metadata,
        )
    # Attach rule decision to response
    if rule_decision:
        result["rule_decision"] = rule_decision
    return result


@route("POST", "/api/task/{project_id}/claim")
def handle_task_claim(ctx: RequestContext):
    """Claim a task. Auth optional — uses principal_id if token provided, else body worker_id."""
    project_id = ctx.get_project_id()
    from . import task_registry
    worker_id = ctx.body.get("worker_id", "anonymous")
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                worker_id = session.get("principal_id", worker_id)
        except Exception:
            pass
    with DBContext(project_id) as conn:
        task = task_registry.claim_task(conn, project_id, worker_id)
        if task is None:
            return {"task": None, "message": "No tasks available"}
        return {"task": task}


@route("POST", "/api/task/{project_id}/complete")
def handle_task_complete(ctx: RequestContext):
    """Complete a task. No auth required."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.complete_task(
            conn, ctx.body.get("task_id", ""),
            status=ctx.body.get("status", "succeeded"),
            result=ctx.body.get("result"),
            error_message=ctx.body.get("error_message", ""),
            project_id=project_id,
            completed_by=ctx.body.get("worker_id", ""),
            override_reason=ctx.body.get("override_reason", ""),
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


@route("GET", "/api/runtime/{project_id}")
def handle_runtime(ctx: RequestContext):
    """Runtime projection — read-only view from Task Registry. No state of its own."""
    project_id = ctx.get_project_id()
    from . import task_registry, session_context
    with DBContext(project_id) as conn:
        active = task_registry.list_tasks(conn, project_id, status="running")
        queued = task_registry.list_tasks(conn, project_id, status="queued")
        claimed = task_registry.list_tasks(conn, project_id, status="claimed")
        pending_notify = task_registry.list_pending_notifications(conn, project_id)

    context = session_context.load_snapshot(project_id)

    return {
        "project_id": project_id,
        "active_tasks": active,
        "queued_tasks": queued,
        "claimed_tasks": claimed,
        "pending_notifications": pending_notify,
        "context": context,
        "summary": {
            "active": len(active),
            "queued": len(queued),
            "claimed": len(claimed),
            "pending_notify": len(pending_notify),
        },
    }


@route("POST", "/api/task/{project_id}/progress")
def handle_task_progress(ctx: RequestContext):
    """Update task progress heartbeat."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.update_progress(
            conn, ctx.body.get("task_id", ""),
            phase=ctx.body.get("phase", "running"),
            percent=int(ctx.body.get("percent", 0)),
            message=ctx.body.get("message", ""),
        )


@route("POST", "/api/task/{project_id}/notify")
def handle_task_notify(ctx: RequestContext):
    """Mark task notification as sent."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.mark_notified(conn, ctx.body.get("task_id", ""))


@route("POST", "/api/task/{project_id}/recover")
def handle_task_recover(ctx: RequestContext):
    """Recover stale tasks with expired leases."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.recover_stale_tasks(conn, project_id)


# --- Health ---

@route("GET", "/api/health")
def handle_health(ctx: RequestContext):
    return {"status": "ok", "service": "governance", "port": PORT,
            "version": SERVER_VERSION, "pid": SERVER_PID}


@route("GET", "/api/version-check/{project_id}")
def handle_version_check(ctx: RequestContext):
    """Check chain version vs git HEAD. All data from DB (synced by executor).

    executor_worker polls git on host and writes to DB via /api/version-sync.
    This endpoint just reads DB — no git, no MCP, no external HTTP calls.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)

    row = conn.execute(
        "SELECT chain_version, updated_at, git_head, dirty_files, git_synced_at "
        "FROM project_version WHERE project_id=?", (pid,)
    ).fetchone()

    if not row:
        return {
            "ok": True, "project_id": pid,
            "head": "unknown", "chain_version": "(not set)",
            "dirty": False, "dirty_files": [],
            "message": "Project not initialized",
            "generated_at": _utc_now(), "project_version": "unknown",
        }

    chain_ver = row["chain_version"]
    git_head = row["git_head"] or ""
    dirty_files = json.loads(row["dirty_files"] or "[]")
    git_synced = row["git_synced_at"] or ""

    # Compare
    ok = True
    parts = []

    if not git_head:
        parts.append("Executor has not synced git status yet")
    elif not (git_head.startswith(chain_ver) or chain_ver.startswith(git_head)):
        ok = False
        parts.append(f"HEAD ({git_head}) != CHAIN_VERSION ({chain_ver})")
    if dirty_files:
        ok = False
        parts.append(f"{len(dirty_files)} uncommitted files")

    return {
        "ok": ok,
        "project_id": pid,
        "head": git_head or "unknown",
        "chain_version": chain_ver,
        "chain_updated_at": row["updated_at"],
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "git_synced_at": git_synced,
        "message": "; ".join(parts),
        "generated_at": _utc_now(),
        "project_version": chain_ver,
    }


@route("POST", "/api/version-sync/{project_id}")
def handle_version_sync(ctx: RequestContext):
    """Executor syncs git status from host machine. Lightweight, no auth."""
    pid = ctx.get_project_id()
    body = ctx.body or {}

    git_head = body.get("git_head", "")
    dirty_files = body.get("dirty_files", [])
    if not git_head:
        return {"error": "missing git_head"}, 400

    now = _utc_now()

    def _do_sync():
        conn = independent_connection(pid)
        try:
            conn.execute("""
                UPDATE project_version
                SET git_head = ?, dirty_files = ?, git_synced_at = ?
                WHERE project_id = ?
            """, (git_head, json.dumps(dirty_files), now, pid))
            conn.commit()
        finally:
            conn.close()

    _retry_on_busy(_do_sync)
    return {"ok": True, "git_head": git_head, "dirty_files": dirty_files, "synced_at": now}


@route("POST", "/api/version-update/{project_id}")
def handle_version_update(ctx: RequestContext):
    """Update chain_version. 5-step validation: token + fields + lifecycle + version + audit."""
    pid = ctx.get_project_id()
    body = ctx.body or {}

    # Validation uses a short-lived independent connection to avoid WAL lock
    # contention with the shared server connection.  The final write is wrapped
    # with _retry_on_busy for resilience against concurrent merges.

    def _open():
        return independent_connection(pid)

    # Step 1: Internal token check (validation — no DB needed yet)
    expected_token = os.environ.get("VERSION_UPDATE_TOKEN", "")
    provided_token = (ctx.handler.headers.get("X-Internal-Token", "") if ctx.handler else "") or body.get("internal_token", "")
    if expected_token and provided_token != expected_token:
        conn = _open()
        try:
            _audit_version_update(conn, pid, body, "rejected", "INVALID_TOKEN")
        finally:
            conn.close()
        return {"error": "INVALID_TOKEN", "message": "Internal token mismatch"}, 403

    # Step 2: Field completeness
    required = ("chain_version", "updated_by")
    missing = [f for f in required if not body.get(f)]
    if missing:
        conn = _open()
        try:
            _audit_version_update(conn, pid, body, "rejected", "MISSING_FIELDS")
        finally:
            conn.close()
        return {"error": "MISSING_FIELDS", "message": f"Missing: {missing}"}, 400

    # Step 3: Lifecycle validation
    updated_by = body["updated_by"]
    if updated_by not in ("auto-chain", "init", "register", "merge-service"):
        conn = _open()
        try:
            _audit_version_update(conn, pid, body, "rejected", "INVALID_UPDATED_BY")
        finally:
            conn.close()
        return {"error": "INVALID_UPDATED_BY", "message": f"updated_by '{updated_by}' not allowed"}, 403

    task_id = body.get("task_id", "")
    chain_stage = body.get("chain_stage", "")
    if updated_by == "auto-chain" and chain_stage and chain_stage != "merge":
        conn = _open()
        try:
            _audit_version_update(conn, pid, body, "rejected", "INVALID_CHAIN_STAGE")
        finally:
            conn.close()
        return {"error": "INVALID_CHAIN_STAGE", "message": f"Expected merge, got {chain_stage}"}, 400

    # Step 3b: Chain link validation — verify task_id references a succeeded merge task
    if updated_by in ("auto-chain", "merge-service") and task_id:
        conn = _open()
        try:
            task_row = conn.execute(
                "SELECT status, type FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_row and task_row["status"] != "succeeded":
                _audit_version_update(conn, pid, body, "rejected", "TASK_NOT_SUCCEEDED")
                return {"error": "TASK_NOT_SUCCEEDED",
                        "message": f"Task {task_id} status is {task_row['status']}, expected succeeded"}, 400
            # Note: task_row could be None if task is in a different DB or not found — allow (backward compat)
        finally:
            conn.close()

    # Step 4: Version consistency (optional — old_version check)
    old_version = body.get("old_version")
    if old_version:
        conn = _open()
        try:
            row = conn.execute("SELECT chain_version FROM project_version WHERE project_id=?", (pid,)).fetchone()
            current = row["chain_version"] if row else None
        finally:
            conn.close()
        if current and old_version != current:
            conn2 = _open()
            try:
                _audit_version_update(conn2, pid, body, "rejected", "OLD_VERSION_MISMATCH")
            finally:
                conn2.close()
            return {"error": "OLD_VERSION_MISMATCH",
                    "message": f"Expected {old_version}, DB has {current}"}, 409

    # Step 5: Update + audit — wrapped with retry for SQLITE_BUSY resilience
    new_version = body["chain_version"]
    now = _utc_now()

    def _do_update():
        conn = independent_connection(pid)
        try:
            conn.execute("""
                INSERT INTO project_version (project_id, chain_version, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    chain_version=excluded.chain_version,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
            """, (pid, new_version, now, updated_by))
            conn.commit()
            _audit_version_update(conn, pid, body, "success", "")
        finally:
            conn.close()

    _retry_on_busy(_do_update)
    return {"ok": True, "chain_version": new_version, "updated_at": now}


def _audit_version_update(conn, pid, body, result, reason):
    """Write audit record for every version-update attempt."""
    try:
        audit_service.record(
            conn, pid, "version.update_attempt",
            actor=body.get("updated_by", "unknown"),
            details={
                "task_id": body.get("task_id", ""),
                "old_version": body.get("old_version", ""),
                "new_version": body.get("chain_version", ""),
                "chain_stage": body.get("chain_stage", ""),
                "updated_by": body.get("updated_by", ""),
                "result": result,
                "reject_reason": reason,
            },
        )
    except Exception:
        pass  # audit failure should not block


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


@route("GET", "/api/context-snapshot/{project_id}")
def handle_context_snapshot(ctx: RequestContext):
    """Return minimal base context for AI session startup (~500 tokens).

    Single API call providing point-in-time consistent snapshot.
    AI can query on-demand APIs for more details.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)
    role = ctx.query.get("role", ["coordinator"])[0]
    task_id = ctx.query.get("task_id", [""])[0]
    now = _utc_now()

    # Task summary — recent 3 tasks
    task_summary = []
    try:
        for row in conn.execute(
            "SELECT task_id, type, status FROM tasks ORDER BY created_at DESC LIMIT 3"
        ).fetchall():
            task_summary.append({
                "task_id": row["task_id"],
                "type": row["type"],
                "status": row["status"],
            })
    except Exception:
        pass

    # Project state
    ver_row = conn.execute(
        "SELECT chain_version, updated_at, dirty_files FROM project_version WHERE project_id=?",
        (pid,)
    ).fetchone()
    dirty_files = json.loads(ver_row["dirty_files"] or "[]") if ver_row and ver_row["dirty_files"] else []
    project_state = {
        "chain_version": ver_row["chain_version"] if ver_row else "unknown",
        "dirty": bool(dirty_files),
    }

    # Node summary (one-line)
    node_counts = {}
    for row in conn.execute(
        "SELECT verify_status, COUNT(*) as cnt FROM node_state WHERE project_id=? GROUP BY verify_status",
        (pid,)
    ).fetchall():
        node_counts[row["verify_status"]] = row["cnt"]

    # Recent memories (top 3 by relevance)
    recent_memories = []
    try:
        all_mems = memory_service.query_all(pid, active_only=True)
        task_prompt = task_summary.get("prompt", "")
        scored = []
        for m in all_mems:
            score = 0
            s = m.get("structured", {}) or {}
            if s.get("followup_needed"):
                score += 10
            if m.get("kind") == "failure_pattern":
                score += 5
            if m.get("kind") == "decision":
                score += 2
            if m.get("module", "") and m["module"] in task_prompt:
                score += 3
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for _, m in scored[:3]:
            recent_memories.append({
                "module": m.get("module", ""),
                "kind": m.get("kind", ""),
                "content": (m.get("content", ""))[:200],
            })
    except Exception:
        pass

    # Task chain context (if task_id provided)
    task_chain = None
    if task_id:
        try:
            from .chain_context import get_store
            task_chain = get_store().get_chain(task_id, role=role)
        except Exception:
            pass

    result = {
        "snapshot_at": now,
        "project_id": pid,
        "role": role,
        "task_summary": task_summary,
        "project_state": project_state,
        "node_summary": node_counts,
        "recent_memories": recent_memories,
        "constraints": "All changes through auto-chain",
        "generated_at": now,
        "project_version": project_state["chain_version"],
    }
    if task_chain:
        result["task_chain"] = task_chain
    return result


# --- Documentation ---

_DOCS = {
    "overview": {
        "title": "Governance Service Overview",
        "description": "Workflow governance service for multi-agent coordination. Manages project initialization, role assignment, node verification, release gating, memory, and audit.",
        "base_url": "http://localhost:40000",
        "api_prefix": "/api",
        "gateway_prefix": "/gateway",
        "auth": "No authentication required. All APIs work without tokens. Optional X-Gov-Token header is accepted for backward compatibility but not enforced.",
    },
    "quickstart": {
        "title": "Coordinator Session Quickstart",
        "base_url": "http://localhost:40000",
        "prerequisites": "Human has already run init_project.py and has the coordinator refresh_token (gov-xxx).",
        "steps": [
            {
                "step": 1,
                "phase": "AUTH",
                "action": "Exchange refresh_token for access_token (4h TTL)",
                "method": "POST /api/token/refresh",
                "body": {"refresh_token": "gov-xxx (from init_project.py)"},
                "returns": "access_token (gat-xxx), expires_in_sec, session_id, project_id, role",
                "note": "Use access_token for all subsequent API calls. Auto-renew before expiry.",
            },
            {
                "step": 2,
                "phase": "LIFECYCLE",
                "action": "Register agent and get a lease",
                "method": "POST /api/agent/register",
                "headers": {"X-Gov-Token": "gat-xxx (access_token)"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": "lease_id, heartbeat_interval_sec (120s)",
                "note": "Heartbeat every 2 min to renew lease. Lease expires in 5 min without heartbeat.",
            },
            {
                "step": 3,
                "phase": "CONTEXT",
                "action": "Load previous session context (if any)",
                "method": "GET /api/context/{project_id}/load",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{context: {...}, exists: true/false}",
                "note": "Contains current_focus, active_nodes, pending_tasks, recent_messages from last session.",
            },
            {
                "step": 4,
                "phase": "CONTEXT",
                "action": "Assemble task-aware context from memory",
                "method": "POST /api/context/{project_id}/assemble",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"task_type": "dev_general", "token_budget": 5000},
                "returns": "Prioritized memories (pitfalls, decisions, architecture) within token budget",
                "note": "Task types: dev_general, telegram_handler, verify_node, code_review, release_check",
            },
            {
                "step": 5,
                "phase": "TELEGRAM",
                "action": "Bind to Telegram chat for message relay",
                "method": "POST /gateway/bind",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "project_id": "amingClaw"},
                "note": "After binding, user messages in Telegram are pushed to Redis Stream chat:inbox:{hash}.",
            },
            {
                "step": 6,
                "phase": "TELEGRAM",
                "action": "Consume messages from Redis Stream",
                "code": "from telegram_gateway.chat_proxy import ChatProxy\nproxy = ChatProxy(token='gat-xxx', gateway_url='http://localhost:40000', redis_url='redis://localhost:40079/0')\nproxy.start(on_message=handler)  # background thread",
                "note": "ChatProxy uses XREADGROUP+ACK. Unacked messages survive crashes.",
            },
            {
                "step": 7,
                "phase": "WORK",
                "action": "Check project status",
                "method": "GET /api/wf/{project_id}/summary",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{total_nodes, by_status: {pending, testing, t2_pass, qa_pass, ...}}",
            },
            {
                "step": 8,
                "phase": "WORK",
                "action": "Verify nodes (tester role)",
                "method": "POST /api/wf/{project_id}/verify-update",
                "headers": {"X-Gov-Token": "tester-token"},
                "body": {
                    "nodes": ["L1.3"],
                    "status": "t2_pass",
                    "evidence": {"type": "test_report", "producer": "tester-001", "tool": "pytest", "summary": {"passed": 42, "failed": 0}},
                },
                "note": "Flow: pending->testing->t2_pass (tester), t2_pass->qa_pass (qa). Evidence required.",
            },
            {
                "step": 9,
                "phase": "WORK",
                "action": "Reply to Telegram user",
                "method": "POST /gateway/reply",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "text": "Task completed"},
                "note": "Or use proxy.reply('text') from ChatProxy.",
            },
            {
                "step": 10,
                "phase": "SAVE",
                "action": "Save session context before exit",
                "method": "POST /api/context/{project_id}/save",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"context": {"current_focus": "...", "active_nodes": ["..."], "pending_tasks": ["..."], "recent_messages": []}},
                "note": "Use expected_version for optimistic locking. Context persists to Redis (24h TTL) + SQLite.",
            },
            {
                "step": 11,
                "phase": "EXIT",
                "action": "Deregister agent",
                "method": "POST /api/agent/deregister",
                "body": {"lease_id": "lease-xxx"},
                "note": "Releases lease. Gateway detects offline, queues messages for next session.",
            },
        ],
        "lifecycle_summary": "AUTH(token) -> LIFECYCLE(register) -> CONTEXT(load+assemble) -> TELEGRAM(bind+consume) -> WORK(verify+reply) -> SAVE(context) -> EXIT(deregister)",
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
        "title": "Telegram Gateway Integration (v5.1)",
        "description": "Gateway 只做消息收发。非命令消息启动 Coordinator CLI session 处理。Coordinator 负责对话+决策+任务编排。",
        "architecture": "Telegram <-> Gateway (Docker) -> Claude CLI session (Coordinator) -> Governance API",
        "v5_1_change": "Gateway 不再分类 query/task/chat，不再直接创建 task。所有决策权归 Coordinator。",
        "role_boundary": {
            "gateway": "消息收发 + /command 处理。不做决策、不创建 task。",
            "coordinator": "对话 + 决策 + 任务编排。不自己写代码。",
            "dev_executor": "代码执行。不和用户对话。",
        },
        "gateway_api": {
            "POST /gateway/bind": "Bind coordinator token to chat_id. Body: {token, chat_id, project_id}",
            "POST /gateway/reply": "Send message to Telegram. Body: {token, chat_id?, text}. If no chat_id, uses bound chat.",
            "POST /gateway/unbind": "Unbind chat_id. Body: {chat_id}",
            "GET /gateway/health": "Gateway health check.",
            "GET /gateway/status": "List all active routes (bound coordinators).",
        },
        "message_flow": {
            "user_to_coordinator": "User sends text -> Gateway launches Claude CLI session (Coordinator) with context -> Coordinator processes -> reply via Gateway",
            "coordinator_to_user": "Coordinator stdout -> Gateway sends to Telegram",
            "task_creation": "Only Coordinator can create tasks (POST /api/task/create). Gateway cannot.",
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
    "coverage_check": {
        "title": "Feature Coverage Check (流程保障)",
        "description": "Detect untracked code changes before release. Reverse impact analysis: checks if all changed files have corresponding acceptance graph nodes.",
        "problem_solved": "Prevents features from being shipped without workflow tracking. Catches cases where developers implement code without first creating acceptance nodes.",
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Check if changed files are covered by acceptance graph nodes.",
                "headers": {"X-Gov-Token": "required"},
                "body": {
                    "files": ["agent/governance/outbox.py", "agent/new_feature.py"],
                },
                "returns": {
                    "covered": [{"file": "agent/governance/outbox.py", "nodes": ["L5.2", "L7.2"]}],
                    "uncovered": [{"file": "agent/new_feature.py", "suggestion": "Create a new node..."}],
                    "coverage_pct": 50.0,
                    "pass": False,
                },
            },
        },
        "integration_with_release_gate": {
            "description": "Run coverage-check before release-gate. If pass=false, block release until all files have nodes.",
            "recommended_flow": [
                "1. git diff --name-only main..HEAD → get changed files",
                "2. POST /api/wf/{pid}/coverage-check {files: [...]}",
                "3. If pass=false → create missing nodes, verify them",
                "4. POST /api/wf/{pid}/release-gate {profile: 'full'}",
            ],
        },
        "gate_types": {
            "L9.1 Feature Coverage Check": "Checks file→node mapping. Uncovered files → warning/block.",
            "L9.2 Node-Before-Code Gate": "verify-update checks if evidence.changed_files are all covered by some node's primary/secondary. Enforces 'create node before writing code'.",
            "L9.3 Artifacts Check": "qa_pass time checks if companion deliverables (api_docs, tests) are complete.",
            "L9.5 Gatekeeper Coverage": "release-gate auto-checks latest coverage-check result. No run / stale / failed → block release.",
        },
    },
    "gatekeeper": {
        "title": "Gatekeeper (发布前置校验)",
        "description": "Gatekeeper is a program (not an AI role) embedded in the governance service. It enforces pre-release checks at two levels: verify-update time and release-gate time.",
        "check_points": {
            "verify-update (前置拦截)": {
                "when": "Any node transitions to t2_pass or qa_pass",
                "what": "Checks that the node's declared primary files are all covered by graph nodes",
                "blocks": "If primary files are uncovered → rejects verify-update with error message",
                "module": "state_service._check_node_coverage → coverage_check.check_feature_coverage",
            },
            "release-gate (发布拦截)": {
                "when": "POST /api/wf/{pid}/release-gate is called",
                "what": "Checks that a coverage-check was run recently (within 1 hour) and passed",
                "blocks": "If never run → 'Run coverage-check first'. If stale → 'Re-run'. If failed → 'Uncovered files'.",
                "module": "gatekeeper.verify_pre_release → reads gatekeeper_checks table",
            },
        },
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Run coverage check AND auto-record result for gatekeeper.",
                "body": {"files": ["agent/governance/server.py"]},
                "side_effect": "Result written to gatekeeper_checks table for release-gate to read.",
            },
            "POST /api/wf/{project_id}/artifacts-check": {
                "description": "Check if nodes have required companion artifacts (docs, tests).",
                "body": {"nodes": ["L9.3"]},
            },
            "POST /api/wf/{project_id}/release-gate": {
                "description": "Release gate now includes gatekeeper check automatically.",
                "gatekeeper_field": "Response includes 'gatekeeper': {pass, checks, missing, stale}",
            },
        },
        "flow": [
            "1. Developer changes code",
            "2. POST /api/wf/{pid}/coverage-check {files: [changed files]}",
            "3a. pass:true → gatekeeper records pass → can proceed to release",
            "3b. pass:false → create missing nodes → re-run coverage-check",
            "4. POST /api/wf/{pid}/release-gate → gatekeeper auto-checks latest coverage result",
            "5. All pass → release approved",
        ],
        "storage": "gatekeeper_checks table in project SQLite DB. Each coverage-check auto-records.",
        "config": {
            "max_age_sec": "3600 (1 hour). Stale results require re-running coverage-check.",
            "required_checks": ["coverage_check"],
            "future_checks": ["security_scan", "dependency_audit", "performance_regression"],
        },
        "artifacts_auto_infer": {
            "title": "L9.6 Artifacts 自动推断",
            "description": "Nodes without explicit artifacts: declaration are auto-analyzed. If primary files contain @route → api_docs required. If test files declared → test_file required.",
            "rules": [
                "primary .py file has @route() → auto-require api_docs (section inferred from title)",
                "node declares test:[] with files → auto-require test_file existence",
                "declared artifacts take precedence over inferred",
            ],
            "module": "artifacts.infer_required_artifacts",
        },
        "deploy_coverage_check": {
            "title": "L9.7 Deploy 前置 Coverage-Check",
            "description": "deploy-governance.sh automatically runs coverage-check before building. Uncovered files block deployment.",
            "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
            "bypass": "SKIP_COVERAGE_CHECK=1 ./deploy-governance.sh (not recommended)",
            "limitation": "Only protects deploy-governance.sh path. docker compose up --build bypasses this check.",
            "mitigation": "verify_loop.sh should be run after any deployment to catch violations.",
        },
        "verify_loop": {
            "title": "Post-Verification Self-Check Script",
            "description": "scripts/verify_loop.sh runs 7 checks after any verification. Catches process violations that individual checks miss.",
            "usage": "bash scripts/verify_loop.sh <token> <project_id>",
            "checks": [
                "1. Node status — all qa_pass?",
                "2. Coverage — all changed files have graph nodes?",
                "3. Docs/Artifacts — nodes with @route have api_docs?",
                "4. Memory — code changes have corresponding dbservice entries? (L9.8)",
                "5. Docs update — API nodes have documentation sections?",
                "6. Gatekeeper — release-gate passes?",
            ],
            "memory_check_rule": "If >5 code files changed but <5 memories → FAIL. If >10 changed but <10 memories → WARN. Forces developers to document decisions and pitfalls.",
        },
        "scheduled_task_management": {
            "title": "L9.9 Scheduled Task 管理",
            "description": "Task prompt 模板存在 scripts/task-templates/ 目录，受 git 跟踪和 coverage-check 保护。",
            "template_location": "scripts/task-templates/telegram-handler.md",
            "variables": "{PROJECT_ID}, {TOKEN}, {CHAT_ID}, {STREAM}, {GROUP}, {BASE}",
            "key_fix": "消息必须用 XREADGROUP 消费 + XACK 确认，不能用 XRANGE（不跟踪消费进度）",
        },
        "human_intervention": {
            "title": "人工介入流程",
            "guide": "docs/human-intervention-guide.md",
            "boundaries": {
                "fully_automated": ["代码测试", "verify-update", "coverage-check", "记忆写入", "消息回复(非敏感)"],
                "needs_human_confirm": ["新节点创建", "baseline 批量变更", "跨项目操作"],
                "must_be_human": ["Token 管理", "发布确认", "rollback", "删除", "Scheduled Task 授权"],
                "human_verification": ["Telegram 交互行为", "UI 变更", "安全功能"],
            },
            "trigger_keywords": ["紧急", "urgent", "人工", "manual", "rollback", "delete", "release", "deploy"],
            "verification_flow": "AI 通知人类 → 人类测试 → 回复'验收通过/失败' → AI 提交 verify-update",
        },
    },
    "token_model": {
        "title": "Token Model (v5 简化版)",
        "description": "消息驱动模式下简化 token：project_token 不过期，Gateway 代理认证。去掉了 refresh/access 双令牌。",
        "tokens": {
            "project_token (gov-xxx)": {
                "holder": "Gateway / 人类",
                "ttl": "不过期",
                "scope": "项目 API 全权限 (coordinator 级别)",
                "obtain": "POST /api/init {project_id, password}",
            },
            "agent_token (gov-xxx)": {
                "holder": "dev/tester/qa 进程",
                "ttl": "24h",
                "scope": "受限 API (verify-update, heartbeat 等角色操作)",
                "obtain": "POST /api/role/assign (coordinator 分配)",
            },
        },
        "api": {
            "POST /api/init": "创建项目 + 获取 project_token",
            "POST /api/token/revoke": "人工撤销 project_token (需密码)",
            "POST /api/role/assign": "coordinator 分配 agent_token",
        },
        "deprecated": [
            "POST /api/token/refresh — 不再需要，project_token 不过期 [deprecated: v5, removal: v8]",
            "POST /api/token/rotate — 简化为 revoke + 重新 init [deprecated: v5, removal: v8]",
            "access_token (gat-*) — 不再使用",
        ],
        "security": [
            "init 密码保护（重置 token 需要密码）",
            "revoke 能力保留（人工可撤销）",
            "网络隔离（token 只在 localhost / Docker 内网）",
            "Gateway 代理认证（CLI session 不直接持有 token）",
            "agent_token 仍有 24h TTL（独立进程权限有时间限制）",
        ],
    },
    "agent_lifecycle": {
        "title": "Agent Lifecycle (租约管理)",
        "description": "Register/heartbeat/deregister agents with lease-based lifecycle. Orphan detection for stale agents.",
        "api": {
            "POST /api/agent/register": {
                "description": "Register an agent, get a lease.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": {"lease_id": "lease-xxx", "heartbeat_interval_sec": 120, "lease_ttl_sec": 600},
            },
            "POST /api/agent/heartbeat": {
                "description": "Renew lease. Call every 2 minutes.",
                "body": {"lease_id": "lease-xxx", "status": "idle|busy|processing", "worker_pid": 12345},
                "returns": {"ok": True, "lease_renewed_until": "..."},
            },
            "POST /api/agent/deregister": {
                "description": "Release lease on exit.",
                "body": {"lease_id": "lease-xxx"},
            },
            "GET /api/agent/orphans": {
                "description": "List agents with expired leases.",
                "query": "project_id=amingClaw (optional)",
                "returns": {"orphans": [{"session_id": "...", "principal_id": "...", "worker_pid": 12345, "reason": "no_active_lease"}]},
            },
            "POST /api/agent/cleanup": {
                "description": "Coordinator cleans up orphaned agents.",
                "headers": {"X-Gov-Token": "coordinator token"},
                "body": {"project_id": "amingClaw"},
            },
        },
        "lease_mechanism": "Agent registers → gets lease (5min TTL in Redis). Heartbeat every 2min renews. No heartbeat for 5min → lease expires → agent marked orphan. Gateway checks lease before routing messages.",
    },
    "session_context": {
        "title": "Session Context (跨会话状态)",
        "description": "Persist coordinator working state across sessions. Snapshot + append log with optimistic locking.",
        "api": {
            "POST /api/context/{project_id}/save": {
                "description": "Save session context snapshot.",
                "body": {
                    "context": {"current_focus": "...", "active_nodes": ["L1.3"], "pending_tasks": ["..."], "chat_id": 123, "recent_messages": []},
                    "expected_version": 5,
                },
                "returns": {"ok": True, "version": 6},
                "note": "expected_version enables optimistic locking. Omit for unconditional save.",
            },
            "GET /api/context/{project_id}/load": {
                "description": "Load latest session context.",
                "returns": {"context": {"...": "..."}, "exists": True},
            },
            "POST /api/context/{project_id}/log": {
                "description": "Append entry to session log.",
                "body": {"type": "decision|msg_in|msg_out|action", "content": {"text": "..."}},
            },
            "GET /api/context/{project_id}/log": {
                "description": "Read session log entries.",
                "query": "limit=50",
            },
            "POST /api/context/{project_id}/assemble": {
                "description": "Assemble task-aware context from dbservice memory.",
                "body": {"task_type": "dev_general|telegram_handler|verify_node|code_review|release_check", "token_budget": 5000},
            },
            "POST /api/context/{project_id}/archive": {
                "description": "Archive valuable content to long-term memory, clear expired context.",
            },
        },
        "storage": "Redis (24h TTL) + SQLite (durable fallback). Auto-archived by OutboxWorker after 24h inactivity.",
    },
    "task_registry": {
        "title": "Task Registry (任务管理)",
        "description": "SQLite-backed task lifecycle with dual-field status: execution_status (queued/claimed/running/succeeded/failed/cancelled/timed_out) + notification_status (none/pending/notified).",
        "api": {
            "POST /api/task/{project_id}/create": {
                "description": "Create a new task. DB is source of truth, task file is secondary.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"prompt": "...", "type": "task", "related_nodes": ["L1.3"], "priority": 1, "max_attempts": 3},
                "returns": {"task_id": "task-xxx", "status": "created"},
            },
            "POST /api/task/{project_id}/claim": {
                "description": "Claim next available task (FIFO by priority). Sets worker_id and lease_expires_at.",
                "body": {"task_id": "task-xxx", "worker_id": "executor-hostname"},
                "returns": {"task": {"task_id": "...", "prompt": "...", "attempt_num": 1}},
            },
            "POST /api/task/{project_id}/complete": {
                "description": "Mark task completed. Sets execution_status and notification_status=pending.",
                "body": {"task_id": "task-xxx", "execution_status": "succeeded|failed", "error_message": ""},
                "note": "Failed tasks auto-retry if attempt_count < max_attempts.",
            },
            "POST /api/task/{project_id}/notify": {
                "description": "Mark task as notified (user has been informed).",
                "body": {"task_id": "task-xxx"},
            },
            "GET /api/task/{project_id}/list": {
                "description": "List tasks.",
                "query": "status=running&limit=50",
            },
        },
    },
    "executor": {
        "title": "Executor (宿主机任务执行器)",
        "description": "常驻进程监听 pending/ 目录，claim 并执行 Claude/Codex CLI 任务。集成 Task Registry + Redis 通知。",
        "flow": {
            "1_pick": "scan pending/*.json (skip .tmp.json) → oldest first",
            "2_claim": "move to processing/ + Task Registry claim (DB insert queued→claimed→running)",
            "3_execute": "run_claude / run_codex / run_pipeline",
            "4_complete": "Task Registry complete (succeeded/failed) + Redis publish task:completed",
            "5_notify": "Gateway polls pending notifications → sends Telegram",
        },
        "features": {
            "atomic_write": "Gateway writes .tmp.json → fsync → rename to .json",
            "startup_recovery": "Scans processing/ for stale tasks (>5min), re-queues them",
            "heartbeat": "Background thread updates heartbeat_at every 30s",
            "tool_policy": "Commands checked against auto_allow/needs_approval/always_deny lists",
        },
    },
    "tool_policy": {
        "title": "Tool Policy (命令安全策略)",
        "description": "Executor 执行命令前检查安全策略。三级分类。",
        "levels": {
            "auto_allow": "git diff, pytest, npm test 等只读/测试命令 → 自动执行",
            "needs_approval": "git push, docker compose down, npm publish → 需要人工确认",
            "always_deny": "rm -rf /, shutdown, reboot → 永远拒绝",
        },
        "note": "当前为字符串匹配，后续升级为结构化命令能力模型。",
    },
    "deployment": {
        "title": "Deployment (部署流程)",
        "description": "开发→生产环境切换的自动化检测和部署流程。",
        "scripts": {
            "scripts/startup.sh": "一键启动所有服务（Docker + domain pack + executor）",
            "scripts/pre-deploy-check.sh": "部署前检测（节点状态/coverage/docs/memory/gatekeeper/staging/config/gateway）",
            "deploy-governance.sh": "零停机部署（自动调 pre-deploy-check → build → staging verify → swap）",
        },
        "checks": {
            "node_status": "所有节点 qa_pass",
            "coverage": "所有变更文件有对应节点",
            "docs": "API 文档 >= 10 sections",
            "memory": "dbservice 记忆 >= 5 entries",
            "gatekeeper": "release-gate PASS",
            "config_consistency": "dev/prod 环境变量一致",
            "staging": "staging 容器 health + smoke test",
            "gateway_channel": "Telegram 消息通道可达",
        },
        "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
    },
    "executor_api": {
        "title": "Executor API (Session 介入接口)",
        "description": "宿主机 Executor 内嵌 HTTP API (:40100)。Claude Code session 通过 curl 直接监控、介入、调试。",
        "port": 40100,
        "endpoints": {
            "monitoring": {
                "GET /health": "API 健康检查",
                "GET /status": "整体状态 (pending/processing/active sessions)",
                "GET /sessions": "活跃 AI 进程列表",
                "GET /tasks": "任务列表 (支持 project_id, status 过滤)",
                "GET /task/{id}": "单任务详情 (含 evidence, validator 日志)",
                "GET /trace/{id}": "链路追踪详情",
            },
            "intervention": {
                "POST /task/{id}/pause": "暂停运行中的任务",
                "POST /task/{id}/cancel": "取消任务 (终止 AI 进程)",
                "POST /task/{id}/retry": "重试失败的任务 (移回 pending)",
                "POST /cleanup-orphans": "清理僵尸进程和卡住的任务",
            },
            "direct_chat": {
                "POST /coordinator/chat": "直接启动 Coordinator session (绕过 Telegram)",
                "body": {"message": "...", "project_id": "amingClaw", "chat_id": 0},
                "note": "同步等待 AI 完成后返回，最多 120s",
            },
            "debugging": {
                "GET /validator/last-result": "最近一次校验结果 (层级/通过/拒绝详情)",
                "GET /context/{project_id}": "当前上下文组装结果",
                "GET /ai-session/{id}/output": "AI 原始输出 (stdout/stderr/exit_code)",
            },
        },
        "access": "仅宿主机 localhost:40100 可访问，不经过 nginx，不需要 token",
        "guide": "详见 docs/executor-api-guide.md",
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
    # PID lock — kill old process, prevent zombies
    _acquire_pid_lock()
    print(f"Governance v{SERVER_VERSION} (PID {SERVER_PID})")

    # Enable Redis Pub/Sub bridge for EventBus
    from .event_bus import get_event_bus
    redis = get_redis()
    if redis.available:
        get_event_bus().enable_redis_bridge()
        print("EventBus: Redis Pub/Sub bridge enabled")
    else:
        print("EventBus: Redis unavailable, in-process only")

    # Register chain context EventBus subscribers + recover active chains
    try:
        from .chain_context import register_events, get_store
        register_events()
        # Recover active chains for known projects
        from .db import _governance_root
        gov_root = _governance_root()
        if gov_root.exists():
            for pdir in gov_root.iterdir():
                if pdir.is_dir() and (pdir / "governance.db").exists():
                    get_store().recover_from_db(pdir.name)
        print("ChainContext: registered + recovered")
    except Exception as e:
        print(f"ChainContext: failed to start ({e})")

    # Start doc generator listener
    try:
        from .doc_generator import setup_listener
        setup_listener()
        print("DocGenerator: listening for node.created events")
    except Exception as e:
        print(f"DocGenerator: failed to start ({e})")

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
