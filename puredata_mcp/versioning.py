"""
Snapshot / restore / versioning for the authoritative IR.

A checkpoint is a commit, in a *dedicated* git repository, of a single
``patch.json`` holding the serialized IR. Keeping it in its own repo (a
folder separate from the project) means snapshots never pollute the project
history; branches are sound variants to A/B.

No new dependency: we shell out to ``git`` via ``subprocess``. Commits carry
a fixed local identity (via ``-c`` flags) so they succeed in headless
environments without touching the user's global git config.

Public surface used by the MCP tools:
  * ``resolve_checkpoints_dir(explicit)`` -- where checkpoints live
  * ``save(dir, ir, label, branch=None)`` -- write + commit a checkpoint
  * ``list_checkpoints(dir)`` -- enumerate checkpoints
  * ``read_ir_at(dir, ref)`` -- load the IR at a label / hash / branch
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List, Optional

PATCH_FILE = "patch.json"

# Local-only identity so commits work without a configured global git user.
_GIT_IDENTITY = [
    "-c", "user.name=puredata-mcp",
    "-c", "user.email=puredata-mcp@localhost",
]

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "checkpoints"


class VersioningError(RuntimeError):
    """Raised when a git operation for versioning fails."""


# --------------------------------------------------------------------------- #
# Directory resolution (mirrors server._resolve_scripts_dir)
# --------------------------------------------------------------------------- #

def resolve_checkpoints_dir(explicit: Optional[str]) -> Path:
    """Pick the checkpoints directory for this call.

    Precedence: explicit arg -> ``PD_CHECKPOINTS_DIR`` env -> bundled
    ``<project>/checkpoints``. Returns an absolute, existing path.
    """
    if explicit:
        p = Path(explicit).expanduser()
        p = p if p.is_absolute() else p.resolve()
    elif os.environ.get("PD_CHECKPOINTS_DIR"):
        p = Path(os.environ["PD_CHECKPOINTS_DIR"]).expanduser()
    else:
        p = _DEFAULT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #

def _git(dir: Path, *args: str, check: bool = True) -> str:
    """Run a git command in ``dir`` and return stripped stdout."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(dir),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # git not installed
        raise VersioningError(
            "git is required for snapshot/restore but was not found on PATH."
        ) from exc
    if check and proc.returncode != 0:
        raise VersioningError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def ensure_repo(dir: Path) -> None:
    """Ensure ``dir`` exists and holds a git repo (init if needed)."""
    dir.mkdir(parents=True, exist_ok=True)
    if (dir / ".git").exists():
        return
    _git(dir, "init", "-q")


def _branches(dir: Path) -> List[str]:
    out = _git(dir, "for-each-ref", "--format=%(refname:short)", "refs/heads",
               check=False)
    return [b for b in out.splitlines() if b]


def _has_commits(dir: Path) -> bool:
    proc = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          cwd=str(dir), capture_output=True, text=True)
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #

def save(dir: Path, ir: dict, label: str, branch: Optional[str] = None) -> dict:
    """Write the IR to ``patch.json`` and commit it as a checkpoint.

    If ``branch`` is given, switch to it (creating it if new) before
    committing. Returns ``{"hash", "branch", "label"}``.
    """
    ensure_repo(dir)

    if branch:
        if branch in _branches(dir):
            _git(dir, "checkout", "-q", branch)
        else:
            _git(dir, "checkout", "-q", "-b", branch)

    (dir / PATCH_FILE).write_text(json.dumps(ir, indent=2) + "\n", encoding="utf-8")

    _git(dir, "add", "-A")
    # --allow-empty: a snapshot is always a checkpoint, even if the IR is
    # byte-identical to the previous one.
    _git(dir, *_GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", label)

    return {
        "hash": _git(dir, "rev-parse", "--short", "HEAD"),
        "branch": _git(dir, "rev-parse", "--abbrev-ref", "HEAD"),
        "label": label,
    }


def list_checkpoints(dir: Path) -> List[dict]:
    """Enumerate checkpoints across all branches, newest first."""
    ensure_repo(dir)
    if not _has_commits(dir):
        return []
    # NUL-separated fields: short hash, subject (label), committer ISO date, refs.
    fmt = "%h%x00%s%x00%cI%x00%D"
    out = _git(dir, "log", "--all", f"--pretty=format:{fmt}", check=False)
    checkpoints: List[dict] = []
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 4:
            continue
        h, label, date, refs = parts[0], parts[1], parts[2], parts[3]
        checkpoints.append({
            "hash": h,
            "label": label,
            "date": date,
            "refs": refs.strip(),
        })
    return checkpoints


def _hash_for_label(dir: Path, label: str) -> Optional[str]:
    """Most recent commit hash whose subject equals ``label`` (exact)."""
    out = _git(dir, "log", "--all", "--pretty=format:%H%x00%s", check=False)
    for line in out.splitlines():
        if "\x00" not in line:
            continue
        full_hash, subject = line.split("\x00", 1)
        if subject == label:
            return full_hash
    return None


def read_ir_at(dir: Path, ref: str) -> dict:
    """Load the IR JSON at ``ref`` (a label, short/long hash, or branch)."""
    ensure_repo(dir)
    candidates = [ref]
    label_hash = _hash_for_label(dir, ref)
    if label_hash:
        candidates.append(label_hash)
    last_err: Optional[str] = None
    for candidate in candidates:
        proc = subprocess.run(
            ["git", "show", f"{candidate}:{PATCH_FILE}"],
            cwd=str(dir), capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        last_err = proc.stderr.strip()
    raise VersioningError(
        f"could not read checkpoint {ref!r}: {last_err or 'no matching ref'}"
    )
