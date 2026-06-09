"""
Authoritative in-memory model (IR) of the Pd canvas.

This server treats its own model as the source of truth: the Pd canvas is a
render target, never read back. Every object and connection created through
the server is recorded here as a structured node/edge so the whole patch can
be serialized to JSON, versioned, and re-rendered (clear + replay) at will.

Object ids are Pd's *creation index* (0-based, in order of creation). The
``connect`` / ``disconnect`` editing messages reference those indices. We
mirror Pd's indexing by counting create calls; that counter value IS the id
we hand back. This stays in sync with Pd as long as we only ever *append* or
*clear* -- which matches Pd vanilla (no "delete single object" message). If a
user hand-edits the canvas, ``resync_to`` realigns the counter.

Unlike the earlier label-only mirror, a node stores the *structured params*
the creating tool received (``kind`` + ``params``), which is exactly what the
FUDI builders in ``builders.py`` consume -- so the model is replayable, not
just a human summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

IR_VERSION = 1
DEFAULT_CANVAS = {"width": 800, "height": 600}


@dataclass
class PdObject:
    """A node in the patch graph.

    ``params`` is the builder kwargs dict (e.g. ``{"type": "osc~",
    "args": ["440"], "x": 40, "y": 40}``) -- both the replay input and the
    IR payload. ``kind`` selects the FUDI builder.
    """
    index: int
    kind: str          # obj|msg|comment|floatatom|bng|tgl|nbx|hsl|vsl|py4pd
    params: dict = field(default_factory=dict)


@dataclass
class Edge:
    """A connection: source outlet -> target inlet, by creation id."""
    src: int
    src_outlet: int
    dst: int
    dst_inlet: int

    def as_tuple(self) -> tuple:
        return (self.src, self.src_outlet, self.dst, self.dst_inlet)


def _summarize(kind: str, params: dict) -> str:
    """Human-readable one-liner for pd_get_state, derived from params."""
    if kind == "obj":
        return " ".join([params.get("type", ""), *params.get("args", [])]).strip()
    if kind == "msg":
        return " ".join(params.get("atoms", []))
    if kind == "comment":
        return params.get("text", "")
    if kind == "py4pd":
        return params.get("name", "")
    # GUIs: compact key=value of the non-position params.
    fields = ", ".join(
        f"{k}={v}" for k, v in params.items() if k not in ("x", "y")
    )
    return f"{kind} {fields}".strip()


@dataclass
class PatchState:
    """Authoritative graph: nodes + edges, with stable creation ids."""

    _next_index: int = 0
    objects: Dict[int, PdObject] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    # Named parameter presets: name -> {receiver: [atoms]}. A preset is a
    # bag of values to push at named [r <name>] receivers -- recalling it is
    # pure pd_send_message, never a re-render. Rides in the IR, so snapshots
    # version it alongside the graph.
    presets: Dict[str, Dict[str, list]] = field(default_factory=dict)
    initialized: bool = False
    # Optional observer fired after every graph mutation. The server wires this
    # to autosave the IR to disk when a project is bound; left None it is a
    # no-op, so the model stays a pure data structure for tests.
    on_change: Optional[Callable[[], None]] = field(
        default=None, repr=False, compare=False)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # -- init gate ------------------------------------------------------------

    def mark_initialized(self) -> None:
        """Record that the agent has read the init guide. Other tools gate on this."""
        self.initialized = True

    # -- nodes ----------------------------------------------------------------

    def add(self, kind: str, params: dict) -> int:
        """Register a newly created node and return its Pd index/id."""
        idx = self._next_index
        self.objects[idx] = PdObject(index=idx, kind=kind, params=dict(params))
        self._next_index += 1
        self._changed()
        return idx

    # -- edges ----------------------------------------------------------------

    def add_edge(self, src: int, src_outlet: int, dst: int, dst_inlet: int) -> None:
        """Record a connection (idempotent: no duplicate edges)."""
        edge = Edge(src, src_outlet, dst, dst_inlet)
        if edge.as_tuple() not in {e.as_tuple() for e in self.edges}:
            self.edges.append(edge)
            self._changed()

    def remove_edge(self, src: int, src_outlet: int, dst: int, dst_inlet: int) -> None:
        """Forget a connection (idempotent: missing edge is a no-op)."""
        target = (src, src_outlet, dst, dst_inlet)
        before = len(self.edges)
        self.edges = [e for e in self.edges if e.as_tuple() != target]
        if len(self.edges) != before:
            self._changed()

    # -- presets --------------------------------------------------------------

    def set_preset(self, name: str, params: Dict[str, list]) -> None:
        """Store (or overwrite) a named parameter preset: receiver -> atoms."""
        self.presets[name] = {recv: list(atoms) for recv, atoms in params.items()}
        self._changed()

    def get_preset(self, name: str) -> Dict[str, list]:
        """Return a copy of a preset's receiver->atoms map (KeyError if absent)."""
        return {recv: list(atoms) for recv, atoms in self.presets[name].items()}

    def preset_names(self) -> List[str]:
        return sorted(self.presets)

    def preset_count(self) -> int:
        return len(self.presets)

    # -- lifecycle ------------------------------------------------------------

    def clear(self) -> None:
        """Mirror a Pd ``clear``: drop everything and reset indexing.

        Presets are part of this patch's state and target receivers in this
        graph, so a blank canvas drops them too.
        """
        self.objects.clear()
        self.edges.clear()
        self.presets.clear()
        self._next_index = 0
        self._changed()

    def resync_to(self, next_index: int) -> None:
        """Realign the counter after the user hand-edits the canvas.

        ``next_index`` is the id Pd will assign to the *next* object created.
        The node list and edges are dropped because we can no longer trust
        prior records to match Pd's actual canvas.
        """
        if next_index < 0:
            raise ValueError("next_index must be >= 0")
        self.objects.clear()
        self.edges.clear()
        self._next_index = next_index
        self._changed()

    # -- IR serialization -----------------------------------------------------

    def to_ir(self) -> dict:
        """Serialize the graph to the JSON-able IR dict."""
        nodes = [
            {"id": o.index, "kind": o.kind, **o.params}
            for o in sorted(self.objects.values(), key=lambda x: x.index)
        ]
        edges = [
            {"from": e.src, "from_outlet": e.src_outlet,
             "to": e.dst, "to_inlet": e.dst_inlet}
            for e in self.edges
        ]
        return {
            "version": IR_VERSION,
            "canvas": dict(DEFAULT_CANVAS),
            "nodes": nodes,
            "edges": edges,
            "presets": {name: {recv: list(atoms) for recv, atoms in m.items()}
                        for name, m in self.presets.items()},
        }

    def load_ir(self, ir: dict) -> None:
        """Replace the in-memory graph with a restored IR dict.

        Ids are taken verbatim from the IR (the caller -- replay -- is
        responsible for having rendered the canvas to match).
        """
        self.objects.clear()
        self.edges.clear()
        self.presets = {name: {recv: list(atoms) for recv, atoms in m.items()}
                        for name, m in ir.get("presets", {}).items()}
        max_id = -1
        for node in ir.get("nodes", []):
            params = {k: v for k, v in node.items() if k not in ("id", "kind")}
            idx = node["id"]
            self.objects[idx] = PdObject(index=idx, kind=node["kind"], params=params)
            max_id = max(max_id, idx)
        for edge in ir.get("edges", []):
            self.edges.append(Edge(edge["from"], edge["from_outlet"],
                                   edge["to"], edge["to_inlet"]))
        self._next_index = max_id + 1
        self._changed()

    # -- queries --------------------------------------------------------------

    def next_index(self) -> int:
        return self._next_index

    def exists(self, index: int) -> bool:
        return index in self.objects

    def count(self) -> int:
        return len(self.objects)

    def edge_count(self) -> int:
        return len(self.edges)

    def as_list(self) -> List[dict]:
        """Node list with a human summary, for pd_get_state."""
        return [
            {"id": o.index, "kind": o.kind, "text": _summarize(o.kind, o.params)}
            for o in sorted(self.objects.values(), key=lambda x: x.index)
        ]
