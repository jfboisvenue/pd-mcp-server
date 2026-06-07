"""
Golden tests for the FUDI atom builders.

These lock the exact atom vectors each builder emits. They are a regression
guard for the extraction of the wire format out of server.py: if a single
atom changes, Pd would render a different object, so these must fail loudly.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp import builders  # noqa: E402


def test_obj_atoms():
    assert builders.obj_atoms("osc~", ["440"], 100, 80) == \
        ["obj", 100, 80, "osc~", "440"]
    assert builders.obj_atoms("dac~", [], 40, 140) == ["obj", 40, 140, "dac~"]


def test_msg_atoms():
    assert builders.msg_atoms(["set", "3.5"], 10, 20) == \
        ["msg", 10, 20, "set", "3.5"]


def test_comment_atoms():
    assert builders.comment_atoms("hello world", 50, 50) == \
        ["text", 50, 50, "hello world"]


def test_floatatom_atoms():
    assert builders.floatatom_atoms(5, 0.0, 0.0, 50, 50) == \
        ["floatatom", 50, 50, 5, 0.0, 0.0, 0, "-", "-", "-"]


def test_bng_atoms():
    assert builders.bng_atoms(15, 40, 40) == [
        "obj", 40, 40, "bng",
        15, 250, 50, 0, "empty", "empty", "empty",
        17, 7, 0, 10, "#fcfcfc", "#000000", "#000000",
    ]


def test_tgl_atoms_initial_on_and_off():
    on = builders.tgl_atoms(15, True, 40, 40)
    off = builders.tgl_atoms(15, False, 40, 40)
    assert on == [
        "obj", 40, 40, "tgl",
        15, 0, "empty", "empty", "empty",
        17, 7, 0, 10, "#fcfcfc", "#000000", "#000000", 1, 1,
    ]
    # Only the INIT_VAL field (second from last) differs.
    assert off[-2] == 0 and on[-2] == 1


def test_nbx_atoms():
    assert builders.nbx_atoms(5, -1e37, 1e37, 40, 40) == [
        "obj", 40, 40, "nbx",
        5, 14, -1e37, 1e37, 0, 0, "empty", "empty", "empty",
        0, -8, 0, 10, "#fcfcfc", "#000000", "#000000", 0, 256,
    ]


def test_slider_atoms_horizontal_and_vertical():
    h = builders.slider_atoms("horizontal", 0.0, 127.0, 40, 40)
    v = builders.slider_atoms("vertical", 0.0, 127.0, 40, 40)
    assert h[:6] == ["obj", 40, 40, "hsl", 128, 15]
    assert v[:6] == ["obj", 40, 40, "vsl", 15, 128]
    # Shared tail after the WIDTH/HEIGHT pair.
    assert h[6:] == [0.0, 127.0, 0, 0, "empty", "empty", "empty",
                     0, -8, 0, 10, "#fcfcfc", "#000000", "#000000", 0, 1]


def test_py4pd_and_connect_atoms():
    assert builders.py4pd_atoms("doubler", 40, 40) == ["obj", 40, 40, "doubler"]
    assert builders.connect_atoms(1, 0, 2, 1) == ["connect", 1, 0, 2, 1]


def test_atoms_for_dispatch_matches_direct_builder():
    assert builders.atoms_for("obj", {"type": "osc~", "args": ["440"],
                                      "x": 10, "y": 20}) == \
        builders.obj_atoms("osc~", ["440"], 10, 20)
    # Sliders dispatch by kind but recover orientation from params.
    assert builders.atoms_for("vsl", {"orientation": "vertical", "min": 0.0,
                                      "max": 127.0, "x": 1, "y": 2}) == \
        builders.slider_atoms("vertical", 0.0, 127.0, 1, 2)


def test_atoms_for_unknown_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        builders.atoms_for("nope", {})
