"""
Tests for FUDI formatting/escaping, the wire round-trip through a mock Pd,
PatchState index bookkeeping, and the init gate. These run WITHOUT Pure
Data or the mcp package installed -- they only import puredata_mcp.fudi
and .patch_state (and a guide string from .guide).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp.fudi import FudiClient, escape_atom, format_message  # noqa: E402
from puredata_mcp.patch_state import PatchState  # noqa: E402
from puredata_mcp.guide import GUIDE  # noqa: E402
from tests.mock_pd import MockPd, _split_fudi  # noqa: E402


# -- escaping / formatting --------------------------------------------------- #

def test_escape_plain_atom():
    assert escape_atom("osc~") == "osc~"
    assert escape_atom("440") == "440"


def test_escape_special_chars():
    assert escape_atom("hello world") == "hello\\ world"
    assert escape_atom("a;b") == "a\\;b"
    assert escape_atom("a,b") == "a\\,b"
    assert escape_atom("a\\b") == "a\\\\b"


def test_format_message_terminator():
    assert format_message(["obj", 100, 100, "osc~", 440]) == b"obj 100 100 osc~ 440;\n"


def test_format_message_escapes_per_atom():
    # A msg-box atom containing a space stays one atom (escaped), not two.
    assert format_message(["msg", 10, 10, "hello world"]) == b"msg 10 10 hello\\ world;\n"


def test_format_comment_preserves_spaces_as_single_atom():
    # Regression: pd_create_comment used to split on spaces, sending each
    # word as its own atom. The fix passes the whole text as one atom so
    # FUDI escaping keeps the spaces intact.
    assert format_message(["text", 50, 50, "hello world"]) == b"text 50 50 hello\\ world;\n"


# -- wire round-trip through mock Pd ----------------------------------------- #

def test_roundtrip_create_and_connect():
    with MockPd() as pd:
        client = FudiClient(port=pd.port)
        client.send_atoms(["obj", 50, 50, "osc~", 440])
        client.send_atoms(["obj", 50, 120, "dac~"])
        client.send_atoms(["connect", 0, 0, 1, 0])
        client.send_atoms(["__dsp", 1])
        pd.wait_for(4)
        client.close()
    assert pd.messages == [
        "obj 50 50 osc~ 440",
        "obj 50 120 dac~",
        "connect 0 0 1 0",
        "__dsp 1",
    ]


def test_roundtrip_send_message():
    with MockPd() as pd:
        client = FudiClient(port=pd.port)
        client.send_atoms(["__send", "freq", 440])
        pd.wait_for(1)
        client.close()
    assert pd.messages == ["__send freq 440"]


def test_roundtrip_comment_with_spaces_stays_one_atom():
    with MockPd() as pd:
        client = FudiClient(port=pd.port)
        client.send_atoms(["text", 50, 50, "hello world"])
        pd.wait_for(1)
        client.close()
    # The escaped space lands inside one FUDI atom; the raw wire bytes keep
    # the backslash. (Pd's parser would un-escape it on receive.)
    assert pd.messages == ["text 50 50 hello\\ world"]


def test_roundtrip_bang_wire_format():
    # Spot-check that the bang creation command goes out as expected.
    with MockPd() as pd:
        client = FudiClient(port=pd.port)
        client.send_atoms(["obj", 10, 10, "bng", 15, 250, 50, 0,
                            "empty", "empty", "empty",
                            17, 7, 0, 10, "#fcfcfc", "#000000", "#000000"])
        pd.wait_for(1)
        client.close()
    assert pd.messages[0].startswith("obj 10 10 bng 15 250 50 0 empty empty empty ")


def test_split_fudi_respects_escaped_semicolon():
    # "a\;b" is ONE message, not two.
    assert _split_fudi("a\\;b;\n") == ["a\\;b"]


# -- PatchState bookkeeping -------------------------------------------------- #

def test_state_starts_uninitialized():
    s = PatchState()
    assert s.initialized is False


def test_state_mark_initialized():
    s = PatchState()
    s.mark_initialized()
    assert s.initialized is True


def test_state_indexing_matches_creation_order():
    s = PatchState()
    assert s.add("obj", "osc~ 440") == 0
    assert s.add("obj", "dac~") == 1
    assert s.add("msg", "1") == 2
    assert s.count() == 3
    assert s.next_index() == 3
    assert s.exists(1) and not s.exists(5)


def test_state_clear_resets_indexing():
    s = PatchState()
    s.add("obj", "osc~ 440")
    s.add("obj", "dac~")
    s.clear()
    assert s.count() == 0
    assert s.next_index() == 0
    assert s.add("obj", "phasor~") == 0  # numbering restarts, mirroring Pd


def test_state_as_list_is_ordered():
    s = PatchState()
    s.add("obj", "a")
    s.add("msg", "b")
    assert s.as_list() == [
        {"id": 0, "kind": "obj", "text": "a"},
        {"id": 1, "kind": "msg", "text": "b"},
    ]


def test_state_resync_to_realigns_counter_and_clears_mirror():
    s = PatchState()
    s.add("obj", "osc~ 440")
    s.add("obj", "dac~")
    s.resync_to(7)
    assert s.count() == 0                     # mirror dropped (labels untrustworthy)
    assert s.next_index() == 7
    assert s.add("obj", "phasor~") == 7       # next created object gets the new id


def test_state_resync_to_rejects_negative():
    s = PatchState()
    import pytest
    with pytest.raises(ValueError):
        s.resync_to(-1)


# -- guide ------------------------------------------------------------------- #

def test_guide_mentions_id_contract_and_workflow():
    # Sanity check: the guide isn't empty and covers the load-bearing points.
    assert "creation index" in GUIDE.lower()
    assert "pd_init" in GUIDE
    assert "pd_resync_index" in GUIDE
    assert "pd_set_dsp" in GUIDE


def test_guide_documents_python_external():
    assert "py4pd" in GUIDE
    assert "pd_create_python_object" in GUIDE
    assert "pd_update_python_script" in GUIDE
    assert "Find externals" in GUIDE
    assert "puredata.info/docs" in GUIDE  # docs link added earlier
    # Current py4pd 1.2.3+ class-based API must be documented.
    assert ".pd_py" in GUIDE
    assert "pd.NewObject" in GUIDE
    assert "declare -lib py4pd" in GUIDE
    # No leftover references to obsolete py/pyext APIs.
    assert "[pyext" not in GUIDE
    assert "mode='py'" not in GUIDE
    # The guide does mention [py4pd script function] as a "do NOT use" warning;
    # we just verify it's explicitly marked as removed/obsolete.
    assert "REMOVED" in GUIDE or "removed" in GUIDE


# -- server-side helpers for Python tools (import server.py) ----------------- #

def test_validate_py_identifier_accepts_valid_names():
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    # Valid identifiers should not raise.
    for name in ["my_proc", "_hidden", "abc123", "X"]:
        server._validate_py_identifier(name, "script")


def test_validate_py_identifier_rejects_invalid_names():
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    import pytest
    for name in ["1starts_with_digit", "has space", "has.dot",
                 "has/slash", "has-dash", "trailing.py", ""]:
        with pytest.raises(ValueError):
            server._validate_py_identifier(name, "script")


_TEMPLATE_CLASS = (
    "import puredata as pd\n\n"
    "class doubler(pd.NewObject):\n"
    "    name = \"doubler\"\n"
    "    def __init__(self, args):\n"
    "        self.inlets = (pd.DATA,)\n"
    "        self.outlets = (pd.DATA,)\n"
    "    def in_0_list(self, l):\n"
    "        self.out(0, pd.LIST, [2*x for x in l])\n"
)


def test_python_object_write_and_create_atomic(tmp_path, monkeypatch):
    """The atomic tool writes <name>.pd_py and sends [obj X Y <name>]."""
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    monkeypatch.setattr(server, "PD_SCRIPTS_DIR", tmp_path)
    server._state.initialized = True

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        import asyncio
        from puredata_mcp.server import (
            pd_create_python_object, CreatePythonObjectInput,
        )
        result = asyncio.run(pd_create_python_object(CreatePythonObjectInput(
            name="doubler", code=_TEMPLATE_CLASS, x=10, y=20,
        )))
        pd.wait_for(1)

    written = tmp_path / "doubler.pd_py"
    assert written.exists()
    assert written.read_text() == _TEMPLATE_CLASS
    # Wire format under py4pd 1.2.3+: [obj X Y <name>] -- the class
    # autoregisters via py4pd's library loader, NOT via [py4pd script function].
    assert pd.messages == ["obj 10 20 doubler"]
    import json as _json
    payload = _json.loads(result)
    assert payload["status"] == "ok"
    assert payload["script_path"].endswith("doubler.pd_py")
    assert payload["object"] == "doubler"


def test_python_object_writes_to_explicit_scripts_dir(tmp_path, monkeypatch):
    """When scripts_dir is provided, the .pd_py file lands there, not in the default."""
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    default_dir = tmp_path / "plugin_default"
    explicit_dir = tmp_path / "user_project" / "scripts"
    monkeypatch.setattr(server, "PD_SCRIPTS_DIR", default_dir)
    server._state.initialized = True

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        import asyncio, json as _json
        from puredata_mcp.server import (
            pd_create_python_object, CreatePythonObjectInput,
        )
        result = asyncio.run(pd_create_python_object(CreatePythonObjectInput(
            name="doubler", code=_TEMPLATE_CLASS,
            scripts_dir=str(explicit_dir), x=10, y=20,
        )))
        pd.wait_for(1)

    assert (explicit_dir / "doubler.pd_py").exists()
    assert not (default_dir / "doubler.pd_py").exists()
    payload = _json.loads(result)
    assert payload["scripts_dir"] == str(explicit_dir)
    # The response should remind the agent about BOTH declarations.
    assert "declare -path" in payload["message"]
    assert "declare -lib py4pd" in payload["message"]


def test_python_object_expands_user_home_in_scripts_dir(tmp_path, monkeypatch):
    """`~` in scripts_dir is expanded to the user's home."""
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    monkeypatch.setenv("HOME", str(tmp_path))
    server._state.initialized = True

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        import asyncio, json as _json
        from puredata_mcp.server import (
            pd_create_python_object, CreatePythonObjectInput,
        )
        result = asyncio.run(pd_create_python_object(CreatePythonObjectInput(
            name="doubler", code=_TEMPLATE_CLASS,
            scripts_dir="~/my_pd_classes", x=10, y=20,
        )))
        pd.wait_for(1)

    expected = tmp_path / "my_pd_classes"
    assert (expected / "doubler.pd_py").exists()
    payload = _json.loads(result)
    assert payload["scripts_dir"] == str(expected)


