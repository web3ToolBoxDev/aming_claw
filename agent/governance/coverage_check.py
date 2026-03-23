"""Feature Coverage Check — detect untracked code changes.

Reverse impact analysis: instead of "which nodes does this file affect",
ask "which files have no node tracking them at all".

Used in release-gate to block publishing untracked features.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


def check_feature_coverage(
    graph,
    changed_files: list[str],
) -> dict:
    """Check if all changed files are covered by at least one acceptance graph node.

    Args:
        graph: AcceptanceGraph instance
        changed_files: List of file paths from git diff

    Returns:
        {
            covered: [{file, nodes: [node_ids]}],
            uncovered: [{file, suggestion}],
            coverage_pct: float,
            pass: bool
        }
    """
    if not changed_files:
        return {"covered": [], "uncovered": [], "coverage_pct": 100.0, "pass": True}

    # Build reverse index: file → [node_ids]
    file_to_nodes = _build_file_index(graph)

    covered = []
    uncovered = []

    for f in changed_files:
        f_normalized = _normalize_path(f)
        matching_nodes = _find_matching_nodes(f_normalized, file_to_nodes)

        if matching_nodes:
            covered.append({"file": f, "nodes": matching_nodes})
        else:
            uncovered.append({
                "file": f,
                "suggestion": _suggest_node(f_normalized, graph),
            })

    total = len(changed_files)
    covered_count = len(covered)
    coverage_pct = (covered_count / total * 100) if total > 0 else 100.0

    return {
        "covered": covered,
        "uncovered": uncovered,
        "coverage_pct": round(coverage_pct, 1),
        "pass": len(uncovered) == 0,
        "total_files": total,
        "covered_files": covered_count,
        "uncovered_files": len(uncovered),
    }


def _build_file_index(graph) -> dict[str, list[str]]:
    """Build reverse index from file paths to node IDs."""
    index = {}
    for node_id in graph.list_nodes():
        node = graph.get_node(node_id)
        if not node:
            continue

        # Graph stores as "primary" and "secondary" (list of file paths)
        all_files = []
        for key in ("primary", "secondary", "primary_files", "secondary_files", "test"):
            val = node.get(key, [])
            if isinstance(val, list):
                all_files.extend(val)
            elif isinstance(val, str) and val:
                all_files.append(val)

        for file_path in all_files:
            fp = _normalize_path(file_path)
            if fp:
                index.setdefault(fp, []).append(node_id)

    return index


def _normalize_path(path: str) -> str:
    """Normalize file path for comparison."""
    return path.replace("\\", "/").strip().strip("/")


def _find_matching_nodes(file_path: str, file_to_nodes: dict) -> list[str]:
    """Find nodes that track a given file. Supports prefix/directory matching."""
    nodes = set()

    # Exact match
    if file_path in file_to_nodes:
        nodes.update(file_to_nodes[file_path])

    # Prefix match (e.g., file is "agent/governance/outbox.py",
    # node tracks "agent/governance/")
    for tracked_path, node_ids in file_to_nodes.items():
        if tracked_path.endswith("/") and file_path.startswith(tracked_path):
            nodes.update(node_ids)
        elif file_path == tracked_path:
            nodes.update(node_ids)
        # Glob-like: "dbservice/" matches "dbservice/index.js"
        elif tracked_path.endswith("/") and file_path.startswith(tracked_path):
            nodes.update(node_ids)

    return sorted(nodes)


def _suggest_node(file_path: str, graph) -> str:
    """Suggest which layer/node should track this file."""
    parts = file_path.split("/")

    if "governance" in parts:
        return "Should be tracked by an L4/L5/L6 node (governance layer)"
    elif "telegram_gateway" in parts:
        return "Should be tracked by a Gateway node (L5/L6)"
    elif "dbservice" in parts:
        return "Should be tracked by a dbservice node (L6.2 or L7)"
    elif "tests" in parts:
        return "Test file — track under the module it tests"
    elif "nginx" in parts:
        return "Infrastructure — track under Docker deployment node"
    elif "docs" in parts:
        return "Documentation — may not need tracking (non-functional)"
    else:
        return "Create a new node or add to an existing node's primary/secondary files"
