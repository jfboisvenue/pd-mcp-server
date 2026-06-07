"""
Tests for the authoritative IR model: structured nodes, edge tracking, and
the to_ir / load_ir round-trip used by snapshot/restore.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp.patch_state import PatchState  # noqa: E402


def _build_sine() -> PatchState:
    s = PatchState()
    s.add("obj", {"type": "osc~", "args": ["440"], "x": 40, "y": 40})
    s.add("obj", {"type": "*~", "args": ["0.1"], "x": 40, "y": 90})
    s.add("obj", {"type": "dac~", "args": [], "x": 40, "y": 140})
    s.add_edge(0, 0, 1, 0)
    s.add_edge(1, 0, 2, 0)
    s.add_edge(1, 0, 2, 1)
    return s


def test_to_ir_shape():
    ir = _build_sine().to_ir()
    assert ir["version"] == 1
    assert ir["nodes"][0] == {"id": 0, "kind": "obj", "type": "osc~",
                              "args": ["440"], "x": 40, "y": 40}
    assert {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 1} in ir["edges"]
    assert len(ir["nodes"]) == 3 and len(ir["edges"]) == 3


def test_add_edge_is_idempotent():
    s = PatchState()
    s.add_edge(0, 0, 1, 0)
    s.add_edge(0, 0, 1, 0)
    assert s.edge_count() == 1


def test_remove_edge():
    s = _build_sine()
    s.remove_edge(1, 0, 2, 1)
    assert s.edge_count() == 2
    s.remove_edge(9, 9, 9, 9)  # missing edge -> no-op
    assert s.edge_count() == 2


def test_to_ir_load_ir_round_trip():
    original = _build_sine().to_ir()
    restored = PatchState()
    restored.load_ir(original)
    assert restored.to_ir() == original
    assert restored.count() == 3
    assert restored.edge_count() == 3
    assert restored.next_index() == 3  # next id after the highest loaded id


def test_load_ir_with_id_holes_sets_next_index_past_max():
    ir = {
        "version": 1, "canvas": {},
        "nodes": [{"id": 0, "kind": "obj", "type": "osc~", "args": [], "x": 0, "y": 0},
                  {"id": 5, "kind": "obj", "type": "dac~", "args": [], "x": 0, "y": 0}],
        "edges": [{"from": 0, "from_outlet": 0, "to": 5, "to_inlet": 0}],
    }
    s = PatchState()
    s.load_ir(ir)
    assert s.next_index() == 6
    assert s.exists(0) and s.exists(5)


def test_clear_and_resync_drop_edges():
    s = _build_sine()
    s.clear()
    assert s.count() == 0 and s.edge_count() == 0
    s2 = _build_sine()
    s2.resync_to(10)
    assert s2.count() == 0 and s2.edge_count() == 0 and s2.next_index() == 10
