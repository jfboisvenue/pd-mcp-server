#!/usr/bin/env python3
"""
puredata_mcp -- a reliable Model Context Protocol server for Pure Data.

It drives Pd vanilla through its *native* FUDI protocol and the built-in
dynamic-patching messages (``obj``, ``msg``, ``floatatom``, ``connect``,
``disconnect``, ``clear``). No externals, no OSC layer, no intermediate
daemon -- which is what makes it robust compared to OSC-based approaches.

The agent is expected to call ``pd_init`` first. Every other tool refuses
to run until then, and the init response is the single source of truth
for conventions (id model, gotchas, cookbook).

Configuration via environment variables:
  PD_HOST (default 127.0.0.1), PD_PORT (default 3000).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from mcp.server.fastmcp import FastMCP

from . import builders, pd_diff, pd_serialize, templates, versioning
from .fudi import FudiClient, FudiError
from .guide import GUIDE
from .patch_state import PatchState

# --------------------------------------------------------------------------- #
# Server, shared client and state
# --------------------------------------------------------------------------- #

mcp = FastMCP("puredata_mcp")

PD_HOST = os.environ.get("PD_HOST", "127.0.0.1")
PD_PORT = int(os.environ.get("PD_PORT", "3000"))

# Where pd_create_python_object/pd_update_python_script drop .pd_py files.
# Default: <project_root>/pd/scripts/. The host patch declares this dir
# via "#X declare -path scripts;" so [py <name>] resolves <name>.py here.
PD_SCRIPTS_DIR = Path(os.environ.get(
    "PD_SCRIPTS_DIR",
    str(Path(__file__).resolve().parent.parent / "pd" / "scripts"),
))

_client = FudiClient(host=PD_HOST, port=PD_PORT)
_state = PatchState()

# Global template library (cross-project): {name: {description, nodes, edges}}.
# Unlike presets, templates do NOT ride in the IR -- they are a library of
# reusable sub-graphs (generators of structure), persisted as one <slug>.json per
# template under the resolved global templates dir (see _resolve_templates_dir)
# and hydrated at pd_init. The graph fragments use local 0-based ids. See templates.py.
_templates: Dict[str, dict] = {}

# Session-scoped project binding, set by pd_init(project_dir=...). When set,
# it becomes the DEFAULT location for this patch's checkpoints repo
# (<project_dir>/checkpoints) and .pd_py scripts (<project_dir>/scripts), so
# each patch/project gets its own versioning instead of one shared repo.
_PROJECT_DIR: Optional[Path] = None


def _session_checkpoints_default() -> Optional[Path]:
    """The per-session checkpoints dir from the project binding, or None."""
    return (_PROJECT_DIR / "checkpoints") if _PROJECT_DIR is not None else None


def _session_file() -> Optional[Path]:
    """Rolling autosave file for the live IR, or None when no project is bound.

    Distinct from snapshots: this is unsaved-work recovery (one rolling
    file), not versioned history. Lives in the bound project dir so it
    survives a server restart; recovered with pd_recover.
    """
    return (_PROJECT_DIR / ".pd_session.json") if _PROJECT_DIR is not None else None


def _persist_session() -> None:
    """Best-effort autosave of the current IR. Never raises into a tool.

    Wired as _state.on_change, so every graph mutation rewrites the file
    when a project is bound. A failed write (permissions, disk) must not
    break the create/connect/etc. that triggered it.
    """
    path = _session_file()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state.to_ir(), indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: never leave a half-written file
    except OSError:
        pass


def _presets_file() -> Optional[Path]:
    """Per-project preset library file, or None when no project is bound.

    Unlike the autosave session file, this is a DURABLE, human-readable
    library: one JSON map {name: {receiver: atoms}} the user can open, and
    that pd_init reloads automatically (presets imply no canvas objects, so
    hydrating them is safe without a re-render).
    """
    return (_PROJECT_DIR / "presets.json") if _PROJECT_DIR is not None else None


def _persist_presets() -> None:
    """Best-effort write of the preset library to <project_dir>/presets.json."""
    path = _presets_file()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        library = {name: _state.get_preset(name) for name in _state.preset_names()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(library, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _load_presets_from_disk() -> int:
    """Hydrate the preset library from <project_dir>/presets.json. Returns count."""
    path = _presets_file()
    if path is None or not path.exists():
        return 0
    try:
        library = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(library, dict):
            _state.load_presets(library)
    except (OSError, ValueError):
        return 0
    return _state.preset_count()


def _pd_user_dir() -> Optional[Path]:
    """Best-effort detect Pd's user folder (the one that holds ``externals``).

    Reads the search paths Pd records for itself and returns the ancestor named
    ``Pd`` -- e.g. ``path1 = ~/Documents/Pd/externals`` yields ``~/Documents/Pd``.
    Sources, in order: ``~/.pdsettings`` (Linux/Windows text) then the macOS
    preferences plist. Returns None if nothing conclusive is found.
    """
    paths: List[str] = []
    settings = Path.home() / ".pdsettings"
    if settings.exists():
        try:
            for line in settings.read_text(encoding="utf-8").splitlines():
                key, sep, val = line.partition(":")
                if sep and key.startswith("path") and key[4:].strip().isdigit():
                    paths.append(val.strip())
        except OSError:
            pass
    plist = Path.home() / "Library" / "Preferences" / "org.puredata.pd.plist"
    if not paths and plist.exists():
        try:
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            for k, v in data.items():
                if k.startswith("path") and k[4:].isdigit() and isinstance(v, str):
                    paths.append(v)
        except Exception:  # noqa: BLE001 -- detection is best-effort
            pass
    for p in paths:
        cur = Path(p).expanduser()
        for anc in (cur, *cur.parents):
            if anc.name.lower() == "pd":
                return anc
    return None


def _resolve_templates_dir() -> Path:
    """Resolve the GLOBAL template library folder (cross-project, always there).

    Templates are reusable tooling -- a synth voice you stamp into ANY patch --
    not the content of one project, so unlike checkpoints/presets they live in a
    single global library, not per ``project_dir``. Resolution is deterministic
    (re-derived identically every session, so the library is always found again):
      1. ``PD_TEMPLATES_DIR`` env var.
      2. Pd's user folder via ``~/.pdsettings`` / macOS plist -> ``<pd>/templates``.
      3. ``~/Documents/Pd/templates`` when ``~/Documents/Pd`` exists (Deken default).
      4. ``~/.pd-mcp/templates`` fallback (always works).

    Does NOT create the directory -- merely resolving a path (e.g. at ``pd_init``
    to load) must not litter the filesystem; ``_persist_template`` mkdirs on the
    first actual save. Each template is one ``<slug>.json`` file in this folder.
    """
    env = os.environ.get("PD_TEMPLATES_DIR")
    if env:
        return Path(env).expanduser()
    pd_dir = _pd_user_dir()
    if pd_dir is not None:
        return pd_dir / "templates"
    documents_pd = Path.home() / "Documents" / "Pd"
    if documents_pd.exists():
        return documents_pd / "templates"
    return Path.home() / ".pd-mcp" / "templates"


def _template_slug(name: str) -> str:
    """Filename-safe slug for a template name (the real name rides inside the file)."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return slug or "template"


