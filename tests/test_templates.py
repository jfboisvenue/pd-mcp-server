"""
Tests for template patches -- reusable sub-graphs stamped into a patch.

Two layers:
  * the pure ``templates`` module (capture / required_params / substitute /
    instantiation_plan), golden-locked on ids, offsets, and token handling;
  * the server tools (save/apply/list), the exact FUDI emitted by apply
    against a mock Pd, the durable templates.json round-trip, and the
    missing-param / unknown-template error paths.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp import templates  # noqa: E402
from puredata_mcp.fudi import FudiClient  # noqa: E402
from tests.mock_pd import MockPd  # noqa: E402


# A small two-voice-ready "voice" IR: an [r freq_${v}] -> [osc~] -> [*~] chain,
# with one boundary edge into a shared [dac~] (id 9) that capture must drop.
def _voice_ir():
    return {
        "version": 1, "canvas": {},
        "nodes": [
            {"id": 3, "kind": "obj", "type": "r", "args": ["freq_${v}"], "x": 40, "y": 40},
            {"id": 4, "kind": "obj", "type": "osc~", "args": [], "x": 40, "y": 90},
            {"id": 5, "kind": "obj", "type": "*~", "args": ["0.1"], "x": 40, "y": 140},
            {"id": 9, "kind": "obj", "type": "dac~", "args": [], "x": 200, "y": 300},
        ],
        "edges": [
            {"from": 3, "from_outlet": 0, "to": 4, "to_inlet": 0},
            {"from": 4, "from_outlet": 0, "to": 5, "to_inlet": 0},
            {"from": 5, "from_outlet": 0, "to": 9, "to_inlet": 0},  # boundary
        ],
    }


# --------------------------------------------------------------------------- #
# Pure module: capture
# --------------------------------------------------------------------------- #

def test_capture_whole_graph_renormalizes_ids():
    frag, dropped = templates.capture(_voice_ir())
    assert dropped == 0
    assert [n["id"] for n in frag["nodes"]] == [0, 1, 2, 3]
    # the dac~ edge stays because the whole graph (incl. id 9) is captured
    assert len(frag["edges"]) == 3


def test_capture_subset_keeps_internal_edges_drops_boundary():
    frag, dropped = templates.capture(_voice_ir(), ids=[3, 4, 5])
    assert dropped == 1  # the [*~] -> [dac~] edge crosses the selection boundary
    assert [n["id"] for n in frag["nodes"]] == [0, 1, 2]
    # internal edges remapped to local 0..2
    assert frag["edges"] == [
        {"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0},
        {"from": 1, "from_outlet": 0, "to": 2, "to_inlet": 0},
    ]
    # the captured [r] keeps its literal token, ride-through (no substitution)
    assert frag["nodes"][0]["args"] == ["freq_${v}"]


def test_capture_unknown_id_raises():
    import pytest
    with pytest.raises(ValueError):
        templates.capture(_voice_ir(), ids=[3, 99])


# --------------------------------------------------------------------------- #
# Pure module: tokens
# --------------------------------------------------------------------------- #

def test_required_params_scans_args_atoms_text():
    tmpl = {"nodes": [
        {"id": 0, "kind": "obj", "type": "r", "args": ["freq_${v}"]},
        {"id": 1, "kind": "obj", "type": "delwrite~", "args": ["buf_${v}", "1000"]},
        {"id": 2, "kind": "msg", "atoms": ["set", "${preset}"]},
        {"id": 3, "kind": "comment", "text": "voice ${v} -- ${label}"},
    ], "edges": []}
    assert templates.required_params(tmpl) == ["label", "preset", "v"]


def test_substitute_replaces_in_all_string_fields():
    frag, _ = templates.capture(_voice_ir(), ids=[3, 4, 5])
    out = templates.substitute(frag, {"v": "2"})
    assert out["nodes"][0]["args"] == ["freq_2"]
    # untouched fields survive
    assert out["nodes"][1]["type"] == "osc~"
    assert out["edges"] == frag["edges"]


def test_substitute_missing_param_raises_listing_it():
    import pytest
    frag, _ = templates.capture(_voice_ir(), ids=[3, 4, 5])
    with pytest.raises(ValueError) as exc:
        templates.substitute(frag, {})
    assert "v" in str(exc.value)


# --------------------------------------------------------------------------- #
# Pure module: instantiation_plan
# --------------------------------------------------------------------------- #

def test_instantiation_plan_remaps_ids_and_offsets_positions():
    frag, _ = templates.capture(_voice_ir(), ids=[3, 4, 5])
    frag = templates.substitute(frag, {"v": "1"})
    nodes, edges, id_map = templates.instantiation_plan(frag, base_index=7, dx=200, dy=0)

    assert id_map == {0: 7, 1: 8, 2: 9}
    assert [n["x"] for n in nodes] == [240, 240, 240]  # 40 + 200
    assert [n["y"] for n in nodes] == [40, 90, 140]    # dy = 0
    assert edges == [(7, 0, 8, 0), (8, 0, 9, 0)]
    assert nodes[0]["kind"] == "obj" and nodes[0]["args"] == ["freq_1"]


# --------------------------------------------------------------------------- #
# Server tools
# --------------------------------------------------------------------------- #

def _server_or_skip():
    try:
        from puredata_mcp import server
        return server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")


def _fresh_state(server):
    import importlib
    server._state = importlib.import_module(
        "puredata_mcp.patch_state").PatchState()
    server._state.mark_initialized()
    server._templates.clear()


import asyncio  # noqa: E402


def test_apply_template_emits_create_then_connect_appended():
    server = _server_or_skip()
    _fresh_state(server)
    # Seed a template directly in the library (capture path tested above).
    frag, _ = templates.capture(_voice_ir(), ids=[3, 4, 5])
    server._templates["voice"] = {"description": "one voice", **frag}

    # Pretend the patch already has 7 objects so the instance appends at id 7.
    server._state._next_index = 7

    with MockPd() as pd:
        old_client = server._client
        server._client = FudiClient(port=pd.port)
        try:
            from puredata_mcp.server import ApplyTemplateInput
            res = asyncio.run(server.pd_apply_template(
                ApplyTemplateInput(name="voice", params={"v": "1"}, dx=200, dy=0)))
        finally:
            server._client.close()
            server._client = old_client
        pd.wait_for(5)

    payload = json.loads(res)
    assert payload["status"] == "ok"
    assert payload["id_map"] == {"0": 7, "1": 8, "2": 9}
    assert pd.messages == [
        "obj 240 40 r freq_1",
        "obj 240 90 osc~",
        "obj 240 140 *~ 0.1",
        "connect 7 0 8 0",
        "connect 8 0 9 0",
    ]
    # the instance is appended to the live graph (no clear)
    assert server._state.count() == 3
    assert server._state.next_index() == 10


def test_apply_template_missing_param_errors():
    server = _server_or_skip()
    _fresh_state(server)
    frag, _ = templates.capture(_voice_ir(), ids=[3, 4, 5])
    server._templates["voice"] = {"description": "", **frag}
    from puredata_mcp.server import ApplyTemplateInput
    res = asyncio.run(server.pd_apply_template(
        ApplyTemplateInput(name="voice", params={})))
    assert "Error" in res and "v" in res


def test_apply_unknown_template_lists_known():
    server = _server_or_skip()
    _fresh_state(server)
    server._templates["voice"] = {"description": "", "nodes": [], "edges": []}
    from puredata_mcp.server import ApplyTemplateInput
    res = asyncio.run(server.pd_apply_template(
        ApplyTemplateInput(name="nope")))
    assert "Error" in res and "voice" in res


# --------------------------------------------------------------------------- #
# Global library persistence (one file per template, PD_TEMPLATES_DIR override)
# --------------------------------------------------------------------------- #

def test_template_slug_is_filename_safe():
    server = _server_or_skip()
    assert server._template_slug("my voice!") == "my_voice"
    assert server._template_slug("fm/op") == "fm_op"
    assert server._template_slug("///") == "template"


def test_save_writes_one_file_then_load_round_trips(tmp_path, monkeypatch):
    server = _server_or_skip()
    _fresh_state(server)
    monkeypatch.setenv("PD_TEMPLATES_DIR", str(tmp_path))

    # Build a tiny patch in the live state, then save a subset as a template.
    server._state.add("obj", {"type": "r", "args": ["freq_${v}"], "x": 40, "y": 40})
    server._state.add("obj", {"type": "osc~", "args": [], "x": 40, "y": 90})
    server._state.add_edge(0, 0, 1, 0)

    from puredata_mcp.server import SaveTemplateInput
    res = asyncio.run(server.pd_save_template(
        SaveTemplateInput(name="my voice", description="one osc")))
    payload = json.loads(res)
    assert payload["status"] == "ok"
    assert payload["templates_dir"] == str(tmp_path)

    # one file per template, slugified, with the real name stored inside
    written = tmp_path / "my_voice.json"
    assert written.exists()
    on_disk = json.loads(written.read_text())
    assert on_disk["name"] == "my voice"
    assert on_disk["nodes"][0]["args"] == ["freq_${v}"]

    # a fresh process (cleared memory) re-discovers it by globbing the dir
    server._templates.clear()
    n = server._load_templates_from_disk()
    assert n == 1
    assert "my voice" in server._templates
    assert server._templates["my voice"]["description"] == "one osc"


def test_pd_user_dir_parses_pdsettings(tmp_path, monkeypatch):
    server = _server_or_skip()
    fake_home = tmp_path
    (fake_home / ".pdsettings").write_text(
        "audioapi: 1\n"
        "path1: /home/u/Documents/Pd/externals\n"
        "path2: /home/u/Documents/Pd/externals/py4pd\n"
        "npath: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.delenv("PD_TEMPLATES_DIR", raising=False)
    # the Pd folder is the ancestor named "Pd"; templates land beside externals
    assert server._pd_user_dir() == server.Path("/home/u/Documents/Pd")
    assert server._resolve_templates_dir() == server.Path("/home/u/Documents/Pd/templates")
