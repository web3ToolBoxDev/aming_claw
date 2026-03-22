"""Dual-token model: refresh_token (90d) + access_token (4h).

refresh_token: Long-lived, held by human. Only used to get access_tokens.
access_token: Short-lived, used for all API calls. Auto-renewable.

Security operations:
  - revoke: Invalidate refresh_token (requires password)
  - rotate: Issue new refresh_token, invalidate old one
"""

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import logging

log = logging.getLogger(__name__)

ACCESS_TOKEN_TTL_HOURS = 4
REFRESH_TOKEN_TTL_DAYS = 90
AGENT_TOKEN_TTL_HOURS = 24


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token(prefix: str = "gov") -> str:
    return f"{prefix}-{secrets.token_hex(32)}"


def issue_access_token(conn: sqlite3.Connection, refresh_token: str) -> dict:
    """Exchange a refresh_token for a short-lived access_token.

    Returns: {access_token, expires_at, session_id, project_id, role}
    """
    rh = _hash(refresh_token)
    row = conn.execute(
        """SELECT session_id, principal_id, project_id, role, status, expires_at
           FROM sessions WHERE token_hash = ? AND status = 'active'""",
        (rh,),
    ).fetchone()

    if not row:
        from .errors import TokenInvalidError
        raise TokenInvalidError()

    session = dict(row)
    if session["expires_at"] < _utc_iso():
        from .errors import TokenExpiredError
        raise TokenExpiredError()

    # Generate access token
    access = _generate_token("gat")  # governance access token
    ah = _hash(access)
    expires = (datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Store access token mapping in Redis (not SQLite — ephemeral)
    from .redis_client import get_redis
    rc = get_redis()
    access_data = {
        "session_id": session["session_id"],
        "principal_id": session["principal_id"],
        "project_id": session["project_id"],
        "role": session["role"],
        "token_type": "access",
        "refresh_hash": rh,
        "expires_at": expires,
        "status": "active",
    }
    ttl_sec = ACCESS_TOKEN_TTL_HOURS * 3600
    rc.cache_token_session(ah, session["session_id"], ttl_sec)
    rc.cache_session(f"access:{ah}", access_data, ttl_sec)

    log.info("Issued access token for %s (project: %s, role: %s)",
             session["principal_id"], session["project_id"], session["role"])

    return {
        "access_token": access,
        "token_type": "access",
        "expires_at": expires,
        "expires_in_sec": ttl_sec,
        "session_id": session["session_id"],
        "project_id": session["project_id"],
        "role": session["role"],
    }


def revoke_refresh_token(conn: sqlite3.Connection, refresh_token: str) -> dict:
    """Revoke a refresh token. Requires the token itself."""
    rh = _hash(refresh_token)
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE token_hash = ?", (rh,),
    ).fetchone()

    if not row:
        from .errors import TokenInvalidError
        raise TokenInvalidError()

    session_id = row["session_id"]
    conn.execute(
        "UPDATE sessions SET status = 'revoked' WHERE session_id = ?",
        (session_id,),
    )

    # Invalidate Redis cache
    from .redis_client import get_redis
    rc = get_redis()
    rc.invalidate_session(session_id)

    log.info("Revoked refresh token for session %s", session_id)
    return {"ok": True, "session_id": session_id, "status": "revoked"}


def rotate_refresh_token(conn: sqlite3.Connection, old_refresh_token: str) -> dict:
    """Issue a new refresh token, invalidate the old one.

    Returns: {refresh_token, expires_at, session_id}
    """
    rh = _hash(old_refresh_token)
    row = conn.execute(
        """SELECT session_id, principal_id, project_id, role
           FROM sessions WHERE token_hash = ? AND status = 'active'""",
        (rh,),
    ).fetchone()

    if not row:
        from .errors import TokenInvalidError
        raise TokenInvalidError()

    session = dict(row)

    # Generate new refresh token
    new_token = _generate_token("gov")
    new_hash = _hash(new_token)
    expires = (datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "UPDATE sessions SET token_hash = ?, expires_at = ? WHERE session_id = ?",
        (new_hash, expires, session["session_id"]),
    )

    # Update Redis
    from .redis_client import get_redis
    rc = get_redis()
    ttl_sec = REFRESH_TOKEN_TTL_DAYS * 86400
    rc.cache_token_session(new_hash, session["session_id"], ttl_sec)

    log.info("Rotated refresh token for session %s", session["session_id"])

    return {
        "refresh_token": new_token,
        "expires_at": expires,
        "session_id": session["session_id"],
        "project_id": session["project_id"],
    }


def authenticate_access(conn: sqlite3.Connection, token: str) -> dict:
    """Authenticate using an access token (gat-...) or legacy refresh token (gov-...).

    Returns session dict compatible with existing role_service.authenticate.
    """
    if token.startswith("gat-"):
        # Access token — check Redis only (ephemeral)
        from .redis_client import get_redis
        rc = get_redis()
        ah = _hash(token)
        cached = rc.get_json(f"access:{ah}")
        if cached and cached.get("status") == "active":
            if cached.get("expires_at", "") >= _utc_iso():
                cached["scope"] = []
                return cached
        from .errors import TokenExpiredError
        raise TokenExpiredError()

    # Legacy: fall through to existing role_service.authenticate
    from . import role_service
    return role_service.authenticate(conn, token)