def test_python_object_falls_back_to_default_without_scripts_dir(tmp_path, monkeypatch):
    """When scripts_dir is omitted, the server uses PD_SCRIPTS_DIR + warns."""
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    monkeypatch.setattr(server, "PD_SCRIPTS_DIR", tmp_path)
    server._state.initialized = True

    with MockPd() as pd:
        monkeypatch.setattr(server, "_client", FudiClient(port=pd.port))
        import asyncio, json as _json
        from puredata_mcp.server import (
            pd_create_python_object, CreatePythonObjectInput,
        )
        result = asyncio.run(pd_create_python_object(CreatePythonObjectInput(
            name="fallback", code=_TEMPLATE_CLASS.replace("doubler", "fallback"),
            x=10, y=20,
        )))
        pd.wait_for(1)

    assert (tmp_path / "fallback.pd_py").exists()
    payload = _json.loads(result)
    assert "default scripts dir" in payload["message"]
    assert "scripts_dir explicitly" in payload["message"]


def test_python_object_rejects_invalid_name(tmp_path, monkeypatch):
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    monkeypatch.setattr(server, "PD_SCRIPTS_DIR", tmp_path)
    server._state.initialized = True

    import pytest
    from pydantic import ValidationError
    from puredata_mcp.server import CreatePythonObjectInput
    with pytest.raises(ValidationError) as exc_info:
        CreatePythonObjectInput(
            name="has space", code=_TEMPLATE_CLASS, x=10, y=20,
        )
    assert "name" in str(exc_info.value)