def _persist_template(name: str) -> Optional[Path]:
    """Best-effort write one template to ``<templates_dir>/<slug>.json``.

    The file stores the real ``name`` alongside the graph, so load is robust to
    slug collisions/renames. Returns the file path on success, else None.
    """
    d = _resolve_templates_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        payload = {"name": name, **_templates[name]}
        dst = d / f"{_template_slug(name)}.json"
        tmp = d / f".{_template_slug(name)}.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, dst)
        return dst
    except OSError:
        return None


def _load_templates_from_disk() -> int:
    """Hydrate the in-memory library by globbing ``<templates_dir>/*.json``.

    Source of truth is the folder, so we clear and reload. Each file is keyed by
    its stored ``name`` (falling back to the filename stem). Returns the count.
    """
    d = _resolve_templates_dir()
    if not d.exists():
        return 0
    _templates.clear()
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or f.stem
        _templates[name] = {"description": data.get("description", ""),
                            "nodes": data.get("nodes", []),
                            "edges": data.get("edges", [])}
    return len(_templates)


_state.on_change = _persist_session

_PY_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_py_identifier(name: str, field: str) -> None:
    if not _PY_IDENT.fullmatch(name):
        raise ValueError(
            f"{field}={name!r} must be a Python identifier "
            "([A-Za-z_][A-Za-z0-9_]*) -- no dots, slashes, or file extension."
        )


def _ensure_scripts_dir() -> Path:
    PD_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    return PD_SCRIPTS_DIR


def _resolve_scripts_dir(explicit: Optional[str]) -> Path:
    """Pick where to write a Python script for this call.

    Order of precedence:
      1. Explicit `scripts_dir` arg from the tool call (RECOMMENDED -- the
         agent should ask the user where their Pd patch lives and pass
         the corresponding scripts folder so the .py file lands next to
         the patch, not inside the plugin's install dir).
      2. The session project binding from ``pd_init(project_dir=...)``,
         i.e. ``<project_dir>/scripts`` -- set once, applies all session.
      3. ``PD_SCRIPTS_DIR`` env var (per-session/global override).
      4. The plugin's bundled ``pd/scripts/`` (only correct when the user
         is also using the bundled mcp_host.pd -- avoid this for shipped
         setups).

    Returns an absolute, existing path. ``~`` is expanded.
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if _PROJECT_DIR is not None:
        p = _PROJECT_DIR / "scripts"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return _ensure_scripts_dir()


def _err(exc: Exception) -> str:
    if isinstance(exc, FudiError):
        return f"Error: {exc}"
    return f"Error: {type(exc).__name__}: {exc}"


def _ok(message: str, **extra) -> str:
    payload = {"status": "ok", "message": message}
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _require_init() -> Optional[str]:
    """Return an error string if pd_init has not been called yet, else None."""
    if not _state.initialized:
        return ("Error: call pd_init first. This MCP server requires reading "
                "its orientation guide before any other tool can be used -- "
                "it covers the id model, the wire protocol, and the gotchas "
                "of Pd vanilla that you will otherwise trip on.")
    return None


# --------------------------------------------------------------------------- #
# Input models
# --------------------------------------------------------------------------- #

class InitInput(BaseModel):
    """Initialize the session, optionally binding it to a project directory."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to THIS patch's project folder. Binds the "
                    "session so each patch gets its OWN versioning: checkpoints "
                    "default to <project_dir>/checkpoints and .pd_py scripts to "
                    "<project_dir>/scripts. ASK the user where their patch lives "
                    "and pass it here on the first call. Omit to fall back to "
                    "the PD_CHECKPOINTS_DIR / PD_SCRIPTS_DIR env vars then the "
                    "bundled defaults -- a single shared repo, which mixes "
                    "unrelated patches' history together.")


