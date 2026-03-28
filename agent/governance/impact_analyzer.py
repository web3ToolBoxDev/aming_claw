"""Policy-based impact analysis.

Given file changes, determines which nodes need re-verification,
what tests to run, and in what order.

Also provides code→doc relationship inference: when code files change,
affected documentation files are surfaced so gates can enforce doc updates.
"""

from .enums import VerifyStatus, VerifyLevel
from .models import FileHitPolicy, PropagationPolicy, VerificationPolicy, ImpactAnalysisRequest

# Code path prefix → related documentation files
# Used by checkpoint gate to verify doc consistency on code changes
CODE_DOC_MAP = {
    "agent/telegram_gateway/": [
        "docs/telegram-project-binding-design.md",
        "docs/ai-agent-integration-guide.md",
        "README.md",
    ],
    "agent/governance/server.py": [
        "docs/ai-agent-integration-guide.md",
        "docs/p0-3-design.md",
        "README.md",
    ],
    "agent/governance/auto_chain.py": [
        "docs/p0-3-design.md",
        "docs/ai-agent-integration-guide.md",
        "docs/human-intervention-guide.md",
    ],
    "agent/governance/task_registry.py": [
        "docs/ai-agent-integration-guide.md",
        "README.md",
    ],
    "agent/governance/state_service.py": [
        "docs/workflow-governance-architecture-v2.md",
        "docs/p0-3-design.md",
    ],
    "agent/governance/role_service.py": [
        "docs/ai-agent-integration-guide.md",
    ],
    "agent/governance/gatekeeper.py": [
        "docs/production-guard.md",
    ],
    "agent/executor_api.py": [
        "docs/executor-api-guide.md",
    ],
    "agent/ai_lifecycle.py": [
        "docs/architecture-v6-executor-driven.md",
    ],
    "agent/deploy_chain.py": [
        "docs/deployment-guide.md",
    ],
    "agent/service_manager.py": [
        "docs/ai-agent-integration-guide.md",
    ],
    "agent/executor_worker.py": [
        "docs/ai-agent-integration-guide.md",
        "docs/p0-3-design.md",
    ],
    "agent/governance/memory_backend.py": [
        "docs/ai-agent-integration-guide.md",
        "docs/prd-memory-coordinator-executor.md",
    ],
    "agent/governance/memory_service.py": [
        "docs/ai-agent-integration-guide.md",
    ],
    "agent/governance/conflict_rules.py": [
        "docs/ai-agent-integration-guide.md",
        "docs/human-intervention-guide.md",
    ],
    "agent/governance/chain_context.py": [
        "docs/prd-memory-coordinator-executor.md",
        "docs/ai-agent-integration-guide.md",
    ],
}


def get_related_docs(changed_files: list[str]) -> set[str]:
    """Given code file changes, return set of docs that may need updating."""
    docs = set()
    for cf in changed_files:
        for pattern, doc_list in CODE_DOC_MAP.items():
            if pattern in cf or cf == pattern:
                docs.update(doc_list)
    return docs


