"""
Tests for IR replay (the engine behind pd_restore): clear + recreate nodes
in id order + remap edges. Asserts the exact FUDI sequence reaching a mock
Pd, including id recompaction when the IR has holes.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp.fudi import FudiClient  # noqa: E402
from tests.mock_pd import MockPd  # noqa: E402


def _server_or_skip():
    try:
        from puredata_mcp import server
        return server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")


def test_replay_emits_clear_creates_then_connects():
    server = _server_or_skip()
    ir = {
        "version": 1, "canvas": {},
        "nodes": [
            {"id": 0, "kind": "obj", "type": "osc~", "args": ["440"], "x": 40, "y": 40},
            {"id": 1, "kind": "obj", "type": "*~", "args": ["0.1"], "x": 40, "y": 90},
            {"id": 2, "kind": "obj", "type": "dac~", "args": [], "x": 40, "y": 140},
        ],
        "edges": [
            {"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0},
            {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 0},
            {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 1},
        ],
    }
    with MockPd() as pd:
        import importlib
        server._state = importlib.import_module(
            "puredata_mcp.patch_state").PatchState()
        old_client = server._client
        server._client = FudiClient(port=pd.port)
        try:
            n_nodes, n_edges = server._replay(ir)
        finally:
            server._client.close()
            server._client = old_client
        pd.wait_for(7)

    assert (n_nodes, n_edges) == (3, 3)
    assert pd.messages == [
        "clear",
        "obj 40 40 osc~ 440",
        "obj 40 90 *~ 0.1",
        "obj 40 140 dac~",
        "connect 0 0 1 0",
        "connect 1 0 2 0",
        "connect 1 0 2 1",
    ]
    # State now reflects the rendered canvas.
    assert server._state.count() == 3 and server._state.edge_count() == 3


def test_replay_recompacts_id_holes_and_remaps_edges():
    server = _server_or_skip()
    # Ids 0 and 5 (a hole): replay should renumber to 0 and 1 and remap the edge.
    ir = {
        "version": 1, "canvas": {},
        "nodes": [
            {"id": 0, "kind": "obj", "type": "osc~", "args": ["440"], "x": 0, "y": 0},
            {"id": 5, "kind": "obj", "type": "dac~", "args": [], "x": 0, "y": 50},
        ],
        "edges": [{"from": 0, "from_outlet": 0, "to": 5, "to_inlet": 0}],
    }
    with MockPd() as pd:
        import importlib
        server._state = importlib.import_module(
            "puredata_mcp.patch_state").PatchState()
        old_client = server._client
        server._client = FudiClient(port=pd.port)
        try:
            server._replay(ir)
        finally:
            server._client.close()
            server._client = old_client
        pd.wait_for(4)

    assert pd.messages == [
        "clear",
        "obj 0 0 osc~ 440",
        "obj 0 50 dac~",
        "connect 0 0 1 0",   # 5 remapped to 1
    ]
    # State carries the compacted ids.
    ids = [n["id"] for n in server._state.to_ir()["nodes"]]
    assert ids == [0, 1]
