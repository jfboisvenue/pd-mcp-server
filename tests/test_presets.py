"""
Tests for Phase 4 -- named parameter presets.

A preset is a {receiver: [atoms]} bag stored in the IR. Two layers are
covered: the PatchState model (save/get/list, IR round-trip, clear, replay
preservation) and the server tools (save/apply/list, the exact FUDI emitted
by apply, validation, and the unknown-preset error path).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp.fudi import FudiClient  # noqa: E402
from puredata_mcp.patch_state import PatchState  # noqa: E402
from tests.mock_pd import MockPd  # noqa: E402


# --------------------------------------------------------------------------- #
# PatchState model
# --------------------------------------------------------------------------- #

def test_set_get_list_presets():
    st = PatchState()
    st.set_preset("bright", {"freq": ["880"], "cutoff": ["4000"]})
    st.set_preset("dark", {"freq": ["110"]})
    assert st.preset_count() == 2
    assert st.preset_names() == ["bright", "dark"]
    assert st.get_preset("bright") == {"freq": ["880"], "cutoff": ["4000"]}


def test_save_preset_overwrites():
    st = PatchState()
    st.set_preset("p", {"freq": ["440"]})
    st.set_preset("p", {"freq": ["220"], "gain": ["0.3"]})
    assert st.get_preset("p") == {"freq": ["220"], "gain": ["0.3"]}
    assert st.preset_count() == 1


def test_get_preset_returns_a_copy():
    st = PatchState()
    st.set_preset("p", {"freq": ["440"]})
    got = st.get_preset("p")
    got["freq"].append("tampered")
    got["new"] = ["x"]
    # Mutating the returned map must not leak back into stored state.
    assert st.get_preset("p") == {"freq": ["440"]}


def test_presets_round_trip_through_ir():
    st = PatchState()
    st.add("obj", {"type": "osc~", "args": ["440"], "x": 40, "y": 40})
    st.set_preset("bright", {"freq": ["880"], "cutoff": ["4000"]})
    ir = st.to_ir()
    assert ir["presets"] == {"bright": {"freq": ["880"], "cutoff": ["4000"]}}

    restored = PatchState()
    restored.load_ir(ir)
    assert restored.get_preset("bright") == {"freq": ["880"], "cutoff": ["4000"]}
    assert restored.to_ir() == ir


def test_to_ir_always_has_presets_key_even_when_empty():
    assert PatchState().to_ir()["presets"] == {}


def test_load_ir_tolerates_missing_presets_key():
    # Old checkpoints predate presets -- they must still load.
    st = PatchState()
    st.set_preset("stale", {"freq": ["1"]})
    st.load_ir({"version": 1, "canvas": {}, "nodes": [], "edges": []})
    assert st.preset_count() == 0


def test_clear_keeps_presets():
    # Presets are a durable per-project library, not canvas state: clearing
    # the graph (to rebuild) must not erase them.
    st = PatchState()
    st.set_preset("p", {"freq": ["440"]})
    st.add("obj", {"type": "osc~", "args": ["1"], "x": 0, "y": 0})
    st.clear()
    assert st.count() == 0
    assert st.get_preset("p") == {"freq": ["440"]}


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


def test_save_then_list_preset_tool(monkeypatch):
    import asyncio
    import json as _json
    server = _server_or_skip()
    from puredata_mcp.server import (
        pd_save_preset, pd_list_presets, SavePresetInput,
    )
    monkeypatch.setattr(server, "_state", PatchState())
    server._state.initialized = True

    res = _json.loads(asyncio.run(pd_save_preset(SavePresetInput(
        name="bright", params={"freq": ["880"], "cutoff": ["4000"]},
    ))))
    assert res["status"] == "ok"
    assert res["preset_count"] == 1

    listed = _json.loads(asyncio.run(pd_list_presets()))
    assert listed["count"] == 1
    assert listed["presets"]["bright"] == {"freq": ["880"], "cutoff": ["4000"]}


def test_apply_preset_emits_send_messages(monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import (
        pd_save_preset, pd_apply_preset, SavePresetInput, ApplyPresetInput,
    )
    monkeypatch.setattr(server, "_state", PatchState())
    server._state.initialized = True

    asyncio.run(pd_save_preset(SavePresetInput(
        name="scene", params={"freq": ["880"], "cutoff": ["4000", "0.7"]},
    )))

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        asyncio.run(pd_apply_preset(ApplyPresetInput(name="scene")))
        pd.wait_for(2)

    assert pd.messages == [
        "__send freq 880",
        "__send cutoff 4000 0.7",
    ]


def test_apply_unknown_preset_errors(monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_apply_preset, ApplyPresetInput
    monkeypatch.setattr(server, "_state", PatchState())
    server._state.initialized = True

    out = asyncio.run(pd_apply_preset(ApplyPresetInput(name="nope")))
    assert out.startswith("Error: no preset named 'nope'")


def test_save_preset_rejects_empty_atoms_and_bad_receiver():
    import pytest
    from pydantic import ValidationError
    from puredata_mcp.server import SavePresetInput

    with pytest.raises(ValidationError):
        SavePresetInput(name="p", params={"freq": []})
    with pytest.raises(ValidationError):
        SavePresetInput(name="p", params={"bad name": ["1"]})


def test_presets_survive_replay(monkeypatch):
    """_replay (the engine behind pd_restore) preserves preset definitions."""
    server = _server_or_skip()
    monkeypatch.setattr(server, "_state", PatchState())
    ir = {
        "version": 1, "canvas": {},
        "nodes": [{"id": 0, "kind": "obj", "type": "r", "args": ["freq"],
                   "x": 40, "y": 40}],
        "edges": [],
        "presets": {"bright": {"freq": ["880"]}},
    }
    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        server._replay(ir)
        pd.wait_for(2)

    assert server._state.get_preset("bright") == {"freq": ["880"]}


# --------------------------------------------------------------------------- #
# Per-project presets.json library
# --------------------------------------------------------------------------- #

def test_save_preset_writes_presets_json(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    server = _server_or_skip()
    from puredata_mcp.server import pd_save_preset, SavePresetInput
    monkeypatch.setattr(server, "_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "_state", PatchState())
    server._state.initialized = True

    asyncio.run(pd_save_preset(SavePresetInput(
        name="bright", params={"freq": ["880"]})))

    lib = tmp_path / "presets.json"
    assert lib.exists()
    assert _json.loads(lib.read_text()) == {"bright": {"freq": ["880"]}}


def test_pd_init_loads_presets_json(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    server = _server_or_skip()
    from puredata_mcp.server import pd_init, pd_list_presets, InitInput
    monkeypatch.setattr(server, "_PROJECT_DIR", None)
    monkeypatch.setattr(server, "_state", PatchState())

    (tmp_path / "presets.json").write_text(_json.dumps({
        "warm": {"freq": ["220"], "cutoff": ["1200"]},
    }))

    out = asyncio.run(pd_init(InitInput(project_dir=str(tmp_path))))
    assert "Loaded 1 preset(s)" in out
    listed = _json.loads(asyncio.run(pd_list_presets()))
    assert listed["presets"]["warm"] == {"freq": ["220"], "cutoff": ["1200"]}


def test_presets_library_survives_clear_via_tool(tmp_path, monkeypatch):
    """Clearing the canvas keeps the preset library (and its file)."""
    import asyncio
    import json as _json
    server = _server_or_skip()
    from puredata_mcp.server import pd_save_preset, pd_clear_canvas, SavePresetInput
    st = PatchState()
    st.on_change = server._persist_session
    monkeypatch.setattr(server, "_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "_state", st)
    server._state.initialized = True

    asyncio.run(pd_save_preset(SavePresetInput(name="p", params={"freq": ["1"]})))

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        asyncio.run(pd_clear_canvas())
        pd.wait_for(1)

    assert server._state.get_preset("p") == {"freq": ["1"]}
    assert _json.loads((tmp_path / "presets.json").read_text()) == {"p": {"freq": ["1"]}}
