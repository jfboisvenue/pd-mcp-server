"""
Tests for IR autosave + pd_recover.

When a project is bound (pd_init(project_dir=...)), every graph mutation
rewrites <project_dir>/.pd_session.json so the working IR survives a server
restart. pd_recover reloads that file and re-renders it (clear + replay).
This is unsaved-work recovery, distinct from versioned snapshots.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp.fudi import FudiClient  # noqa: E402
from puredata_mcp.patch_state import PatchState  # noqa: E402
from tests.mock_pd import MockPd  # noqa: E402


def _server_or_skip():
    try:
        from puredata_mcp import server
        return server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")


def _fresh_bound_state(server, project_dir, monkeypatch):
    """Bind a project and install a fresh autosaving state."""
    monkeypatch.setattr(server, "_PROJECT_DIR", Path(project_dir))
    st = PatchState()
    st.on_change = server._persist_session
    monkeypatch.setattr(server, "_state", st)
    return st


# --------------------------------------------------------------------------- #
# Autosave
# --------------------------------------------------------------------------- #

def test_mutation_autosaves_when_bound(tmp_path, monkeypatch):
    server = _server_or_skip()
    st = _fresh_bound_state(server, tmp_path, monkeypatch)

    st.add("obj", {"type": "osc~", "args": ["440"], "x": 40, "y": 40})

    session = tmp_path / ".pd_session.json"
    assert session.exists()
    saved = json.loads(session.read_text())
    assert saved["nodes"][0]["type"] == "osc~"


def test_preset_and_clear_are_autosaved(tmp_path, monkeypatch):
    server = _server_or_skip()
    st = _fresh_bound_state(server, tmp_path, monkeypatch)
    session = tmp_path / ".pd_session.json"

    st.add("obj", {"type": "osc~", "args": ["1"], "x": 0, "y": 0})
    st.set_preset("bright", {"freq": ["880"]})
    assert json.loads(session.read_text())["presets"] == {"bright": {"freq": ["880"]}}

    st.clear()  # clearing the graph is autosaved, but keeps the preset library
    saved = json.loads(session.read_text())
    assert saved["nodes"] == [] and saved["presets"] == {"bright": {"freq": ["880"]}}


def test_no_autosave_when_unbound(tmp_path, monkeypatch):
    server = _server_or_skip()
    monkeypatch.setattr(server, "_PROJECT_DIR", None)
    st = PatchState()
    st.on_change = server._persist_session
    monkeypatch.setattr(server, "_state", st)

    st.add("obj", {"type": "osc~", "args": ["440"], "x": 40, "y": 40})
    # Nothing to write to -- no project bound, stays ephemeral.
    assert not (tmp_path / ".pd_session.json").exists()


def test_autosave_swallows_write_errors(tmp_path, monkeypatch):
    server = _server_or_skip()
    # Point the project at a *file*, so writing <file>/.pd_session.json fails.
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    _fresh_bound_state(server, blocker, monkeypatch)
    # Must not raise -- best-effort autosave.
    server._state.add("obj", {"type": "osc~", "args": ["1"], "x": 0, "y": 0})


# --------------------------------------------------------------------------- #
# pd_init recoverable notice
# --------------------------------------------------------------------------- #

def test_pd_init_reports_recoverable_session(tmp_path, monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_init, InitInput
    monkeypatch.setattr(server, "_PROJECT_DIR", None)
    monkeypatch.setattr(server, "_state", PatchState())

    (tmp_path / ".pd_session.json").write_text(json.dumps({
        "version": 1, "canvas": {},
        "nodes": [{"id": 0, "kind": "obj", "type": "r", "args": ["freq"],
                   "x": 0, "y": 0}],
        "edges": [], "presets": {"p": {"freq": ["1"]}},
    }))

    out = asyncio.run(pd_init(InitInput(project_dir=str(tmp_path))))
    assert "recoverable session found" in out
    assert "1 objects" in out and "1 presets" in out


# --------------------------------------------------------------------------- #
# pd_recover
# --------------------------------------------------------------------------- #

def test_pd_recover_replays_autosaved_ir(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    server = _server_or_skip()
    from puredata_mcp.server import pd_recover
    st = _fresh_bound_state(server, tmp_path, monkeypatch)
    st.initialized = True

    (tmp_path / ".pd_session.json").write_text(_json.dumps({
        "version": 1, "canvas": {},
        "nodes": [
            {"id": 0, "kind": "obj", "type": "osc~", "args": ["440"], "x": 40, "y": 40},
            {"id": 1, "kind": "obj", "type": "dac~", "args": [], "x": 40, "y": 90},
        ],
        "edges": [{"from": 0, "from_outlet": 0, "to": 1, "to_inlet": 0}],
        "presets": {"bright": {"freq": ["880"]}},
    }))

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        result = asyncio.run(pd_recover())
        pd.wait_for(4)

    assert pd.messages == [
        "clear",
        "obj 40 40 osc~ 440",
        "obj 40 90 dac~",
        "connect 0 0 1 0",
    ]
    payload = _json.loads(result)
    assert payload["status"] == "ok"
    assert payload["nodes"] == 2 and payload["edges"] == 1 and payload["presets"] == 1
    # State + presets are live again after recovery.
    assert server._state.get_preset("bright") == {"freq": ["880"]}


def test_pd_recover_errors_without_project(monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_recover
    monkeypatch.setattr(server, "_PROJECT_DIR", None)
    monkeypatch.setattr(server, "_state", PatchState())
    server._state.initialized = True

    out = asyncio.run(pd_recover())
    assert out.startswith("Error: no project bound")


def test_pd_recover_errors_when_nothing_autosaved(tmp_path, monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_recover
    _fresh_bound_state(server, tmp_path, monkeypatch)
    server._state.initialized = True

    out = asyncio.run(pd_recover())
    assert out.startswith("Error: no recoverable session")
