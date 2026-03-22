"""Initialize a governance project and get the coordinator token.

Usage:
  python init_project.py

Interactive prompts for project name and password.
"""

import json
import sys
import getpass
import requests

GOVERNANCE_URL = "http://localhost:40000"


def main():
    print("=" * 50)
    print("  Governance Service — Project Init")
    print("=" * 50)
    print()

    # Check service is running
    try:
        resp = requests.get(f"{GOVERNANCE_URL}/api/health", timeout=5)
        if resp.status_code != 200:
            print(f"[ERROR] Service unhealthy: {resp.status_code}")
            sys.exit(1)
    except requests.ConnectionError:
        print(f"[ERROR] Cannot reach governance service at {GOVERNANCE_URL}")
        print("        Start it first: python start_governance.py")
        sys.exit(1)

    print(f"[OK] Service running at {GOVERNANCE_URL}")
    print()

    # Get project info
    project_id = input("Project ID (e.g. toolbox-client): ").strip()
    if not project_id:
        print("[ERROR] Project ID cannot be empty")
        sys.exit(1)

    project_name = input(f"Project name [{project_id}]: ").strip() or project_id
    password = getpass.getpass("Password (min 6 chars, for token reset): ")
    if len(password) < 6:
        print("[ERROR] Password must be at least 6 characters")
        sys.exit(1)

    print()
    print(f"Initializing project '{project_id}'...")

    resp = requests.post(
        f"{GOVERNANCE_URL}/api/init",
        json={
            "project_id": project_id,
            "password": password,
            "project_name": project_name,
        },
        timeout=10,
    )

    result = resp.json()

    if resp.status_code == 201:
        token = result["coordinator"]["token"]
        session_id = result["coordinator"]["session_id"]
        print()
        print("=" * 50)
        print("  PROJECT INITIALIZED SUCCESSFULLY")
        print("=" * 50)
        print()
        print(f"  Project:    {project_id}")
        print(f"  Session:    {session_id}")
        print()
        print("  Coordinator Token (SAVE THIS):")
        print()
        print(f"    {token}")
        print()
        print("=" * 50)
        print()
        print("  Next steps:")
        print(f"  1. Give this token to your Coordinator agent")
        print(f"  2. Coordinator uses POST /api/role/assign to create other roles")
        print(f"  3. To reset token later: re-run this script with the same password")
        print()
        print("  Set as env var:")
        print(f"    export GOV_COORDINATOR_TOKEN={token}")
        print()
    elif resp.status_code == 401:
        # Project exists, might be password reset
        print()
        print(f"[INFO] {result.get('message', 'Project already initialized')}")
        if "reset" in result.get("message", "").lower():
            token = result["coordinator"]["token"]
            print()
            print("  New Coordinator Token:")
            print(f"    {token}")
            print()
    else:
        print()
        print(f"[ERROR] {result.get('message', 'Unknown error')}")
        if result.get("details"):
            print(f"  Details: {json.dumps(result['details'])}")
        sys.exit(1)


if __name__ == "__main__":
    main()
