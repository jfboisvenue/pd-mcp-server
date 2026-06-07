"""
Serialize the authoritative IR to a standalone Pure Data ``.pd`` file.

We serialize TO ``.pd`` only -- never parse FROM it. The flat text format is
a gift on write, a trap on read; we exploit only the favorable direction.

Why this is short: Pd parses dynamic-patching messages (``obj``, ``msg``,
``floatatom``, ``text``) with the SAME code that reads ``.pd`` file records.
So the body of a file record equals the atoms ``builders.py`` already emits
for the wire. Serializing a node is therefore: ``#X`` + the builder atoms
(file-escaped) + ``;``. The only file-specific logic here is escaping and the
canvas header.

A ``.pd`` file looks like::

    #N canvas 0 0 800 600 12;
    #X obj 100 80 osc~ 440;
    #X obj 100 160 *~ 0.1;
    #X obj 100 240 dac~;
    #X connect 0 0 1 0;
    #X connect 1 0 2 0;
"""

from __future__ import annotations

from typing import Dict, List

from . import builders

# Chars that must be backslash-escaped inside a .pd file atom. Order matters:
# backslash first, so we don't double-escape the escapes we just added.
_PD_SPECIAL = ("\\", ";", ",", "$")

DEFAULT_W = 800
DEFAULT_H = 600
FONT_SIZE = 12


def _pd_escape(atom: str) -> str:
    """Escape one atom for the .pd file format.

    Spaces are preserved on purpose: a comment is a single text atom whose
    spaces are literal in the file (``#X text x y hello world;``). No other
    generated atom contains a space.
    """
    out = str(atom)
    for ch in _PD_SPECIAL:
        out = out.replace(ch, "\\" + ch)
    return out


def _canvas_header(canvas: dict) -> str:
    w = canvas.get("width", DEFAULT_W)
    h = canvas.get("height", DEFAULT_H)
    return f"#N canvas 0 0 {w} {h} {FONT_SIZE};"


def _node_line(kind: str, params: dict) -> str:
    atoms = builders.atoms_for(kind, params)
    return "#X " + " ".join(_pd_escape(a) for a in atoms) + ";"


def ir_to_pd(ir: dict) -> str:
    """Render an IR dict to the text of a standalone ``.pd`` file.

    Node ids are recompacted to 0..n-1 in id order (same as the canvas
    replay) so the file's positional indexing matches the ``#X connect``
    lines; edges are remapped through that compaction.
    """
    nodes = sorted(ir.get("nodes", []), key=lambda n: n["id"])
    id_map: Dict[int, int] = {node["id"]: new for new, node in enumerate(nodes)}

    lines: List[str] = [_canvas_header(ir.get("canvas", {}))]
    for node in nodes:
        params = {k: v for k, v in node.items() if k not in ("id", "kind")}
        lines.append(_node_line(node["kind"], params))
    for e in ir.get("edges", []):
        if e["from"] in id_map and e["to"] in id_map:
            lines.append(
                f"#X connect {id_map[e['from']]} {e['from_outlet']} "
                f"{id_map[e['to']]} {e['to_inlet']};"
            )
    return "\n".join(lines) + "\n"