class ImpactAnalyzer:
    """Analyzes the impact of file changes on the acceptance graph."""

    def __init__(self, graph, get_node_status_fn):
        """
        Args:
            graph: AcceptanceGraph instance.
            get_node_status_fn: callable(node_id) -> VerifyStatus
        """
        self.graph = graph
        self.get_status = get_node_status_fn

    def analyze(self, request: ImpactAnalysisRequest) -> dict:
        file_policy = request.file_policy or FileHitPolicy()
        prop_policy = request.propagation_policy or PropagationPolicy()
        ver_policy = request.verification_policy or VerificationPolicy()

        # Step 1: File → direct hit nodes
        direct_hit = self._file_match(request.changed_files, file_policy)

        # Step 2: Propagation
        affected = set(direct_hit)
        if prop_policy.follow_deps:
            for nid in list(direct_hit):
                try:
                    affected |= self.graph.descendants(nid)
                except Exception:
                    pass

        # Step 3: Pruning
        skipped = []
        if ver_policy.skip_already_passed:
            for nid in list(affected):
                try:
                    status = self.get_status(nid)
                    if status == VerifyStatus.QA_PASS and nid not in direct_hit:
                        affected.discard(nid)
                except Exception:
                    pass

        if ver_policy.respect_gates:
            for nid in list(affected):
                try:
                    gates = self.graph.get_gates(nid)
                    for gate in gates:
                        gate_nid = gate.node_id if hasattr(gate, 'node_id') else gate.get("node_id", "")
                        gate_status = self.get_status(gate_nid)
                        if gate_status in (VerifyStatus.FAILED, VerifyStatus.PENDING):
                            affected.discard(nid)
                            skipped.append({"node": nid, "reason": f"gate {gate_nid} is {gate_status.value}"})
                            break
                except Exception:
                    pass

        # Step 4: Group by verify level + topological sort
        by_phase = {"T1": [], "T2": [], "T3": [], "T4": []}
        level_map = {1: "T1", 2: "T2", 3: "T3", 4: "T4", 5: "T4"}

        for nid in affected:
            try:
                node_data = self.graph.get_node(nid)
                vl = node_data.get("verify_level", 1)
                if isinstance(vl, str):
                    try:
                        vl = int(vl)
                    except ValueError:
                        vl = 1
                phase = level_map.get(vl, "T4")
                by_phase[phase].append(nid)
            except Exception:
                by_phase["T4"].append(nid)

        # Topological order filtered to affected
        try:
            topo = self.graph.topological_order()
            ordered = [n for n in topo if n in affected]
        except Exception:
            ordered = sorted(affected)

        # Collect test files
        test_files = set()
        for nid in affected:
            try:
                node_data = self.graph.get_node(nid)
                for tf in node_data.get("test", []):
                    if tf and tf != "TBD" and tf != "[TBD]":
                        test_files.add(tf)
            except Exception:
                pass

        # Max verify level
        max_vl = 1
        for nid in direct_hit:
            try:
                max_vl = max(max_vl, self.graph.max_verify_level(nid))
            except Exception:
                pass

        # Step 5: Doc consistency — which docs should be reviewed
        related_docs = get_related_docs(request.changed_files)

        # Build affected_nodes list with node details
        affected_nodes = []
        for nid in ordered:
            try:
                nd = self.graph.get_node(nid)
                affected_nodes.append({
                    "node_id": nid,
                    "title": nd.get("title", ""),
                    "primary": nd.get("primary", []),
                    "verify_level": nd.get("verify_level", 1),
                    "is_direct": nid in direct_hit,
                })
            except Exception:
                affected_nodes.append({"node_id": nid, "is_direct": nid in direct_hit})

        return {
            "direct_hit": sorted(direct_hit),
            "affected_nodes": affected_nodes,
            "total_affected": len(affected),
            "verification_order": ordered,
            "by_phase": {k: sorted(v) for k, v in by_phase.items()},
            "skipped": skipped,
            "test_files": sorted(test_files),
            "max_verify": max_vl,
            "related_docs": sorted(related_docs),
        }

    def _file_match(self, changed_files: list[str], policy: FileHitPolicy) -> set[str]:
        """Match changed files to graph nodes."""
        import fnmatch as _fnmatch

        changed_set = set(changed_files)
        hits = set()

        for nid in self.graph.list_nodes():
            try:
                node_data = self.graph.get_node(nid)
            except Exception:
                continue

            if policy.match_primary:
                primary = set(node_data.get("primary", []))
                if primary & changed_set:
                    hits.add(nid)
                    continue

            if policy.match_secondary:
                secondary = set(node_data.get("secondary", []))
                if secondary & changed_set:
                    hits.add(nid)
                    continue

            if policy.match_config_glob:
                for pattern in policy.match_config_glob:
                    for cf in changed_files:
                        if _fnmatch.fnmatch(cf, pattern):
                            # Check if this file is in any of the node's file lists
                            all_files = set(node_data.get("primary", [])) | set(node_data.get("secondary", []))
                            if cf in all_files:
                                hits.add(nid)

        return hits
