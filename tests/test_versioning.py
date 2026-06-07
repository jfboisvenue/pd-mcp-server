"""
Tests for snapshot/restore versioning over a dedicated git repo.

These run real ``git`` in a tmp dir (no Pure Data, no mcp package). If git
is unavailable the suite skips.
"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from puredata_mcp import versioning  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _ir(freq: str) -> dict:
    return {
        "version": 1, "canvas": {"width": 800, "height": 600},
        "nodes": [{"id": 0, "kind": "obj", "type": "osc~", "args": [freq],
                   "x": 40, "y": 40}],
        "edges": [],
    }


def test_save_then_read_by_label(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir("440"), "base")
    ir = versioning.read_ir_at(d, "base")
    assert ir["nodes"][0]["args"] == ["440"]
    assert (d / "patch.json").exists()
    assert (d / ".git").exists()


def test_list_checkpoints_newest_first(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir("440"), "first")
    versioning.save(d, _ir("880"), "second")
    cps = versioning.list_checkpoints(d)
    assert [c["label"] for c in cps] == ["second", "first"]
    assert all(c["hash"] and c["date"] for c in cps)


def test_read_by_hash(tmp_path):
    d = tmp_path / "checkpoints"
    info = versioning.save(d, _ir("220"), "base")
    ir = versioning.read_ir_at(d, info["hash"])
    assert ir["nodes"][0]["args"] == ["220"]


def test_branches_isolate_variants(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir("440"), "base")
    bright = versioning.save(d, _ir("1760"), "bright", branch="bright")
    assert bright["branch"] == "bright"
    # The branch holds the variant...
    assert versioning.read_ir_at(d, "bright")["nodes"][0]["args"] == ["1760"]
    # ...and the label round-trips too.
    assert versioning.read_ir_at(d, "base")["nodes"][0]["args"] == ["440"]


def test_save_with_pd_text_commits_patch_pd(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir("440"), "base", pd_text="#N canvas 0 0 800 600 12;\n")
    assert (d / "patch.pd").exists()
    # patch.pd is part of the committed checkpoint, not just on disk.
    committed = versioning._git(d, "show", "HEAD:patch.pd")
    assert committed.startswith("#N canvas")


def test_read_unknown_ref_raises(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.save(d, _ir("440"), "base")
    with pytest.raises(versioning.VersioningError):
        versioning.read_ir_at(d, "does-not-exist")


def test_list_on_empty_repo_is_empty(tmp_path):
    d = tmp_path / "checkpoints"
    versioning.ensure_repo(d)
    assert versioning.list_checkpoints(d) == []


def test_resolve_dir_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    assert versioning.resolve_checkpoints_dir(str(explicit)) == explicit.resolve()
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("PD_CHECKPOINTS_DIR", str(env_dir))
    assert versioning.resolve_checkpoints_dir(None) == env_dir
