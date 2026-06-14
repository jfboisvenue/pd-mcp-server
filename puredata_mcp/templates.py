"""
Reusable template patches -- capture a sub-graph once, stamp it many times.

A *template* is a reusable fragment of the patch graph (nodes + edges with
local, 0-based ids) plus a name and description. Unlike a preset -- which is a
bag of runtime *values* pushed at ``[r <name>]`` receivers -- a template is a
*generator of structure*: applying it lays down real objects and connections.

Two ideas make this small:

  * **Pd's creation index is global and append-only.** Instantiating a template
    is just creating its k nodes in order; they land at ids ``base..base+k-1``.
    An internal edge ``i -> j`` therefore remaps to ``base+i -> base+j``. This is
    the same id-remap math the snapshot replay does, minus the ``clear`` -- we
    *append* into the live patch instead of rebuilding it.

  * **Parameterization is ``${token}`` substitution, not a language.** A template
    can carry ``${name}`` placeholders in object args, message atoms, and comment
    text (e.g. ``[r freq_${v}]``, ``[delwrite~ buf_${v} 1000]``). Applying with
    ``params={"v": "1"}`` substitutes them, so multiple instances get unique,
    non-colliding receiver/buffer names. Capture is literal -- tokens ride through
    as ordinary strings; substitution happens only at apply.

This module is pure (no socket, no state): the server calls ``capture`` to build
a template from the live IR, ``substitute`` + ``instantiation_plan`` to compute
what FUDI to emit, and reuses ``builders.atoms_for`` for the actual wire format.

Templates live in a durable per-project ``templates.json`` library; unlike
presets they do NOT ride in the IR (a template is tooling, not patch content),
so the serializer / diff / replay are untouched.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# A placeholder is ``${name}`` where name is a Python-ish identifier. We scan and
# substitute only the string fields the agent authors -- never GUI numeric
# vectors -- so a stray ``$`` in a property list can never be mistaken for one.
_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Per node kind, the param fields that may carry ${tokens} and get substituted.
# Everything else (positions, GUI vectors, py4pd class name) is left untouched.
#   obj     -> args  (list[str])  e.g. ['freq_${v}'] for [r freq_${v}]
#   msg     -> atoms (list[str])
#   comment -> text  (str)
_LIST_FIELDS = {"obj": "args", "msg": "atoms"}
_STR_FIELDS = {"comment": "text"}


def _node_id(node: dict) -> int:
    return node["id"]


def capture(ir: dict, ids: Optional[List[int]] = None) -> Tuple[dict, int]:
    """Build a template fragment from an IR, returning ``(fragment, dropped)``.

    ``fragment`` is ``{"nodes": [...], "edges": [...]}`` with ids renormalized to
    a contiguous ``0..k-1`` (sorted by original id) so it is position-independent
    and stampable anywhere. When ``ids`` is given, only those nodes are captured
    and only edges whose *both* endpoints are inside the set are kept (internal
    wiring); ``dropped`` counts the boundary edges discarded (one endpoint
    outside the selection) so the caller can warn. When ``ids`` is None the whole
    graph is captured and ``dropped`` is 0.

    Raises ``ValueError`` if an id in ``ids`` is not present in the IR.
    """
    all_nodes = {n["id"]: n for n in ir.get("nodes", [])}
    if ids is None:
        selected = sorted(all_nodes)
    else:
        missing = [i for i in ids if i not in all_nodes]
        if missing:
            raise ValueError(f"ids not in patch: {missing}")
        selected = sorted(set(ids))

    remap = {old: new for new, old in enumerate(selected)}
    nodes = []
    for old in selected:
        node = all_nodes[old]
        nodes.append({**{k: v for k, v in node.items() if k != "id"},
                      "id": remap[old]})

    inside = set(selected)
    edges = []
    dropped = 0
    for e in ir.get("edges", []):
        if e["from"] in inside and e["to"] in inside:
            edges.append({"from": remap[e["from"]], "from_outlet": e["from_outlet"],
                          "to": remap[e["to"]], "to_inlet": e["to_inlet"]})
        elif e["from"] in inside or e["to"] in inside:
            dropped += 1
    return {"nodes": nodes, "edges": edges}, dropped


def _iter_strings(node: dict):
    """Yield every substitutable string in a node as ``(getter, setter)`` pairs.

    Hidden behind a tiny accessor so ``substitute``/``required_params`` share one
    definition of *what* is scanned and never drift apart.
    """
    kind = node.get("kind")
    if kind in _LIST_FIELDS:
        field = _LIST_FIELDS[kind]
        values = node.get(field, [])
        for i, v in enumerate(values):
            if isinstance(v, str):
                yield v, ("list", field, i)
    elif kind in _STR_FIELDS:
        field = _STR_FIELDS[kind]
        v = node.get(field)
        if isinstance(v, str):
            yield v, ("str", field, None)


def required_params(template: dict) -> List[str]:
    """Sorted unique ``${token}`` names a template needs at instantiation."""
    found = set()
    for node in template.get("nodes", []):
        for value, _ in _iter_strings(node):
            found.update(_TOKEN.findall(value))
    return sorted(found)


def substitute(template: dict, params: Dict[str, str]) -> dict:
    """Return a copy of ``template`` with every ``${name}`` replaced from ``params``.

    Raises ``ValueError`` listing any ``${token}`` present in the template but
    absent from ``params`` -- a half-substituted template would create dead
    ``${v}`` receivers, so we fail loud instead.
    """
    missing = [p for p in required_params(template) if p not in params]
    if missing:
        raise ValueError(f"missing template params: {missing}")

    def sub(text: str) -> str:
        return _TOKEN.sub(lambda m: str(params[m.group(1)]), text)

    nodes = []
    for node in template.get("nodes", []):
        new = dict(node)
        for kind, field in _LIST_FIELDS.items():
            if new.get("kind") == kind and field in new:
                new[field] = [sub(v) if isinstance(v, str) else v
                              for v in new[field]]
        for kind, field in _STR_FIELDS.items():
            if new.get("kind") == kind and isinstance(new.get(field), str):
                new[field] = sub(new[field])
        nodes.append(new)
    return {"nodes": nodes, "edges": list(template.get("edges", []))}


def instantiation_plan(
    template: dict, base_index: int, dx: int = 0, dy: int = 0,
) -> Tuple[List[dict], List[Tuple[int, int, int, int]], Dict[int, int]]:
    """Compute how to stamp a template into a patch whose next id is ``base_index``.

    Returns ``(nodes, edges, id_map)`` where:
      * ``nodes`` is the ordered list of ``{"kind", **params}`` to create, with
        ``x``/``y`` offset by ``dx``/``dy``; creating them in order yields ids
        ``base_index, base_index+1, ...``.
      * ``edges`` is the internal wiring as ``(src, src_outlet, dst, dst_inlet)``
        tuples already remapped into the patch's id space.
      * ``id_map`` maps each template-local id to its new canvas id, so the caller
        can wire the instance's boundary into the rest of the patch.

    Does NOT substitute tokens -- call ``substitute`` first if the template is
    parameterized.
    """
    ordered = sorted(template.get("nodes", []), key=_node_id)
    id_map = {node["id"]: base_index + i for i, node in enumerate(ordered)}

    nodes = []
    for node in ordered:
        params = {k: v for k, v in node.items() if k not in ("id", "kind")}
        if "x" in params:
            params["x"] = params["x"] + dx
        if "y" in params:
            params["y"] = params["y"] + dy
        nodes.append({"kind": node["kind"], **params})

    edges = []
    for e in template.get("edges", []):
        if e["from"] in id_map and e["to"] in id_map:
            edges.append((id_map[e["from"]], e["from_outlet"],
                          id_map[e["to"]], e["to_inlet"]))
    return nodes, edges, id_map