def test_python_update_script_rewrites_file(tmp_path, monkeypatch):
    try:
        from puredata_mcp import server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")
        return
    monkeypatch.setattr(server, "PD_SCRIPTS_DIR", tmp_path)
    server._state.initialized = True

    # Pre-existing .pd_py file.
    (tmp_path / "doubler.pd_py").write_text("old code\n")

    import asyncio
    from puredata_mcp.server import (
        pd_update_python_script, UpdatePythonScriptInput,
    )
    result = asyncio.run(pd_update_python_script(UpdatePythonScriptInput(
        name="doubler", code="new code\n",
    )))
    assert (tmp_path / "doubler.pd_py").read_text() == "new code\n"
    import json as _json
    payload = _json.loads(result)
    assert payload["existed"] is True
    assert "Rewrote" in payload["message"]
    # Should tell the agent that py4pd's sys.modules cache means re-creating
    # the object alone is insufficient -- only a Pd restart picks up edits.
    assert "RESTART" in payload["message"]
    assert "sys.modules" in payload["message"]


# -- server-side init gate (imports server.py) ------------------------------- #

def test_require_init_blocks_until_initialized():
    # The MCP framework is mocked away just enough to import the module;
    # we test the gate function and the shared _state in isolation.
    try:
        from puredata_mcp import server  # noqa: F401  (may fail without `mcp` installed)
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed; server import skipped")
        return
    # Reset the shared state for a deterministic test.
    server._state.initialized = False
    assert server._require_init() is not None
    assert "pd_init" in server._require_init()
    server._state.mark_initialized()
    assert server._require_init() is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