class CreateObjectInput(BaseModel):
    """Create a Pd object box (``[type args...]``) on the canvas."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    type: str = Field(
        ...,
        description="Pd object name, e.g. 'osc~', '*~', 'dac~', 'metro', '+', 'r', 's'.",
        min_length=1, max_length=120,
    )
    args: List[str] = Field(
        default_factory=list,
        description="Creation arguments as strings, e.g. ['440'] for osc~ 440.",
        max_length=64,
    )
    x: int = Field(default=50, ge=0, le=10000, description="X position on the canvas (pixels).")
    y: int = Field(default=50, ge=0, le=10000, description="Y position on the canvas (pixels).")


class CreateMessageInput(BaseModel):
    """Create a Pd message box (``[content (``)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    atoms: List[str] = Field(
        ...,
        description="Message-box contents as atoms, e.g. ['1'] or ['set','foo'].",
        min_length=1, max_length=64,
    )
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateCommentInput(BaseModel):
    """Create a Pd comment (a text annotation, not a live object)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(..., description="Comment text. Spaces preserved.", min_length=1, max_length=500)
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateFloatatomInput(BaseModel):
    """Create a typeable number atom ``[floatatom]``."""
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=5, ge=1, le=80, description="Display width in characters.")
    min: float = Field(default=0.0, description="Minimum value (0 0 means unbounded).")
    max: float = Field(default=0.0, description="Maximum value (0 0 means unbounded).")
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateBangInput(BaseModel):
    """Create a bang button ``[bng]``."""
    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=15, ge=8, le=200, description="Square size in pixels.")
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateToggleInput(BaseModel):
    """Create a toggle box ``[tgl]``."""
    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=15, ge=8, le=200, description="Square size in pixels.")
    initial: bool = Field(default=False, description="Initial on/off state.")
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateNumberBoxInput(BaseModel):
    """Create a number box ``[nbx]`` (the boxed numeric display/control)."""
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=5, ge=1, le=80, description="Display width in characters.")
    min: float = Field(default=-1e37, description="Minimum value.")
    max: float = Field(default=1e37, description="Maximum value.")
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class CreateSliderInput(BaseModel):
    """Create a slider, horizontal ``[hsl]`` or vertical ``[vsl]``."""
    model_config = ConfigDict(extra="forbid")

    orientation: Literal["horizontal", "vertical"] = Field(
        ..., description="Slider direction."
    )
    min: float = Field(default=0.0, description="Minimum output value.")
    max: float = Field(default=127.0, description="Maximum output value.")
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)


class ConnectInput(BaseModel):
    """Connect/disconnect two objects by their creation ids."""
    model_config = ConfigDict(extra="forbid")

    source_id: int = Field(..., ge=0, description="Id of the source object.")
    source_outlet: int = Field(default=0, ge=0, le=512, description="0-based outlet index.")
    target_id: int = Field(..., ge=0, description="Id of the target object.")
    target_inlet: int = Field(default=0, ge=0, le=512, description="0-based inlet index.")


class DspInput(BaseModel):
    """Toggle global audio DSP."""
    model_config = ConfigDict(extra="forbid")

    on: bool = Field(..., description="True to start audio DSP, False to stop it.")


class SendMessageInput(BaseModel):
    """Send a runtime message to a named [receive] in the patch."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    receiver: str = Field(..., min_length=1, max_length=120,
                          description="Name of an existing [r <name>] in the patch.")
    atoms: List[str] = Field(..., min_length=1, max_length=64,
                             description="Payload atoms, e.g. ['440'] or ['set','3.5'].")

    @field_validator("receiver")
    @classmethod
    def _no_spaces(cls, v: str) -> str:
        if any(c.isspace() for c in v):
            raise ValueError("receiver name cannot contain whitespace")
        return v


class ResyncInput(BaseModel):
    """Realign the server's id counter after a hand-edit of the canvas."""
    model_config = ConfigDict(extra="forbid")

    next_index: int = Field(..., ge=0,
                            description="The id Pd will assign to the NEXT object created.")


class CreatePythonObjectInput(BaseModel):
    """Create a new Pd object class in Python (py4pd 1.2.3+ API).

    Writes a ``<name>.pd_py`` file containing a ``pd.NewObject`` subclass
    and instantiates ``[<name>]`` on the canvas. py4pd autoloads the
    class via Pd's search path because the host patch declared
    ``[declare -lib py4pd]``.

    Mental model: one ``.pd_py`` file = one Pd object class. The class
    declares its inlets/outlets and message handlers (``in_<idx>_<type>``
    methods). For multiple distinct objects, create multiple files.
    """
    # Do NOT enable str_strip_whitespace -- it would clip trailing
    # newlines / leading shebangs from the `code` field.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ..., min_length=1, max_length=64,
        description="Class name AND Pd object name. Becomes <name>.pd_py on disk "
                    "and [<name>] on the canvas. Must match the class's `name = ` "
                    "attribute in the code -- they are the same identifier.",
    )
    code: str = Field(
        ..., min_length=1, max_length=200_000,
        description="Full source of the .pd_py file. Must `import puredata as pd` "
                    "and define a class extending `pd.NewObject` whose `name` "
                    "attribute equals the `name` parameter above. See the Python "
                    "section in pd_init's response for the canonical template.",
    )
    scripts_dir: Optional[str] = Field(
        default=None,
        description="Absolute path where <name>.pd_py should be written. "
                    "RECOMMENDED: ask the user where their Pd patch lives and "
                    "pass <patch-dir>/scripts so the file ends up alongside the "
                    "patch. The user's patch must contain BOTH "
                    "[declare -path <this-dir>] (so py4pd finds the .pd_py file) "
                    "AND [declare -lib py4pd] (so py4pd is loaded as a library). "
                    "If omitted, falls back to PD_SCRIPTS_DIR env var then to "
                    "the plugin's bundled pd/scripts/.",
    )
    x: int = Field(default=50, ge=0, le=10000)
    y: int = Field(default=50, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        _validate_py_identifier(v, "name")
        return v


class UpdatePythonScriptInput(BaseModel):
    """Rewrite an existing .pd_py file on disk."""
    model_config = ConfigDict(extra="forbid")  # preserve newlines in `code`

    name: str = Field(..., min_length=1, max_length=64,
                      description="Class name (no '.pd_py', no path).")
    code: str = Field(..., min_length=1, max_length=200_000,
                      description="New .pd_py source. Overwrites the previous file.")
    scripts_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to the directory holding <name>.pd_py. "
                    "Pass the same value you used at pd_create_python_object "
                    "time. Falls back to PD_SCRIPTS_DIR / plugin default if "
                    "omitted.",
    )

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        _validate_py_identifier(v, "name")
        return v


class SnapshotInput(BaseModel):
    """Commit the current patch as a versioned checkpoint."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    label: str = Field(..., min_length=1, max_length=200,
                       description="Human label for this checkpoint (becomes the "
                                   "git commit message; restore by this label).")
    branch: Optional[str] = Field(
        default=None, max_length=120,
        description="Optional branch name. Use branches for sound variants to "
                    "A/B (e.g. 'bright', 'dark'). Created if new, switched to if "
                    "it exists. Omit to commit on the current branch.")
    checkpoints_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to the dedicated checkpoints repo. Defaults "
                    "to PD_CHECKPOINTS_DIR env, then the bundled checkpoints/. "
                    "Pass the same value across snapshot/restore/list in a session.")

    @field_validator("branch")
    @classmethod
    def _branch_no_spaces(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and any(c.isspace() for c in v):
            raise ValueError("branch name cannot contain whitespace")
        return v


class RestoreInput(BaseModel):
    """Re-render the canvas from a saved checkpoint (destructive)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    ref: str = Field(..., min_length=1, max_length=200,
                     description="Checkpoint to restore: a label, a short/long "
                                 "commit hash, or a branch name.")
    checkpoints_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to the checkpoints repo (see pd_snapshot).")


class ListCheckpointsInput(BaseModel):
    """List available checkpoints."""
    model_config = ConfigDict(extra="forbid")

    checkpoints_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to the checkpoints repo (see pd_snapshot).")


class ExportPdInput(BaseModel):
    """Export the IR to a standalone, openable .pd file."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., min_length=1,
                      description="Absolute path of the .pd file to write "
                                  "(parent dirs are created).")
    ref: Optional[str] = Field(
        default=None,
        description="Optional checkpoint to export instead of the current "
                    "patch: a label, commit hash, or branch. If omitted, the "
                    "current in-memory patch is exported.")
    checkpoints_dir: Optional[str] = Field(
        default=None,
        description="Checkpoints repo to read `ref` from (see pd_snapshot). "
                    "Only used when `ref` is given.")


class DiffInput(BaseModel):
    """Graph-level diff between two checkpoints (or a checkpoint vs current)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    from_ref: str = Field(..., min_length=1, max_length=200,
                          description="Base checkpoint: a label, commit hash, or "
                                      "branch. The diff is computed FROM here.")
    to_ref: Optional[str] = Field(
        default=None,
        description="Target checkpoint (label/hash/branch). If omitted, the "
                    "current in-memory patch is used as the target -- i.e. "
                    "'what changed since `from_ref`?'.")
    checkpoints_dir: Optional[str] = Field(
        default=None,
        description="Absolute path to the checkpoints repo (see pd_snapshot).")


