"""
Tests for IR -> .pd serialization.

Golden lines per object kind, the canvas header, connect lines, id
recompaction, and escaping. A separate test actually loads the generated
.pd in a headless Pd to prove the format is accepted (skipped if pd is not
on PATH).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from puredata_mcp import pd_serialize  # noqa: E402


def _ir(nodes, edges=None):
    return {"version": 1, "canvas": {"width": 800, "height": 600},
            "nodes": nodes, "edges": edges or []}


def _lines(ir):
    return pd_serialize.ir_to_pd(ir).splitlines()


# -- header ------------------------------------------------------------------ #

def test_canvas_header():
    assert _lines(_ir([]))[0] == "#N canvas 0 0 800 600 12;"


# -- per-kind golden lines --------------------------------------------------- #

def test_obj_and_connect_lines():
    ir = _ir(
        [{"id": 0, "kind": "obj", "type": "osc~", "args": ["440"], "x": 100, "y": 80},
         {"id": 1, "kind": "obj", "type": "dac~", "args": [], "x": 100, "y": 160}],
        [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0}],
    )
    lines = _lines(ir)
    assert lines[1] == "#X obj 100 80 osc~ 440;"
    assert lines[2] == "#X obj 100 160 dac~;"
    assert lines[3] == "#X connect 0 0 1 0;"


def test_msg_line():
    ir = _ir([{"id": 0, "kind": "msg", "atoms": ["set", "3.5"], "x": 10, "y": 20}])
    assert _lines(ir)[1] == "#X msg 10 20 set 3.5;"


def test_comment_keeps_spaces():
    ir = _ir([{"id": 0, "kind": "comment", "text": "hello world", "x": 50, "y": 50}])
    assert _lines(ir)[1] == "#X text 50 50 hello world;"


def test_floatatom_line():
    ir = _ir([{"id": 0, "kind": "floatatom", "width": 5, "min": 0.0, "max": 0.0,
               "x": 50, "y": 50}])
    assert _lines(ir)[1] == "#X floatatom 50 50 5 0.0 0.0 0 - - -;"


def test_gui_lines():
    ir = _ir([
        {"id": 0, "kind": "tgl", "size": 15, "initial": True, "x": 40, "y": 40},
        {"id": 1, "kind": "hsl", "orientation": "horizontal", "min": 0.0,
         "max": 127.0, "x": 40, "y": 90},
        {"id": 2, "kind": "bng", "size": 15, "x": 40, "y": 140},
    ])
    lines = _lines(ir)
    assert lines[1].startswith("#X obj 40 40 tgl 15 0 empty empty empty")
    assert lines[1].endswith(" 1 1;")           # initial=True -> INIT_VAL 1
    assert lines[2].startswith("#X obj 40 90 hsl 128 15 0.0 127.0")
    assert lines[3].startswith("#X obj 40 140 bng 15 250 50")


def test_py4pd_line():
    ir = _ir([{"id": 0, "kind": "py4pd", "name": "doubler", "x": 40, "y": 40}])
    assert _lines(ir)[1] == "#X obj 40 40 doubler;"


# -- recompaction & escaping ------------------------------------------------- #

def test_id_holes_are_recompacted_and_edges_remapped():
    ir = _ir(
        [{"id": 0, "kind": "obj", "type": "osc~", "args": [], "x": 0, "y": 0},
         {"id": 5, "kind": "obj", "type": "dac~", "args": [], "x": 0, "y": 50}],
        [{"from": 0, "from_outlet": 0, "to": 5, "to_inlet": 0}],
    )
    # id 5 becomes index 1 in the file.
    assert _lines(ir)[-1] == "#X connect 0 0 1 0;"


def test_escaping_in_message_atoms():
    ir = _ir([{"id": 0, "kind": "msg", "atoms": ["a,b", "c;d", "e$1"],
               "x": 0, "y": 0}])
    assert _lines(ir)[1] == r"#X msg 0 0 a\,b c\;d e\$1;"


# -- headless Pd load -------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("pd") is None, reason="pd not installed")
def test_generated_pd_loads_in_headless_pd(tmp_path):
    ir = _ir(
        [{"id": 0, "kind": "obj", "type": "osc~", "args": ["440"], "x": 40, "y": 40},
         {"id": 1, "kind": "obj", "type": "*~", "args": ["0.1"], "x": 40, "y": 90},
         {"id": 2, "kind": "obj", "type": "dac~", "args": [], "x": 40, "y": 140},
         {"id": 3, "kind": "tgl", "size": 15, "initial": False, "x": 200, "y": 40},
         {"id": 4, "kind": "hsl", "orientation": "horizontal", "min": 0.0,
          "max": 127.0, "x": 200, "y": 90},
         {"id": 5, "kind": "nbx", "width": 5, "min": -1e37, "max": 1e37,
          "x": 200, "y": 140},
         {"id": 6, "kind": "floatatom", "width": 5, "min": 0.0, "max": 0.0,
          "x": 200, "y": 190},
         {"id": 7, "kind": "msg", "atoms": ["1"], "x": 200, "y": 240},
         {"id": 8, "kind": "comment", "text": "a sine to dac", "x": 40, "y": 200}],
        [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0},
         {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 0},
         {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 1}],
    )
    pd_file = tmp_path / "out.pd"
    pd_file.write_text(pd_serialize.ir_to_pd(ir), encoding="utf-8")

    proc = subprocess.run(
        ["pd", "-nogui", "-noaudio", "-stderr",
         "-open", str(pd_file), "-send", "; pd quit"],
        capture_output=True, text=True, timeout=20,
    )
    combined = proc.stdout + proc.stderr
    assert "couldn't create" not in combined.lower(), combined
