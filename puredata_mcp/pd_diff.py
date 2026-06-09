"""
Semantic diff between two IR snapshots.

Compares two patches at the graph level -- "node osc~ 880 added", "edge
1:0 -> 2:1 removed", "node 3: args [440] -> [880]" -- instead of the
coordinate line-noise a raw JSON/text diff produces. Ideal for seeing what
distinguishes two A/B sound variants on different branches.

Nodes are matched by creation id (deterministic; correct for the append +
branch-from-common-base workflow). A node recreated elsewhere shows up as
removed+added rather than changed. Edges are compared as sets of tuples,
consistent with id-based node matching.

Pure functions: no Pd, no git. The server tool loads the two IRs (via
versioning) and feeds them here.
"""

from __future__ import annotations

from typing import Dict, List

from .patch_state import _summarize

# Keys in a node dict that are position rather than semantic identity.
_POSITION_KEYS = ("x", "y")


def _nodes_by_id(ir: dict) -> Dict[int, dict]:
    return {n["id"]: n for n in ir.get("nodes", [])}


def _params(node: dict) -> dict:
    return {k: v for k, v in node.items() if k not in ("id", "kind")}


def _edge_tuples(ir: dict) -> set:
    return {
        (e["from"], e["from_outlet"], e["to"], e["to_inlet"])
        for e in ir.get("edges", [])
    }


def _node_entry(node: dict) -> dict:
    return {"id": node["id"], "kind": node["kind"],
            "summary": _summarize(node["kind"], _params(node))}


def _node_changes(old: dict, new: dict) -> dict:
    """Per-field changes between two nodes with the same id.

    Returns a dict possibly containing:
      - "kind": [old, new]
      - "moved": [[ox, oy], [nx, ny]]   (only x/y changed)
      - "<param>": [old, new]           (semantic params: type/args/props/...)
    Empty dict means the nodes are identical.
    """
    changes: dict = {}
    if old["kind"] != new["kind"]:
        changes["kind"] = [old["kind"], new["kind"]]

    op, np_ = _params(old), _params(new)

    # Position grouped as a single "moved" entry.
    if any(op.get(k) != np_.get(k) for k in _POSITION_KEYS):
        changes["moved"] = [[op.get("x"), op.get("y")], [np_.get("x"), np_.get("y")]]

    # Semantic params (everything except position).
    for key in sorted(set(op) | set(np_)):
        if key in _POSITION_KEYS:
            continue
        if op.get(key) != np_.get(key):
            changes[key] = [op.get(key), np_.get(key)]
    return changes


def diff_ir(old: dict, new: dict) -> dict:
    """Compute a graph-level diff from `old` to `new`.

    "added" = present in `new`, absent from `old`. Symmetric for "removed".
    """
    old_nodes, new_nodes = _nodes_by_id(old), _nodes_by_id(new)
    old_ids, new_ids = set(old_nodes), set(new_nodes)

    added_nodes = [_node_entry(new_nodes[i]) for i in sorted(new_ids - old_ids)]
    removed_nodes = [_node_entry(old_nodes[i]) for i in sorted(old_ids - new_ids)]

    changed_nodes: List[dict] = []
    for i in sorted(old_ids & new_ids):
        ch = _node_changes(old_nodes[i], new_nodes[i])
        if ch:
            changed_nodes.append({
                "id": i,
                "kind": new_nodes[i]["kind"],
                "summary": _summarize(new_nodes[i]["kind"], _params(new_nodes[i])),
                "changes": ch,
            })

    old_edges, new_edges = _edge_tuples(old), _edge_tuples(new)
    added_edges = [_edge_dict(t) for t in sorted(new_edges - old_edges)]
    removed_edges = [_edge_dict(t) for t in sorted(old_edges - new_edges)]

    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "changed_nodes": changed_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "counts": {
            "added_nodes": len(added_nodes),
            "removed_nodes": len(removed_nodes),
            "changed_nodes": len(changed_nodes),
            "added_edges": len(added_edges),
            "removed_edges": len(removed_edges),
        },
    }


def _edge_dict(t: tuple) -> dict:
    return {"from": t[0], "from_outlet": t[1], "to": t[2], "to_inlet": t[3]}


def is_empty(diff: dict) -> bool:
    return not any(diff["counts"].values())


def _fmt_change(key: str, pair) -> str:
    if key == "moved":
        (ox, oy), (nx, ny) = pair
        return f"moved ({ox},{oy}) -> ({nx},{ny})"
    return f"{key} {pair[0]} -> {pair[1]}"


def _fmt_edge(e: dict) -> str:
    return f"{e['from']}:{e['from_outlet']} -> {e['to']}:{e['to_inlet']}"


def format_diff(diff: dict) -> str:
    """Render a diff dict to a human-readable, music-oriented summary."""
    if is_empty(diff):
        return "No differences."

    c = diff["counts"]
    lines = [
        f"{c['added_nodes']} added, {c['removed_nodes']} removed, "
        f"{c['changed_nodes']} changed nodes; "
        f"{c['added_edges']} added, {c['removed_edges']} removed edges.",
    ]
    for n in diff["added_nodes"]:
        lines.append(f"+ node {n['id']} [{n['summary']}]")
    for n in diff["removed_nodes"]:
        lines.append(f"- node {n['id']} [{n['summary']}]")
    for n in diff["changed_nodes"]:
        detail = "; ".join(_fmt_change(k, v) for k, v in n["changes"].items())
        lines.append(f"~ node {n['id']} [{n['summary']}]: {detail}")
    for e in diff["added_edges"]:
        lines.append(f"+ edge {_fmt_edge(e)}")
    for e in diff["removed_edges"]:
        lines.append(f"- edge {_fmt_edge(e)}")
    return "\n".join(lines)