class SavePresetInput(BaseModel):
    """Save a named bag of parameter values (receiver -> atoms)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120,
                      description="Preset name to save under (overwrites if it "
                                  "exists). Recall later with pd_apply_preset.")
    params: Dict[str, List[str]] = Field(
        ..., min_length=1,
        description="Map of receiver name -> atoms, e.g. "
                    "{'freq': ['440'], 'cutoff': ['800']}. Each key must match an "
                    "[r <name>] in the patch; applying re-sends these values. "
                    "Build your patch so every tweakable parameter lives behind "
                    "an [r <name>] -- then presets are pure message sends, never "
                    "a re-render.")

    @field_validator("params")
    @classmethod
    def _valid_params(cls, v: Dict[str, List[str]]) -> Dict[str, List[str]]:
        for recv, atoms in v.items():
            if not recv or any(c.isspace() for c in recv):
                raise ValueError(f"receiver name {recv!r} must be non-empty and "
                                 "contain no whitespace")
            if not atoms:
                raise ValueError(f"receiver {recv!r} has no atoms; give it at "
                                 "least one value")
        return v


class ApplyPresetInput(BaseModel):
    """Re-send a saved preset's parameter values into the live patch."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120,
                      description="Name of a saved preset (see pd_list_presets).")


class SaveTemplateInput(BaseModel):
    """Capture a reusable sub-graph from the current canvas as a template."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120,
                      description="Template name to save under (overwrites if it exists).")
    description: Optional[str] = Field(
        default=None, max_length=500,
        description="Optional human note on what this template builds.")
    ids: Optional[List[int]] = Field(
        default=None,
        description="Object ids to capture (see pd_get_state). Only edges whose "
                    "BOTH endpoints are in this set are kept; boundary edges are "
                    "dropped (reported). Omit to capture the entire current patch. "
                    "Local ids are renormalized to 0..k-1 so the template stamps "
                    "anywhere. To parameterize, put ${token} in object args / "
                    "message atoms / comment text (e.g. [r freq_${v}], "
                    "[delwrite~ buf_${v} 1000]); tokens ride through capture "
                    "literally and are filled in at pd_apply_template.")


class ApplyTemplateInput(BaseModel):
    """Stamp a saved template into the current patch (append, non-destructive)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120,
                      description="Name of a saved template (see pd_list_templates).")
    params: Dict[str, str] = Field(
        default_factory=dict,
        description="Values for the template's ${tokens}, e.g. {'v': '1'}. EVERY "
                    "${token} in the template must be supplied. Use a distinct "
                    "value per instance so receiver/buffer names stay unique "
                    "(freq_1, freq_2, ...).")
    dx: int = Field(default=0, ge=-10000, le=10000,
                    description="X offset added to every object's position so a "
                                "fresh instance doesn't overlap an existing one.")
    dy: int = Field(default=0, ge=-10000, le=10000,
                    description="Y offset added to every object's position.")


