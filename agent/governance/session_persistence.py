"""Session persistence — environment variable based token injection.

Trust chain:
  1. Human runs bootstrap → gets token (displayed once, not saved to disk)
  2. Human writes token to .env or sets as environment variable
  3. Agent reads token from env var at startup
  4. On restart → reads from .env again, no re-registration needed

Token naming convention:
  GOV_COORDINATOR_TOKEN=gov-xxx
  GOV_TESTER_TOKEN=gov-xxx
  GOV_DEV_TOKEN=gov-xxx
  GOV_QA_TOKEN=gov-xxx

Or generic:
  GOV_TOKEN=gov-xxx  (single-role agent)
"""

import os
import logging

log = logging.getLogger(__name__)

# Env var names by role
TOKEN_ENV_VARS = {
    "coordinator": "GOV_COORDINATOR_TOKEN",
    "tester":      "GOV_TESTER_TOKEN",
    "dev":         "GOV_DEV_TOKEN",
    "qa":          "GOV_QA_TOKEN",
    "gatekeeper":  "GOV_GATEKEEPER_TOKEN",
}

GENERIC_TOKEN_VAR = "GOV_TOKEN"


def get_token_from_env(role: str = "") -> str | None:
    """Read token from environment variable.

    Priority:
    1. Role-specific: GOV_{ROLE}_TOKEN
    2. Generic: GOV_TOKEN
    """
    if role:
        role_var = TOKEN_ENV_VARS.get(role.lower())
        if role_var:
            token = os.environ.get(role_var)
            if token:
                log.info("Token loaded from %s", role_var)
                return token

    token = os.environ.get(GENERIC_TOKEN_VAR)
    if token:
        log.info("Token loaded from %s", GENERIC_TOKEN_VAR)
        return token

    return None


def connect_from_env(
    governance_url: str = "http://localhost:40000",
    role: str = "",
    project_id: str = "",
) -> dict:
    """Connect to governance service using token from environment.

    Returns:
      {token, session, connected} on success
      {error, connected: False} on failure
    """
    import requests

    token = get_token_from_env(role)
    if not token:
        var_name = TOKEN_ENV_VARS.get(role.lower(), GENERIC_TOKEN_VAR)
        return {
            "error": f"No token found. Set {var_name} in .env or environment.",
            "connected": False,
        }

    # Validate token via heartbeat
    try:
        resp = requests.post(
            f"{governance_url}/api/role/heartbeat",
            json={"project_id": project_id, "status": "starting"},
            headers={
                "Content-Type": "application/json",
                "X-Gov-Token": token,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info("Connected to governance service (session: %s)", data.get("session_id"))
            return {
                "token": token,
                "session": data,
                "connected": True,
            }
        elif resp.status_code == 401:
            return {
                "error": "Token expired or invalid. Request new token from admin.",
                "connected": False,
            }
        else:
            return {
                "error": f"Unexpected response: {resp.status_code}",
                "connected": False,
            }
    except requests.ConnectionError:
        log.warning("Governance service unreachable at %s", governance_url)
        return {
            "token": token,
            "connected": False,
            "offline": True,
            "error": "Governance service unreachable. Token preserved for retry.",
        }


def check_team_status(
    project_id: str,
    token: str,
    governance_url: str = "http://localhost:40000",
) -> dict:
    """Check the status of all roles in the project.

    Returns:
    {
        "roles": {
            "coordinator": {"status": "active", "count": 1, ...},
            "tester": {"status": "missing", "count": 0, ...},
        },
        "warnings": [...],
        "healthy": bool
    }
    """
    import requests

    try:
        resp = requests.get(
            f"{governance_url}/api/role/{project_id}/sessions",
            headers={"X-Gov-Token": token},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e), "healthy": False}

    sessions = data.get("sessions", [])

    role_map = {}
    for s in sessions:
        r = s.get("role", "unknown")
        if r not in role_map:
            role_map[r] = {"status": "active", "count": 0, "principals": []}
        role_map[r]["count"] += 1
        role_map[r]["principals"].append(s.get("principal_id", ""))
        if s.get("status") == "stale":
            role_map[r]["status"] = "stale"

    required = ["coordinator", "tester", "dev"]
    warnings = []
    for r in required:
        if r not in role_map:
            role_map[r] = {"status": "missing", "count": 0, "principals": []}
            warnings.append(f"{r} role missing")

    if "qa" not in role_map:
        role_map["qa"] = {"status": "missing", "count": 0, "principals": []}
        warnings.append("qa role missing: blocks release")

    healthy = all(
        role_map.get(r, {}).get("status") == "active"
        for r in required
    )

    return {"roles": role_map, "warnings": warnings, "healthy": healthy}
