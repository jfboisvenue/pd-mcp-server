"""
Tests for the graph-level IR diff. Pure functions: no Pd, no git. One test
exercises the read_ir_at -> diff_ir path over a real tmp git repo.
"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from puredata_mcp import pd_diff, versioning  # noqa: E402


def _node(i, type, args=None, x=40, y=40):
    return {"id": i, "kind": "obj", "type": type, "args": args or [], "x": x, "y": y}


def _ir(nodes, edges=None):
    return {"version": 1, "canvas": {"width": 800, "height": 600},
            "nodes": nodes, "edges": edges or []}


def test_identical_irs_have_empty_diff():
    ir = _ir([_node(0, "osc~", ["440"]), _node(1, "dac~")])
    d = pd_diff.diff_ir(ir, ir)
    assert pd_diff.is_empty(d)
    assert pd_diff.format_diff(d) == "No differences."


def test_added_and_removed_nodes():
    old = _ir([_node(0, "osc~", ["440"])])
    new = _ir([_node(0, "osc~", ["440"]), _node(1, "dac~")])
    d = pd_diff.diff_ir(old, new)
    assert d["counts"]["added_nodes"] == 1 and d["counts"]["removed_nodes"] == 0
    assert d["added_nodes"][0]["id"] == 1
    # reverse direction
    d2 = pd_diff.diff_ir(new, old)
    assert d2["counts"]["removed_nodes"] == 1
    assert d2["removed_nodes"][0]["summary"] == "dac~"


def test_changed_args():
    old = _ir([_node(0, "osc~", ["440"])])
    new = _ir([_node(0, "osc~", ["880"])])
    d = pd_diff.diff_ir(old, new)
    assert d["changed_nodes"][0]["changes"] == {"args": [["440"], ["880"]]}


def test_moved_only():
    old = _ir([_node(0, "osc~", ["440"], x=40, y=40)])
    new = _ir([_node(0, "osc~", ["440"], x=60, y=40)])
    d = pd_diff.diff_ir(old, new)
    assert d["changed_nodes"][0]["changes"] == {"moved": [[40, 40], [60, 40]]}


def test_kind_change():
    old = _ir([_node(0, "osc~", ["440"])])
    new = _ir([{"id": 0, "kind": "msg", "atoms": ["440"], "x": 40, "y": 40}])
    d = pd_diff.diff_ir(old, new)
    assert d["changed_nodes"][0]["changes"]["kind"] == ["obj", "msg"]


def test_added_and_removed_edges():
    old = _ir([_node(0, "osc~"), _node(1, "dac~")],
              [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0}])
    new = _ir([_node(0, "osc~"), _node(1, "dac~")],
              [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 1}])
    d = pd_diff.diff_ir(old, new)
    assert d["counts"]["added_edges"] == 1 and d["counts"]["removed_edges"] == 1
    assert d["added_edges"][0] == {"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 1}


def test_format_diff_text():
    old = _ir([_node(0, "osc~", ["440"]), _node(2, "lop~", ["1000"])])
    new = _ir([_node(0, "osc~", ["880"]), _node(1, "dac~")],
              [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0}])
    text = pd_diff.format_diff(pd_diff.diff_ir(old, new))
    assert "+ node 1 [dac~]" in text
    assert "- node 2 [lop~ 1000]" in text
    assert "~ node 0 [osc~ 880]: args [440] -> [880]" not in text  # list repr differs
    assert "~ node 0 [osc~ 880]:" in text and "args" in text
    assert "+ edge 0:0 -> 1:0" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_diff_two_saved_checkpoints(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir([_node(0, "osc~", ["440"]), _node(1, "dac~")]), "a")
    versioning.save(
        d,
        _ir([_node(0, "osc~", ["880"]), _node(1, "dac~")],
            [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0}]),
        "b",
    )
    old = versioning.read_ir_at(d, "a")
    new = versioning.read_ir_at(d, "b")
    diff = pd_diff.diff_ir(old, new)
    assert diff["counts"]["changed_nodes"] == 1   # osc~ args 440 -> 880
    assert diff["counts"]["added_edges"] == 1