# --------------------------------------------------------------------------- #
# Init -- mandatory first call
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_init",
    annotations={"title": "Initialize Pd MCP Session", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_init(params: InitInput = InitInput()) -> str:
    """**MANDATORY FIRST CALL.** Return the orientation guide for this MCP.

    Every other tool refuses to run until this is called. The guide covers
    the FUDI wire model, how object ids work, Pd vanilla's quirks (no
    single-object delete, manual edits drift ids), and a cookbook of
    common patches. Read it before doing anything else.

    Pass ``project_dir`` (absolute) to bind this session to the patch's
    project folder, so its checkpoints and .pd_py scripts live with it and
    each patch keeps its own versioning. Re-calling without it preserves an
    earlier binding.

    Returns:
        Plain-text guide. The server also marks this session as initialized.
    """
    global _PROJECT_DIR
    if params.project_dir:
        p = Path(params.project_dir).expanduser()
        p = p if p.is_absolute() else p.resolve()
        p.mkdir(parents=True, exist_ok=True)
        _PROJECT_DIR = p
    _state.mark_initialized()
    # The template library is GLOBAL (cross-project), so it loads whether or not
    # a project is bound -- unlike presets/checkpoints, which are per project.
    n_templates = _load_templates_from_disk()
    templates_note = ""
    if n_templates:
        templates_note = (f"\nLoaded {n_templates} template(s) from "
                          f"{_resolve_templates_dir()} -- see pd_list_templates.")
    if _PROJECT_DIR is not None:
        n_presets = _load_presets_from_disk()  # hydrate the durable library
        note = (f"{GUIDE}\n\n[session bound to project: {_PROJECT_DIR}]\n"
                f"Checkpoints default to {_PROJECT_DIR / 'checkpoints'}; "
                f".pd_py scripts to {_PROJECT_DIR / 'scripts'}. Override per "
                f"call with checkpoints_dir / scripts_dir.")
        if n_presets:
            note += (f"\nLoaded {n_presets} preset(s) from "
                     f"{_PROJECT_DIR / 'presets.json'} -- see pd_list_presets.")
        note += templates_note
        recover = _recoverable_summary()
        if recover:
            note += (f"\n\n[recoverable session found: {recover}] The live IR "
                     "is autosaved here. Call pd_recover to clear the canvas and "
                     "re-render this state, OR pd_clear_canvas to start fresh "
                     "(which overwrites the autosave).")
        return note
    return GUIDE + templates_note


def _recoverable_summary() -> Optional[str]:
    """One-line count of a recoverable autosave, or None if absent/unreadable."""
    path = _session_file()
    if path is None or not path.exists():
        return None
    try:
        ir = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (f"{len(ir.get('nodes', []))} objects, "
            f"{len(ir.get('edges', []))} connections, "
            f"{len(ir.get('presets', {}))} presets")


# --------------------------------------------------------------------------- #
# Creation tools
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_create_object",
    annotations={"title": "Create Pd Object", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_object(params: CreateObjectInput) -> str:
    """Create an object box ``[type args...]`` on the canvas.

    The returned id is Pd's creation index and is what pd_connect expects.
    """
    if (gate := _require_init()): return gate
    try:
        text = " ".join([params.type, *params.args]).strip()
        _client.send_atoms(builders.obj_atoms(params.type, params.args, params.x, params.y))
        oid = _state.add("obj", {"type": params.type, "args": params.args,
                                 "x": params.x, "y": params.y})
        return _ok(f"Created object [{text}] (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_message",
    annotations={"title": "Create Pd Message Box", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_message(params: CreateMessageInput) -> str:
    """Create a message box ``[atoms...(`` on the canvas."""
    if (gate := _require_init()): return gate
    try:
        text = " ".join(params.atoms)
        _client.send_atoms(builders.msg_atoms(params.atoms, params.x, params.y))
        oid = _state.add("msg", {"atoms": params.atoms, "x": params.x, "y": params.y})
        return _ok(f"Created message box [{text}( (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_comment",
    annotations={"title": "Create Pd Comment", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_comment(params: CreateCommentInput) -> str:
    """Create a text comment on the canvas (annotation, not a live object).

    The text is sent as a single FUDI atom; spaces in the comment are
    preserved via FUDI escaping rather than being split into atoms.
    """
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(builders.comment_atoms(params.text, params.x, params.y))
        oid = _state.add("comment", {"text": params.text, "x": params.x, "y": params.y})
        return _ok(f"Created comment (id {oid}).", object_id=oid, object=params.text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_floatatom",
    annotations={"title": "Create Pd Floatatom", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_floatatom(params: CreateFloatatomInput) -> str:
    """Create a typeable number atom ``[floatatom]``.

    Use this when you want the user to type or scroll a value directly.
    For a bounded boxed display, prefer pd_create_number_box.
    """
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(builders.floatatom_atoms(
            params.width, params.min, params.max, params.x, params.y))
        text = f"floatatom w={params.width} min={params.min} max={params.max}"
        oid = _state.add("floatatom", {"width": params.width, "min": params.min,
                                       "max": params.max, "x": params.x, "y": params.y})
        return _ok(f"Created floatatom (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_bang",
    annotations={"title": "Create Pd Bang Button", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_bang(params: CreateBangInput) -> str:
    """Create a bang button ``[bng]``. Outlet 0 fires 'bang' on click."""
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(builders.bng_atoms(params.size, params.x, params.y))
        text = f"bng size={params.size}"
        oid = _state.add("bng", {"size": params.size, "x": params.x, "y": params.y})
        return _ok(f"Created bang (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_toggle",
    annotations={"title": "Create Pd Toggle", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_toggle(params: CreateToggleInput) -> str:
    """Create a toggle ``[tgl]``. Outlet 0 emits 0 or 1 on click."""
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(builders.tgl_atoms(params.size, params.initial,
                                              params.x, params.y))
        text = f"tgl size={params.size} init={params.initial}"
        oid = _state.add("tgl", {"size": params.size, "initial": params.initial,
                                 "x": params.x, "y": params.y})
        return _ok(f"Created toggle (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_number_box",
    annotations={"title": "Create Pd Number Box", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_number_box(params: CreateNumberBoxInput) -> str:
    """Create a boxed number ``[nbx]`` with min/max bounds and display."""
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(builders.nbx_atoms(
            params.width, params.min, params.max, params.x, params.y))
        text = f"nbx w={params.width} min={params.min} max={params.max}"
        oid = _state.add("nbx", {"width": params.width, "min": params.min,
                                 "max": params.max, "x": params.x, "y": params.y})
        return _ok(f"Created number box (id {oid}).", object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_create_slider",
    annotations={"title": "Create Pd Slider", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_slider(params: CreateSliderInput) -> str:
    """Create a slider ``[hsl]`` (horizontal) or ``[vsl]`` (vertical)."""
    if (gate := _require_init()): return gate
    try:
        kind = "hsl" if params.orientation == "horizontal" else "vsl"
        _client.send_atoms(builders.slider_atoms(
            params.orientation, params.min, params.max, params.x, params.y))
        text = f"{kind} min={params.min} max={params.max}"
        oid = _state.add(kind, {"orientation": params.orientation, "min": params.min,
                                "max": params.max, "x": params.x, "y": params.y})
        return _ok(f"Created {params.orientation} slider (id {oid}).",
                   object_id=oid, object=text)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --------------------------------------------------------------------------- #
# Python externals (py4pd 1.2.3+ by Charles Neimog)
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_create_python_object",
    annotations={"title": "Create Pd Python Object", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_create_python_object(params: CreatePythonObjectInput) -> str:
    """Write a .pd_py file defining a pd.NewObject class and instantiate it.

    Atomic: writes <name>.pd_py to the managed scripts dir, then creates
    [<name>] on the canvas. py4pd resolves the class via Pd's search path
    (the host patch's [declare -path scripts] + [declare -lib py4pd]).

    py4pd 1.2.3+ class model:
      * The Python file defines `class <name>(pd.NewObject)` with
        `name = "<name>"`.
      * `__init__(self, args)` sets `self.inlets` and `self.outlets`.
        ⚠️ ALWAYS use pd.DATA for inlets -- pd.FLOAT/SYMBOL/LIST
        inlets segfault Pd at instantiation in py4pd 1.2.3. The
        in_<idx>_<msgtype> dispatch still works with pd.DATA inlets.
        Safe output types: pd.DATA, pd.FLOAT, pd.SYMBOL, pd.LIST,
        pd.BANG, pd.SIGNAL.
      * Handlers are methods named `in_<idx>_<msgtype>` (e.g. in_0_list,
        in_0_float, in_0_bang).
      * Outputs go through `self.out(idx, pd.<TYPE>, value)`.

    See pd_init for the canonical template and the full warning.
    """
    if (gate := _require_init()): return gate
    try:
        scripts_dir = _resolve_scripts_dir(params.scripts_dir)
        script_path = scripts_dir / f"{params.name}.pd_py"
        script_path.write_text(params.code, encoding="utf-8")

        # The Pd object name IS the class name -- py4pd autoregisters it
        # when Pd encounters the unknown [name] and finds name.pd_py on path.
        _client.send_atoms(builders.py4pd_atoms(params.name, params.x, params.y))
        oid = _state.add("py4pd", {"name": params.name, "x": params.x, "y": params.y})
        reminder = (
            "" if params.scripts_dir else
            " (Used default scripts dir -- pass scripts_dir explicitly if the "
            "user's patch is not the bundled mcp_host.pd.)"
        )
        return _ok(
            f"Wrote {script_path.name} and created [{params.name}] (id {oid})."
            + reminder
            + " The user's patch must contain "
            + f"[declare -path {scripts_dir}] (or a relative declare resolving "
            + "to the same dir) AND [declare -lib py4pd] for py4pd to "
            + "autoregister the class.",
            object_id=oid, object=params.name,
            script_path=str(script_path), scripts_dir=str(scripts_dir),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_update_python_script",
    annotations={"title": "Update Pd Python Script", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_update_python_script(params: UpdatePythonScriptInput) -> str:
    """Rewrite an existing .pd_py file in the managed scripts dir.

    ⚠️ py4pd 1.2.3 caches every .pd_py in sys.modules and never
    re-imports on object re-creation. After this tool returns, the new
    file is on disk but **the running Pd still executes the OLD
    bytecode**, even if you call pd_clear_canvas and rebuild. A
    traceback would show the new source while the actual error refers
    to the cached symbols -- a confusing signal.

    The only reliable recovery: tell the user to **restart Pd** (close
    + reopen mcp_host.pd). After restart, the next instantiation of
    [<name>] picks up the new file fresh. This tool writes the file
    and surfaces the restart instruction; it cannot bypass the cache.
    """
    if (gate := _require_init()): return gate
    try:
        scripts_dir = _resolve_scripts_dir(params.scripts_dir)
        script_path = scripts_dir / f"{params.name}.pd_py"
        existed = script_path.exists()
        script_path.write_text(params.code, encoding="utf-8")
        verb = "Rewrote" if existed else "Created"
        return _ok(f"{verb} {script_path.name}. To pick up the new code, "
                   f"the user must RESTART Pure Data -- py4pd 1.2.3 caches "
                   f".pd_py in sys.modules and re-creating the [{params.name}] "
                   f"object alone still runs the old bytecode.",
                   script_path=str(script_path), scripts_dir=str(scripts_dir),
                   existed=existed)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --------------------------------------------------------------------------- #
# Wiring / runtime / state
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_connect",
    annotations={"title": "Connect Pd Objects", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_connect(params: ConnectInput) -> str:
    """Connect source outlet -> target inlet using object ids."""
    if (gate := _require_init()): return gate
    try:
        for label, oid in (("source_id", params.source_id), ("target_id", params.target_id)):
            if not _state.exists(oid):
                return (f"Error: {label}={oid} does not exist. "
                        f"Known ids: {[o['id'] for o in _state.as_list()]}.")
        _client.send_atoms(builders.connect_atoms(
            params.source_id, params.source_outlet,
            params.target_id, params.target_inlet))
        _state.add_edge(params.source_id, params.source_outlet,
                        params.target_id, params.target_inlet)
        return _ok(f"Connected {params.source_id}:{params.source_outlet} -> "
                   f"{params.target_id}:{params.target_inlet}.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_disconnect",
    annotations={"title": "Disconnect Pd Objects", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_disconnect(params: ConnectInput) -> str:
    """Remove a connection between two objects (same args as pd_connect)."""
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(["disconnect", params.source_id, params.source_outlet,
                             params.target_id, params.target_inlet])
        _state.remove_edge(params.source_id, params.source_outlet,
                           params.target_id, params.target_inlet)
        return _ok(f"Disconnected {params.source_id}:{params.source_outlet} -> "
                   f"{params.target_id}:{params.target_inlet}.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_set_dsp",
    annotations={"title": "Toggle Pd Audio DSP", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_set_dsp(params: DspInput) -> str:
    """Start or stop global audio processing (DSP) in Pure Data.

    Audio is LOUD by default -- only enable once the chain is wired and
    you have a [*~ <gain>] (gain < 1) in front of [dac~].
    """
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(["__dsp", 1 if params.on else 0])
        return _ok(f"Audio DSP turned {'on' if params.on else 'off'}.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_send_message",
    annotations={"title": "Send Message To Pd Receiver", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_send_message(params: SendMessageInput) -> str:
    """Send atoms to a named [receive] object in the patch.

    Use this to drive a built patch live, e.g. send ['440'] to a
    [r freq]. Requires a matching [r <receiver>] already on the canvas.
    """
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(["__send", params.receiver, *params.atoms])
        return _ok(f"Sent {params.atoms} to receiver '{params.receiver}'.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_clear_canvas",
    annotations={"title": "Clear Pd Canvas", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_clear_canvas() -> str:
    """Delete every object on the canvas and reset id numbering to 0.

    Destructive -- the user should have asked for this (or for a fresh
    start) before you call it.
    """
    if (gate := _require_init()): return gate
    try:
        _client.send_atoms(["clear"])
        _state.clear()
        return _ok("Canvas cleared; object ids reset to 0.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_resync_index",
    annotations={"title": "Resync Pd Id Counter", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_resync_index(params: ResyncInput) -> str:
    """Realign the server's id counter after the user hand-edited the canvas.

    The server mirrors Pd's creation index by counting create calls --
    it does not read Pd back. If the user adds or removes objects in
    [pd canvas] by hand, call this with the id Pd will assign to the
    NEXT object created. The mirror's prior object list is dropped
    because labels can no longer be trusted.
    """
    if (gate := _require_init()): return gate
    try:
        _state.resync_to(params.next_index)
        return _ok(f"Id counter realigned. Next created object will be id "
                   f"{params.next_index}. Mirror list cleared.")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_get_state",
    annotations={"title": "List Tracked Pd Objects", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_get_state() -> str:
    """Return the list of objects this server has created on the canvas.

    This is the server's mirror of Pd's creation order. If the canvas
    was hand-edited it may be stale -- use pd_resync_index to realign.
    """
    if (gate := _require_init()): return gate
    return json.dumps({
        "count": _state.count(),
        "next_index": _state.next_index(),
        "objects": _state.as_list(),
    }, indent=2)


# --------------------------------------------------------------------------- #
# Versioning -- snapshot / restore / list
# --------------------------------------------------------------------------- #

def _replay(ir: dict) -> tuple[int, int]:
    """Re-render the canvas from an IR: clear, recreate nodes, reconnect edges.

    Nodes are replayed in ascending id order so Pd reassigns contiguous
    creation indices 0..n-1. Original ids may have holes (hand-edits); we
    build an old->new id map and remap edges through it. The authoritative
    state is then reloaded from the *compacted* graph so it matches what is
    actually on the canvas.

    Returns (nodes_rendered, edges_rendered).
    """
    _client.send_atoms(["clear"])
    nodes = sorted(ir.get("nodes", []), key=lambda n: n["id"])
    id_map = {node["id"]: new_idx for new_idx, node in enumerate(nodes)}

    for node in nodes:
        params = {k: v for k, v in node.items() if k not in ("id", "kind")}
        _client.send_atoms(builders.atoms_for(node["kind"], params))

    rendered_edges = []
    for e in ir.get("edges", []):
        if e["from"] in id_map and e["to"] in id_map:
            src, dst = id_map[e["from"]], id_map[e["to"]]
            _client.send_atoms(builders.connect_atoms(
                src, e["from_outlet"], dst, e["to_inlet"]))
            rendered_edges.append((src, e["from_outlet"], dst, e["to_inlet"]))

    compacted = {
        "version": ir.get("version", 1),
        "canvas": ir.get("canvas", {}),
        "nodes": [{**{k: v for k, v in node.items() if k != "id"},
                   "id": id_map[node["id"]]} for node in nodes],
        "edges": [{"from": s, "from_outlet": so, "to": d, "to_inlet": di}
                  for (s, so, d, di) in rendered_edges],
        # Presets are receiver-name keyed, not id keyed, so recompaction
        # leaves them untouched -- carry them through verbatim. Restore keeps
        # the definitions but does NOT auto-apply them (the agent recalls a
        # preset explicitly with pd_apply_preset).
        "presets": ir.get("presets", {}),
    }
    _state.load_ir(compacted)
    return len(nodes), len(rendered_edges)


@mcp.tool(
    name="pd_snapshot",
    annotations={"title": "Snapshot Pd Patch", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def pd_snapshot(params: SnapshotInput) -> str:
    """Commit the current patch (IR) as a versioned checkpoint.

    Serializes the authoritative model to JSON and commits it in a
    dedicated git repo. Use ``branch`` for sound variants to A/B. Restore
    later with pd_restore using the label, hash, or branch.
    """
    if (gate := _require_init()): return gate
    try:
        checkpoints_dir = versioning.resolve_checkpoints_dir(
            params.checkpoints_dir, _session_checkpoints_default())
        ir = _state.to_ir()
        info = versioning.save(checkpoints_dir, ir, params.label, params.branch,
                               pd_text=pd_serialize.ir_to_pd(ir))
        return _ok(
            f"Snapshot '{params.label}' committed ({info['hash']}) on branch "
            f"{info['branch']} -- {_state.count()} objects, "
            f"{_state.edge_count()} connections.",
            checkpoints_dir=str(checkpoints_dir),
            hash=info["hash"], branch=info["branch"], label=info["label"],
            nodes=_state.count(), edges=_state.edge_count(),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_restore",
    annotations={"title": "Restore Pd Checkpoint", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_restore(params: RestoreInput) -> str:
    """Re-render the canvas from a saved checkpoint (clears first).

    Destructive: clears the canvas, then replays the checkpoint's objects
    and connections deterministically from the IR. Ids are recompacted to
    0..n-1. Note: for py4pd objects the .pd_py file must still exist on
    disk, and Pd's module cache may require a restart to pick up changed
    Python code.
    """
    if (gate := _require_init()): return gate
    try:
        checkpoints_dir = versioning.resolve_checkpoints_dir(
            params.checkpoints_dir, _session_checkpoints_default())
        ir = versioning.read_ir_at(checkpoints_dir, params.ref)
        n_nodes, n_edges = _replay(ir)
        return _ok(
            f"Restored '{params.ref}': re-rendered {n_nodes} objects and "
            f"{n_edges} connections.",
            checkpoints_dir=str(checkpoints_dir), nodes=n_nodes, edges=n_edges,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_list_checkpoints",
    annotations={"title": "List Pd Checkpoints", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_list_checkpoints(params: ListCheckpointsInput) -> str:
    """List checkpoints (across all branches) in the checkpoints repo."""
    if (gate := _require_init()): return gate
    try:
        checkpoints_dir = versioning.resolve_checkpoints_dir(
            params.checkpoints_dir, _session_checkpoints_default())
        cps = versioning.list_checkpoints(checkpoints_dir)
        return json.dumps({
            "checkpoints_dir": str(checkpoints_dir),
            "count": len(cps),
            "checkpoints": cps,
        }, indent=2)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_export_pd",
    annotations={"title": "Export Pd Patch File", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_export_pd(params: ExportPdInput) -> str:
    """Write a standalone, openable ``.pd`` file from the IR.

    Exports the current in-memory patch, or a saved checkpoint when ``ref``
    is given. The file opens directly in Pure Data and is versionable /
    shareable. Object positions and wiring are reproduced; ids are
    recompacted to 0..n-1.
    """
    if (gate := _require_init()): return gate
    try:
        if params.ref:
            checkpoints_dir = versioning.resolve_checkpoints_dir(
                params.checkpoints_dir, _session_checkpoints_default())
            ir = versioning.read_ir_at(checkpoints_dir, params.ref)
        else:
            ir = _state.to_ir()
        out = Path(params.path).expanduser()
        if not out.is_absolute():
            out = out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(pd_serialize.ir_to_pd(ir), encoding="utf-8")
        return _ok(
            f"Exported {len(ir.get('nodes', []))} objects and "
            f"{len(ir.get('edges', []))} connections to {out}.",
            path=str(out),
            nodes=len(ir.get("nodes", [])), edges=len(ir.get("edges", [])),
            source=params.ref or "current",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_diff",
    annotations={"title": "Diff Pd Checkpoints", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_diff(params: DiffInput) -> str:
    """Graph-level diff between two checkpoints (or a checkpoint vs current).

    Reports added/removed/changed objects and added/removed connections in
    musical terms ("node osc~ 880 added", "edge 1:0 -> 2:1 removed"),
    instead of raw JSON/coordinate noise. Omit `to_ref` to ask "what
    changed since `from_ref`?" against the live patch.
    """
    if (gate := _require_init()): return gate
    try:
        checkpoints_dir = versioning.resolve_checkpoints_dir(
            params.checkpoints_dir, _session_checkpoints_default())
        old = versioning.read_ir_at(checkpoints_dir, params.from_ref)
        new = (versioning.read_ir_at(checkpoints_dir, params.to_ref)
               if params.to_ref else _state.to_ir())
        diff = pd_diff.diff_ir(old, new)
        return _ok(
            pd_diff.format_diff(diff),
            from_ref=params.from_ref, to_ref=params.to_ref or "current",
            diff=diff,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_recover",
    annotations={"title": "Recover Autosaved Pd Session", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_recover() -> str:
    """Re-render the autosaved working IR after a restart (clears first).

    The live IR is autosaved to the bound project on every change. After a
    server/Pd restart the canvas is empty but the model persists here; this
    reloads it and re-renders deterministically (clear + replay, ids
    recompacted to 0..n-1) so the canvas matches again. Requires a project
    binding (pd_init(project_dir=...)). This is unsaved-work recovery, not a
    checkpoint -- use pd_restore for a named/versioned state.
    """
    if (gate := _require_init()): return gate
    if _PROJECT_DIR is None:
        return ("Error: no project bound. Call pd_init(project_dir=...) first "
                "so the server knows where the autosaved session lives.")
    path = _session_file()
    if path is None or not path.exists():
        return (f"Error: no recoverable session at {path}. Nothing was "
                "autosaved yet for this project.")
    try:
        ir = json.loads(path.read_text(encoding="utf-8"))
        n_nodes, n_edges = _replay(ir)
        return _ok(
            f"Recovered autosaved session: re-rendered {n_nodes} objects and "
            f"{n_edges} connections ({len(ir.get('presets', {}))} presets).",
            session_file=str(path), nodes=n_nodes, edges=n_edges,
            presets=len(ir.get("presets", {})),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --------------------------------------------------------------------------- #
# Presets / automation -- named parameter recall (non-destructive)
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_save_preset",
    annotations={"title": "Save Pd Parameter Preset", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_save_preset(params: SavePresetInput) -> str:
    """Save a named set of parameter values (receiver -> atoms).

    A preset captures the values you want at named [r <name>] receivers --
    e.g. {'freq': ['440'], 'cutoff': ['800']}. It is stored in the IR, so
    pd_snapshot versions it with the graph. Recall it with pd_apply_preset
    (a pure message send -- no re-render). Saving an existing name
    overwrites it.
    """
    if (gate := _require_init()): return gate
    try:
        _state.set_preset(params.name, params.params)
        _persist_presets()  # durable per-project library (presets.json)
        return _ok(
            f"Preset '{params.name}' saved ({len(params.params)} parameter(s)).",
            name=params.name, params=params.params,
            preset_count=_state.preset_count(),
            presets_file=str(_presets_file()) if _presets_file() else None,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_apply_preset",
    annotations={"title": "Apply Pd Parameter Preset", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def pd_apply_preset(params: ApplyPresetInput) -> str:
    """Re-send a saved preset's values into the live patch (non-destructive).

    Sends each receiver -> atoms via the same channel as pd_send_message.
    The matching [r <name>] objects must exist on the canvas (after a
    pd_restore they do, since restore re-renders the graph). Never
    re-renders -- this is parameter recall, not a structural change.
    """
    if (gate := _require_init()): return gate
    try:
        try:
            preset = _state.get_preset(params.name)
        except KeyError:
            return (f"Error: no preset named '{params.name}'. "
                    f"Known presets: {_state.preset_names()}.")
        for receiver, atoms in preset.items():
            _client.send_atoms(["__send", receiver, *atoms])
        return _ok(
            f"Applied preset '{params.name}': sent {len(preset)} parameter(s).",
            name=params.name, params=preset,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_list_presets",
    annotations={"title": "List Pd Parameter Presets", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_list_presets() -> str:
    """List saved parameter presets and their values (receiver -> atoms)."""
    if (gate := _require_init()): return gate
    return json.dumps({
        "count": _state.preset_count(),
        "presets": {name: _state.get_preset(name)
                    for name in _state.preset_names()},
    }, indent=2)


# --------------------------------------------------------------------------- #
# Templates -- reusable sub-graphs (structure), stamped with ${token} params
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pd_save_template",
    annotations={"title": "Save Pd Template Patch", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_save_template(params: SaveTemplateInput) -> str:
    """Capture the current patch (or a subset by id) as a reusable template.

    A template is a sub-graph -- structure, unlike a preset's values. Build a
    unit once (e.g. a synth voice), save it here, then pd_apply_template stamps
    copies into any patch. Pass ``ids`` to capture only part of the canvas; only
    edges internal to that selection are kept. Parameterize by putting ${token}
    in object args / message atoms / comment text (e.g. [r freq_${v}]) -- they
    are filled at apply, so multiple instances get unique names. Saved to a
    GLOBAL library (one <name>.json per template) that auto-locates your Pd user
    folder (<pd>/templates, e.g. ~/Documents/Pd/templates) so it's reusable in
    EVERY patch, no project binding needed; override with PD_TEMPLATES_DIR. The
    response reports the exact path. Reloaded at pd_init.
    """
    if (gate := _require_init()): return gate
    try:
        frag, dropped = templates.capture(_state.to_ir(), params.ids)
        _templates[params.name] = {"description": params.description or "",
                                   "nodes": frag["nodes"], "edges": frag["edges"]}
        saved_to = _persist_template(params.name)  # global library, one file per template
        required = templates.required_params(frag)
        where = str(saved_to) if saved_to else f"{_resolve_templates_dir()} (write failed)"
        msg = (f"Template '{params.name}' saved to {where} "
               f"({len(frag['nodes'])} objects, {len(frag['edges'])} connections).")
        if dropped:
            msg += (f" Dropped {dropped} boundary connection(s) -- one endpoint "
                    "was outside the selection.")
        if required:
            msg += f" Supply these params at apply: {required}."
        return _ok(
            msg, name=params.name, nodes=len(frag["nodes"]),
            edges=len(frag["edges"]), dropped_boundary_edges=dropped,
            params=required, template_count=len(_templates),
            templates_dir=str(_resolve_templates_dir()),
            saved_to=str(saved_to) if saved_to else None,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_apply_template",
    annotations={"title": "Apply Pd Template Patch", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def pd_apply_template(params: ApplyTemplateInput) -> str:
    """Stamp a saved template into the current patch (append, non-destructive).

    Substitutes ${tokens} from ``params``, offsets every object by ``dx``/``dy``
    so it doesn't overlap, creates the objects and their internal wiring, and
    APPENDS them to the live graph (no clear). Returns ``id_map`` (template-local
    id -> new canvas id) so you can wire the instance's boundary -- e.g. its
    output -- into the rest of the patch with pd_connect. Stamp N copies by
    calling N times with distinct ``params`` (freq_1, freq_2, ...) and growing
    ``dx``/``dy``. Note: py4pd nodes still need their .pd_py file on disk.
    """
    if (gate := _require_init()): return gate
    try:
        if params.name not in _templates:
            return (f"Error: no template named '{params.name}'. "
                    f"Known templates: {sorted(_templates)}.")
        try:
            filled = templates.substitute(_templates[params.name], params.params)
        except ValueError as exc:
            return _err(exc)
        base = _state.next_index()
        nodes, edges, id_map = templates.instantiation_plan(
            filled, base, params.dx, params.dy)
        for node in nodes:
            kind = node["kind"]
            node_params = {k: v for k, v in node.items() if k != "kind"}
            _client.send_atoms(builders.atoms_for(kind, node_params))
            _state.add(kind, node_params)
        for (src, so, dst, di) in edges:
            _client.send_atoms(builders.connect_atoms(src, so, dst, di))
            _state.add_edge(src, so, dst, di)
        last = base + len(nodes) - 1
        return _ok(
            f"Applied template '{params.name}': stamped {len(nodes)} objects and "
            f"{len(edges)} connections at ids {base}..{last}." if nodes else
            f"Template '{params.name}' is empty; nothing stamped.",
            name=params.name, nodes=len(nodes), edges=len(edges),
            id_map=id_map, base_index=base,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="pd_list_templates",
    annotations={"title": "List Pd Template Patches", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def pd_list_templates() -> str:
    """List saved templates: description, object/connection counts, and the
    ${params} each one requires at pd_apply_template."""
    if (gate := _require_init()): return gate
    out = {
        name: {
            "description": _templates[name].get("description", ""),
            "nodes": len(_templates[name].get("nodes", [])),
            "edges": len(_templates[name].get("edges", [])),
            "params": templates.required_params(_templates[name]),
        }
        for name in sorted(_templates)
    }
    return json.dumps({"count": len(_templates),
                       "templates_dir": str(_resolve_templates_dir()),
                       "templates": out}, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
