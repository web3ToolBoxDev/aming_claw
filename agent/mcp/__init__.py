"""Aming Claw MCP Server — Worker Pool + Event Push.

Provides a Model Context Protocol server that:
  - Manages Claude CLI worker pool (claim → execute → complete)
  - Bridges Redis governance events to MCP notifications
  - Exposes task/workflow/executor tools to Claude Code
"""
