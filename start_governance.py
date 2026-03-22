"""Start the governance service."""
import os
os.environ.setdefault("GOVERNANCE_PORT", "40006")

from agent.governance.server import main
main()
