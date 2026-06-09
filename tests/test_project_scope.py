"""
Tests for per-project versioning: pd_init(project_dir=...) binds the session
so checkpoints and .pd_py scripts default under that project folder, giving
each patch its own versioning instead of one shared repo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puredata_mcp import versioning  # noqa: E402


def _server_or_skip():
    try:
        from puredata_mcp import server
        return server
    except ImportError:
        import pytest
        pytest.skip("mcp package not installed")


# --------------------------------------------------------------------------- #
# Directory resolution precedence
# --------------------------------------------------------------------------- #

def test_session_default_beats_env_and_bundled(tmp_path, monkeypatch):
    session = tmp_path / "proj" / "checkpoints"
    monkeypatch.setenv("PD_CHECKPOINTS_DIR", str(tmp_path / "env"))
    got = versioning.resolve_checkpoints_dir(None, session_default=session)
    assert got == session.resolve()


def test_explicit_beats_session(tmp_path):
    explicit = tmp_path / "explicit"
    session = tmp_path / "session"
    got = versioning.resolve_checkpoints_dir(str(explicit), session_default=session)
    assert got == explicit.resolve()


def test_no_session_falls_back_to_env(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    monkeypatch.setenv("PD_CHECKPOINTS_DIR", str(env_dir))
    assert versioning.resolve_checkpoints_dir(None) == env_dir.resolve()


# --------------------------------------------------------------------------- #
# pd_init project binding
# --------------------------------------------------------------------------- #

def test_pd_init_binds_project_dir(tmp_path, monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_init, InitInput
    monkeypatch.setattr(server, "_PROJECT_DIR", None)

    proj = tmp_path / "my_patch"
    out = asyncio.run(pd_init(InitInput(project_dir=str(proj))))

    assert server._PROJECT_DIR == proj.resolve()
    assert server._session_checkpoints_default() == proj.resolve() / "checkpoints"
    assert "[session bound to project" in out
    # The bound scripts dir lands under the project, not the plugin.
    assert server._resolve_scripts_dir(None) == proj.resolve() / "scripts"


def test_pd_init_no_arg_leaves_binding_unset(monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_init
    monkeypatch.setattr(server, "_PROJECT_DIR", None)

    out = asyncio.run(pd_init())
    assert server._PROJECT_DIR is None
    assert server._session_checkpoints_default() is None
    assert "[session bound to project" not in out


def test_pd_init_recall_without_dir_preserves_binding(tmp_path, monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_init, InitInput
    monkeypatch.setattr(server, "_PROJECT_DIR", None)

    proj = tmp_path / "patch"
    asyncio.run(pd_init(InitInput(project_dir=str(proj))))
    asyncio.run(pd_init())  # re-init without a dir must not clobber the binding
    assert server._PROJECT_DIR == proj.resolve()


def test_explicit_checkpoints_dir_overrides_session_binding(tmp_path, monkeypatch):
    import asyncio
    server = _server_or_skip()
    from puredata_mcp.server import pd_init, InitInput
    monkeypatch.setattr(server, "_PROJECT_DIR", None)

    proj = tmp_path / "patch"
    asyncio.run(pd_init(InitInput(project_dir=str(proj))))
    other = tmp_path / "elsewhere"
    got = versioning.resolve_checkpoints_dir(
        str(other), server._session_checkpoints_default())
    assert got == other.resolve()
