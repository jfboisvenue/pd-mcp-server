# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Template patches** — reusable sub-graphs (structure), distinct from presets
  (values). Tool count grew from 27 to **30**.
  - `pd_save_template(name, description?, ids?)` captures the current patch, or a
    subset by id (keeping only the wiring internal to the selection; boundary
    edges are dropped and reported), with local ids renormalized to `0..k-1`.
  - `pd_apply_template(name, params?, dx?, dy?)` stamps a template into the
    current patch by **appending** its objects and internal wiring (no clear),
    offsetting positions by `dx`/`dy`, and returns an `id_map` (template-local id
    → new canvas id) for wiring the instance's boundary into the rest of the
    patch.
  - `pd_list_templates()` lists each template's description, object/connection
    counts, and the `${params}` it requires.
  - **`${token}` parameterization** (not a DSL): `${name}` holes in object args,
    message atoms, and comment text are substituted at apply, so multiple
    instances get unique, non-colliding receiver/buffer names (`freq_1`,
    `freq_2`, …). Applying validates that every token is supplied.
  - Templates live in a **global library** (cross-project), one `<name>.json`
    file per template, in your **Pd user folder** — auto-located by reading Pd's
    search paths from `~/.pdsettings` / the macOS plist (e.g.
    `~/Documents/Pd/templates`), overridable with `PD_TEMPLATES_DIR`. They
    persist and are reusable in every patch with no project binding; `pd_init`
    loads them unconditionally and announces the path. Unlike presets they do
    **not** ride in the IR — a template is reusable tooling, not the content of
    one patch — so the serializer, diff, and replay are untouched.
- New pure module `templates.py` (`capture` / `required_params` / `substitute` /
  `instantiation_plan`); the wire/file format stays defined once in
  `builders.py`.

## [0.2.0] - 2026-06-13

The versioning release. The server now holds an **authoritative in-memory model
(the IR)** of the patch; the Pd canvas is treated as a render target, never the
source of truth. Everything below is a layer over that decision. Tool count grew
from 23 to **27**.

### Added

- **Authoritative IR + git checkpoints** (Phase 1). `pd_snapshot` commits the
  current patch as a git-backed checkpoint in a dedicated, per-patch repo;
  `pd_restore` re-renders the canvas from any checkpoint (by label / hash /
  branch); `pd_list_checkpoints` lists them across branches. Branches are how you
  A/B sound variants.
- **`.pd` serializer** (Phase 2). `pd_export_pd` writes a standalone, openable
  `.pd` file from the live patch or any checkpoint. The record body reuses the
  same `builders` atoms as the wire format, so the format is defined once.
- **Semantic graph diff** (Phase 3). `pd_diff` reports a musical, graph-level
  diff between two checkpoints, or a checkpoint vs. the live patch — nodes matched
  by id, position changes ("moved") split from semantic param changes.
- **Named parameter presets** (Phase 4). `pd_save_preset` / `pd_apply_preset` /
  `pd_list_presets` capture a bag of `{receiver: atoms}` values and re-send them
  into the live patch non-destructively (no re-render). Presets ride in the IR, so
  snapshots version them, and they survive `pd_clear_canvas`.
- **Durable per-project preset library.** When the session is bound to a project,
  presets mirror to a human-readable `presets.json` (meant to be committed) and
  reload automatically at `pd_init`.
- **Per-project binding.** `pd_init(project_dir=...)` binds the session so each
  patch gets its own `checkpoints/` and `scripts/` directories. Precedence:
  explicit call arg → session binding → env var → bundled default.
- **IR autosave + recovery.** When bound, every IR mutation rewrites
  `<project_dir>/.pd_session.json` (atomic, best-effort). `pd_recover` reloads and
  re-renders it so the canvas survives a server restart. This is unsaved-work
  recovery, distinct from the versioned `pd_snapshot` history.

### Changed

- Restore and serialize now **recompact object ids** to `0..n-1` and remap edges,
  so hand-edit / resync holes don't leak into checkpoints or exported `.pd` files.
- `builders.py` is now the single source of truth for the wire/file format,
  shared by the create tools, replay, and the `.pd` serializer.
- README updated to document all 27 tools and the versioning / presets layers.

### Notes

- Phase 5 (live read-back from Pd) is intentionally **not** implemented: the
  canvas stays a render target. The IR carries a `version` field to leave room for
  future format migrations.

## [0.1.1] - 2025

### Fixed

- Corrected a `PD_SIGNAL` typo.
- Documented the typed-inlet crash (py4pd inlets must be `pd.DATA`) and the
  module-cache reload limitation in the guide.

[Unreleased]: https://github.com/jfboisvenue/pd-mcp-server/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jfboisvenue/pd-mcp-server/releases/tag/v0.2.0
[0.1.1]: https://github.com/jfboisvenue/pd-mcp-server/releases/tag/v0.1.1
