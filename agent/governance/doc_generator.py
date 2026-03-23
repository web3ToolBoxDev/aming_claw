"""Auto-generate API docs skeleton when a node is created.

Listens to node.created events via EventBus.
Scans the node's primary files for @route decorators.
Generates a skeleton doc entry in _DOCS.
"""

import logging
import os
import re

log = logging.getLogger(__name__)


def generate_doc_skeleton(node_id: str, node_data: dict, project_id: str) -> dict | None:
    """Generate a docs skeleton for a node based on its primary files.

    Scans files for @route("METHOD", "/path") patterns.

    Returns: doc dict if endpoints found, None otherwise.
    """
    primary_files = node_data.get("primary", [])
    if not primary_files:
        return None

    endpoints = []
    workspace = os.environ.get("WORKSPACE_PATH", "/workspace")

    for fp in primary_files:
        full_path = os.path.join(workspace, fp)
        if not os.path.exists(full_path):
            continue
        if not fp.endswith(".py"):
            continue

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Find @route decorators
            for match in re.finditer(
                r'@route\(["\'](\w+)["\'],\s*["\']([^"\']+)["\']\)',
                content,
            ):
                method = match.group(1)
                path = match.group(2)
                endpoints.append({"method": method, "path": path, "source": fp})
        except Exception:
            continue

    if not endpoints:
        return None

    title = node_data.get("title", node_id)
    description = node_data.get("description", "")

    # Build skeleton
    api_dict = {}
    for ep in endpoints:
        key = f"{ep['method']} {ep['path']}"
        api_dict[key] = f"[AUTO-GENERATED SKELETON] From {ep['source']}. TODO: add description."

    skeleton = {
        "title": f"{node_id}: {title}",
        "description": description or f"Auto-generated docs for {node_id}",
        "api": api_dict,
        "_skeleton": True,  # Marker: needs human review
        "_node_id": node_id,
        "_generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    }

    return skeleton


def register_skeleton_in_docs(node_id: str, skeleton: dict) -> bool:
    """Register a skeleton doc in the _DOCS dict."""
    try:
        from .server import _DOCS
        section_key = _make_section_key(node_id)

        # Don't overwrite existing complete docs
        if section_key in _DOCS and not _DOCS[section_key].get("_skeleton"):
            log.info("Doc section '%s' already exists and is not a skeleton, skipping", section_key)
            return False

        _DOCS[section_key] = skeleton
        log.info("Doc skeleton registered: %s (%d endpoints)", section_key, len(skeleton.get("api", {})))
        return True
    except Exception as e:
        log.warning("Failed to register doc skeleton: %s", e)
        return False


def _make_section_key(node_id: str) -> str:
    """Convert node ID to a docs section key."""
    return node_id.lower().replace(".", "_")


def on_node_created(payload: dict) -> None:
    """EventBus handler for node.created events."""
    node_id = payload.get("node_id", "")
    project_id = payload.get("project_id", "")
    node_data = payload.get("node_data", {})

    if not node_id or not node_data:
        return

    skeleton = generate_doc_skeleton(node_id, node_data, project_id)
    if skeleton:
        register_skeleton_in_docs(node_id, skeleton)
        log.info("Auto-generated doc skeleton for %s", node_id)


def setup_listener() -> None:
    """Subscribe to node.created events."""
    from . import event_bus
    event_bus.subscribe("node.created", on_node_created)
    log.info("DocGenerator: listening for node.created events")
