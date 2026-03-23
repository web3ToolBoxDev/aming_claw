"""NetworkX DAG management for the acceptance graph (Layer 1 — rules).

The graph is the "rule layer": node definitions, deps edges, gate policies.
It changes rarely (only when nodes are added/removed).
"""

import re
import json
import sys
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:
    import networkx as nx
except ImportError:
    nx = None

from .enums import VerifyLevel, GateMode
from .models import GateRequirement, NodeDef
from .errors import DAGError, NodeNotFoundError


class AcceptanceGraph:
    """DAG manager for the acceptance graph, backed by NetworkX."""

    def __init__(self):
        if nx is None:
            raise ImportError("networkx is required: pip install networkx")
        self.G = nx.DiGraph()       # deps edges
        self.gates_G = nx.DiGraph()  # gate edges

    # --- Persistence ---

    def load(self, path: str | Path) -> None:
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        self.G = nx.node_link_graph(data["deps_graph"])
        if "gates_graph" in data:
            self.gates_G = nx.node_link_graph(data["gates_graph"])

    def save(self, path: str | Path) -> None:
        data = {
            "version": 1,
            "deps_graph": nx.node_link_data(self.G),
            "gates_graph": nx.node_link_data(self.gates_G),
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # --- Markdown Import ---

    def import_from_markdown(self, md_path: str | Path) -> dict:
        """Parse acceptance-graph.md with tolerance for format variants."""
        content = Path(md_path).read_text(encoding="utf-8")
        warnings = []
        nodes_parsed = 0

        # Find all code blocks (nodes are inside ```)
        code_blocks = re.findall(r'```\n(.*?)```', content, re.DOTALL)
        all_text = "\n".join(code_blocks)

        # Parse node blocks: each starts with Lx.y
        node_pattern = re.compile(
            r'^(L\d+\.\d+)\s+(.+?)$',
            re.MULTILINE,
        )

        # Split into node blocks
        lines = all_text.split("\n")
        current_node = None
        current_block = []
        blocks = []

        for line in lines:
            m = node_pattern.match(line.strip())
            if m:
                if current_node:
                    blocks.append((current_node, current_block))
                current_node = m.group(1)
                current_block = [line]
            elif current_node:
                current_block.append(line)

        if current_node:
            blocks.append((current_node, current_block))

        for node_id, block_lines in blocks:
            try:
                node_def = self._parse_node_block(node_id, block_lines, warnings)
                self._add_parsed_node(node_def)
                nodes_parsed += 1
            except Exception as e:
                warnings.append(f"node {node_id}: {e}")

        return {"nodes_parsed": nodes_parsed, "warnings": warnings}

    def _parse_node_block(self, node_id: str, lines: list[str], warnings: list) -> NodeDef:
        """Parse a single node block with multi-format tolerance."""
        text = "\n".join(lines)
        first_line = lines[0] if lines else ""

        # Title: everything after the node ID on the first line, before status brackets
        title_match = re.search(r'^L\d+\.\d+\s+(.+?)(?:\s+\[|$)', first_line)
        title = title_match.group(1).strip() if title_match else ""

        # Build status: [impl:done] or impl:done or impl: done
        build = self._extract_field(text, [
            r'\[impl:(done|partial|missing)\]',
            r'impl:\s*(done|partial|missing)',
        ], "missing")

        # Verify status: [verify:pass] or verify:pass etc
        verify_status = self._extract_field(text, [
            r'\[verify:(pass|T2-pass|fail|pending|skipped)\]',
            r'verify:\s*(pass|T2-pass|fail|pending|skipped)',
        ], "pending")

        # Layer from ID
        layer_match = re.match(r'(L\d+)', node_id)
        layer = layer_match.group(1) if layer_match else "L0"
        layer_num = int(layer[1:])

        # Dependencies
        deps = self._extract_list(text, [r'deps:\s*\[(.*?)\]', r'deps:\[(.*?)\]'])

        # Gates
        gates_raw = self._extract_list(text, [r'gates:\s*\[(.*?)\]', r'gates:\[(.*?)\]'])

        # Gate mode
        gate_mode = self._extract_field(text, [
            r'gate_mode:\s*(\w+)', r'gate_mode:(\w+)',
        ], "auto")

        # Verify level
        verify_level_str = self._extract_field(text, [
            r'verify:\s*(L\d+)', r'verify:(L\d+)',
        ], f"L{min(layer_num + 1, 5)}")
        try:
            verify_level = int(verify_level_str[1:]) if verify_level_str.startswith("L") else 1
        except (ValueError, IndexError):
            verify_level = 1

        # Test coverage
        test_coverage = self._extract_field(text, [
            r'test_coverage:\s*(\w+)', r'test_coverage:(\w+)',
        ], "none")

        # File mappings
        primary = self._extract_list(text, [r'primary:\s*\[(.*?)\]', r'primary:\[(.*?)\]'])
        secondary = self._extract_list(text, [r'secondary:\s*\[(.*?)\]', r'secondary:\[(.*?)\]'])
        test_files = self._extract_list(text, [r'test:\s*\[(.*?)\]', r'test:\[(.*?)\]'])

        # Artifacts (multi-line: "- type: xxx\n  section: yyy")
        artifacts = self._parse_artifacts(text)

        # Description
        description = self._extract_field(text, [
            r'description:\s*(.+?)(?:\n|$)',
        ], "")

        # Propagation
        propagation = self._extract_field(text, [
            r'propagation:\s*(\w+)', r'propagation:(\w+)',
        ], None)

        # Guard
        guard = "GUARD" in first_line

        # Version
        version_match = re.search(r'v(\d+[\.\d]*)', first_line)
        version = f"v{version_match.group(1)}" if version_match else ""

        # Normalize verify_status to enum values
        from .enums import VerifyStatus, BuildStatus
        try:
            parsed_vs = VerifyStatus.from_str(verify_status).value
        except ValueError:
            parsed_vs = "pending"
        try:
            parsed_bs = f"impl:{build}" if not build.startswith("impl:") else build
            BuildStatus.from_str(parsed_bs)  # validate
        except ValueError:
            parsed_bs = "impl:missing"

        # Build gate requirements
        gate_reqs = [{"node_id": g, "min_status": "qa_pass", "policy": "default"} for g in gates_raw]

        node_def = NodeDef(
            id=node_id, title=title, layer=layer,
            verify_level=verify_level, gate_mode=gate_mode,
            test_coverage=test_coverage,
            primary=primary, secondary=secondary, test=test_files,
            propagation=propagation, guard=guard, version=version,
            gates=gate_reqs,
        )
        # Store parsed statuses for init_node_states to read
        node_def._parsed_verify_status = parsed_vs
        node_def._parsed_build_status = parsed_bs
        node_def._artifacts = artifacts
        node_def._description = description or ""
        return node_def

    def _extract_field(self, text: str, patterns: list[str], default=None) -> str | None:
        for p in patterns:
            m = re.search(p, text)
            if m:
                return m.group(1)
        return default

    def _parse_artifacts(self, text: str) -> list[dict]:
        """Parse artifacts block from node text.

        Format:
          artifacts:
            - type: api_docs
              section: coverage_check
            - type: test_file
        """
        artifacts = []
        in_artifacts = False
        current = {}

        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("artifacts:"):
                in_artifacts = True
                continue
            if not in_artifacts:
                continue
            # End of artifacts block (next field or empty)
            if stripped and not stripped.startswith("-") and not stripped.startswith("type:") and not stripped.startswith("section:") and not stripped.startswith("check:") and not stripped.startswith("required:"):
                break

            if stripped.startswith("- type:"):
                if current:
                    artifacts.append(current)
                current = {"type": stripped.split(":", 1)[1].strip()}
            elif ":" in stripped and current:
                key, val = stripped.split(":", 1)
                current[key.strip()] = val.strip()

        if current:
            artifacts.append(current)

        return artifacts

    def _extract_list(self, text: str, patterns: list[str]) -> list[str]:
        for p in patterns:
            m = re.search(p, text, re.DOTALL)
            if m:
                raw = m.group(1).strip()
                if not raw or raw == "TBD":
                    return []
                items = [x.strip().strip("'\"") for x in raw.split(",") if x.strip()]
                return [i for i in items if i and i != "TBD"]
        return []

    def _add_parsed_node(self, node_def: NodeDef):
        """Add a parsed node to both graphs."""
        attrs = node_def.to_dict()
        deps = attrs.pop("gates", [])  # gates handled separately

        self.G.add_node(node_def.id, **attrs)

        # Carry parsed statuses from markdown into node data
        if hasattr(node_def, "_parsed_verify_status"):
            self.G.nodes[node_def.id]["parsed_verify_status"] = node_def._parsed_verify_status
        if hasattr(node_def, "_parsed_build_status"):
            self.G.nodes[node_def.id]["parsed_build_status"] = node_def._parsed_build_status
        if hasattr(node_def, "_artifacts"):
            self.G.nodes[node_def.id]["artifacts"] = node_def._artifacts
        if hasattr(node_def, "_description"):
            self.G.nodes[node_def.id]["description"] = node_def._description

        self.G.nodes[node_def.id]["_deps"] = []
        self.G.nodes[node_def.id]["_gates_raw"] = deps

    def finalize_edges(self):
        """After all nodes are parsed, add edges."""
        for node_id, data in list(self.G.nodes(data=True)):
            # Parse deps from the stored text
            pass  # deps already added during import

    # --- Node CRUD ---

    def add_node(self, node_def: NodeDef, deps: list[str] = None) -> list[str]:
        """Add a new node. Returns list of warnings."""
        warnings = []
        deps = deps or []

        # Validate deps exist
        for dep in deps:
            if dep not in self.G:
                raise NodeNotFoundError(dep)

        # Add node
        self.G.add_node(node_def.id, **node_def.to_dict())

        # Add deps edges
        for dep in deps:
            self.G.add_edge(dep, node_def.id)

        # Gate mode auto: derive gates from deps where verify >= L3
        if node_def.gate_mode == "auto":
            derived_gates = self.auto_derive_gates(node_def.id)
            node_def.gates = [
                {"node_id": g, "min_status": "qa_pass", "policy": "default"}
                for g in derived_gates
            ]
            self.G.nodes[node_def.id]["gates"] = node_def.gates

        # Add gates edges
        for gate_req in node_def.gates:
            gate_nid = gate_req["node_id"] if isinstance(gate_req, dict) else gate_req.node_id
            if gate_nid in self.G:
                self.gates_G.add_node(gate_nid)
                self.gates_G.add_node(node_def.id)
                self.gates_G.add_edge(gate_nid, node_def.id)

        # Validate DAG
        errors = self.validate_dag()
        if errors:
            self.G.remove_node(node_def.id)
            raise DAGError(f"Adding {node_def.id} creates cycle", {"errors": errors})

        # Layer warning (not enforced)
        if deps:
            max_dep_layer = max(
                int(self.G.nodes[d].get("layer", "L0")[1:]) for d in deps
            )
            expected = max_dep_layer + 1
            actual = int(node_def.layer[1:]) if node_def.layer.startswith("L") else 0
            if actual != expected:
                warnings.append(
                    f"Layer mismatch: expected L{expected} based on deps, got {node_def.layer}"
                )

        return warnings

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        self.G.remove_node(node_id)
        if node_id in self.gates_G:
            self.gates_G.remove_node(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self.G

    def get_node(self, node_id: str) -> dict:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        return dict(self.G.nodes[node_id])

    def list_nodes(self) -> list[str]:
        return list(self.G.nodes)

    def node_count(self) -> int:
        return len(self.G)

    # --- DAG Queries ---

    def validate_dag(self) -> list[str]:
        errors = []
        if not nx.is_directed_acyclic_graph(self.G):
            try:
                cycles = list(nx.simple_cycles(self.G))
                errors.extend(f"cycle: {c}" for c in cycles[:5])
            except Exception:
                errors.append("cycle detected in deps graph")
        return errors

    def topological_order(self) -> list[str]:
        return list(nx.topological_sort(self.G))

    def ancestors(self, node_id: str) -> set[str]:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        return nx.ancestors(self.G, node_id)

    def descendants(self, node_id: str) -> set[str]:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        return nx.descendants(self.G, node_id)

    def direct_deps(self, node_id: str) -> list[str]:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        return list(self.G.predecessors(node_id))

    def direct_dependents(self, node_id: str) -> list[str]:
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        return list(self.G.successors(node_id))

    def gate_predecessors(self, node_id: str) -> list[str]:
        if node_id not in self.gates_G:
            return []
        return list(self.gates_G.predecessors(node_id))

    def auto_derive_gates(self, node_id: str) -> list[str]:
        """Auto-derive gates: deps where verify_level >= L3."""
        gates = []
        for dep in self.G.predecessors(node_id):
            vl = self.G.nodes[dep].get("verify_level", 1)
            if isinstance(vl, str):
                try:
                    vl = int(vl)
                except ValueError:
                    vl = 1
            if vl >= 3:
                gates.append(dep)
        return gates

    def get_gates(self, node_id: str) -> list[GateRequirement]:
        """Get gate requirements for a node."""
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        raw_gates = self.G.nodes[node_id].get("gates", [])
        return [
            GateRequirement.from_dict(g) if isinstance(g, dict) else g
            for g in raw_gates
        ]

    def affected_nodes_by_files(self, changed_files: list[str], include_secondary: bool = False) -> set[str]:
        """Given file changes, return affected nodes + their descendants."""
        affected = set()
        changed_set = set(changed_files)
        for nid, data in self.G.nodes(data=True):
            primary = set(data.get("primary", []))
            if primary & changed_set:
                affected.add(nid)
            if include_secondary:
                secondary = set(data.get("secondary", []))
                if secondary & changed_set:
                    affected.add(nid)

        # Propagate to descendants
        propagated = set()
        for nid in affected:
            propagated |= self.descendants(nid)
        affected |= propagated
        return affected

    def max_verify_level(self, node_id: str) -> int:
        """Max verify level across node and all its descendants."""
        if node_id not in self.G:
            raise NodeNotFoundError(node_id)
        all_nodes = self.descendants(node_id) | {node_id}
        max_vl = 1
        for nid in all_nodes:
            vl = self.G.nodes[nid].get("verify_level", 1)
            if isinstance(vl, str):
                try:
                    vl = int(vl)
                except ValueError:
                    vl = 1
            max_vl = max(max_vl, vl)
        return max_vl

    # --- Export ---

    def export_mermaid(self, node_statuses: dict = None) -> str:
        """Export as Mermaid graph code."""
        statuses = node_statuses or {}
        colors = {
            "qa_pass": ":::pass",
            "t2_pass": ":::t2pass",
            "failed": ":::fail",
            "pending": ":::pending",
        }
        lines = ["graph TD"]
        for nid in self.topological_order():
            title = self.G.nodes[nid].get("title", nid)
            status = statuses.get(nid, "pending")
            css = colors.get(status, "")
            safe_title = title.replace('"', "'")
            lines.append(f'  {nid}["{nid} {safe_title}"]{css}')
        for u, v in self.G.edges():
            lines.append(f"  {u} --> {v}")
        return "\n".join(lines)
