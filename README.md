# puredata-mcp

A reliable [Model Context Protocol](https://modelcontextprotocol.io) server for
[Pure Data](https://puredata.info). It lets an MCP client (Claude Desktop, etc.)
build and drive Pd patches in natural language: create objects, wire them
together, toggle audio, and send live messages into a running patch.

## Why this design is reliable

It speaks Pd's **native FUDI protocol** over a plain TCP socket and uses Pd
vanilla's built-in **dynamic patching** messages (`obj`, `msg`, `connect`,
`disconnect`, `clear`). That means:

- **No externals** to install (no mrpeach/OSC, no zexy, no `namecanvas`).
- **No intermediate daemon** — the MCP server talks straight to `[netreceive]`.
- **Object ids stay in sync** with Pd because the server mirrors Pd's exact
  creation-index ordering.

Earlier OSC-based Pd MCP attempts were fragile precisely because they stacked an
OSC layer plus a relay daemon on top of Pd. This removes both.

## Architecture

```
 Claude / MCP client
        │  (stdio, JSON-RPC)
        ▼
 puredata_mcp.server  ──FUDI over TCP──▶  [netreceive 3000]   (mcp_host.pd)
                                               │
                                          [route __dsp __send]
                                          │     │          └─▶ [send pd-canvas] ─▶ [pd canvas]
                                          │     └─▶ relay ─▶ [send] (to a named [receive])
                                          └─▶ [; pd dsp $1(   (global audio on/off)
```

Objects you create land inside the `[pd canvas]` subpatch, addressed by Pd's
dynamic-patching receiver `pd-canvas`.

## Install

Pick the channel that matches your client. The repo ships in three
formats so each lands cleanly:

| Client | How to install | What it activates |
|---|---|---|
| **Claude Code** (CLI REPL) | inside a `claude` session: `/plugin marketplace add jfboisvenue/pd-mcp-server` then `/plugin install puredata@puredata-marketplace` | Skill auto-load + 18 MCP tools |
| **Claude Desktop — Cowork sessions** | Plugins panel → **"Add from repository"** → paste `https://github.com/jfboisvenue/pd-mcp-server` | Skill auto-load + 18 MCP tools (Cowork chat only) |
| **Claude Desktop — regular chat** | Download `puredata-mcp.mcpb` from [Releases](https://github.com/jfboisvenue/pd-mcp-server/releases) → double-click → Install via **Settings → Extensions** | 18 MCP tools (no skill — the plugin/skill format isn't loaded by this channel) |

You can install both — the plugin (Code + Cowork) and the `.mcpb`
(regular Desktop chat) coexist, share the same source code, and don't
conflict.

After install, the skill (where applicable) auto-loads when relevant
and the MCP tools become available. **You still need to open
`pd/mcp_host.pd` in Pd** for the server to have something to talk to.

### Why three channels?

Anthropic's plugin marketplace targets Claude Code and Desktop's Cowork
sessions. Plugins installed this way load their skill in regular Desktop
chat too, but **not** the MCP server — that's by design across all
platforms (macOS, Windows, Linux), not a Linux-specific bug. For the MCP
to appear in regular Desktop chat, you need either a `.mcpb` Desktop
Extension or a manual entry in `~/.config/Claude/claude_desktop_config.json`
(see *Manual MCP install* below).

### Local dev / testing

For iteration on the code itself:

```bash
claude --plugin-dir /absolute/path/to/pd-mcp-server   # per-session
```

Or symlink once, persistent across sessions and surfaces:

```bash
mkdir -p ~/.claude/skills && \
ln -s /absolute/path/to/pd-mcp-server ~/.claude/skills/puredata
```

## Requirements

**For the MCP core (object/wire/DSP tools):**
- Pure Data **vanilla 0.51+**
- Python **3.10+** to run the MCP server
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

**For the Python tools** (`pd_create_python_object`, `pd_update_python_script`)
the user's Pd host machine additionally needs:

- Pure Data **vanilla ≥ 0.56** — py4pd 1.2.3 uses `pd_snprintf`, missing in
  0.54.x and earlier (symptom: `undefined symbol: pd_snprintf` /
  `couldn't create`).
- [**py4pd external**](https://github.com/charlesneimog/py4pd) **≥ 1.2.3**
  installed in Pd (Help → Find externals → "py4pd").
- **Python 3.14** installed system-wide — py4pd's Deken build links against
  `libpython3.14.so.1.0`. On Ubuntu 24.04 (which ships 3.12 by default) you
  may need to install 3.14 separately (e.g. deadsnakes PPA).
- The host patch must contain `[declare -path <scripts dir> -lib py4pd]`.
  The bundled `pd/mcp_host.pd` already does this; custom host patches need
  it added.

Verified working stack: Pd 0.56-3 + py4pd 1.2.3 + Python 3.14.5 +
`.pd_py` classes inheriting `pd.NewObject`.

## Manual MCP install (fallback)

If the `.mcpb` install isn't an option (corporate restriction, edge
case, you want to point Desktop at your dev checkout), register the
server directly in `~/.config/Claude/claude_desktop_config.json` — the
same file Desktop reads other classic MCPs from:

```json
{
  "mcpServers": {
    "puredata": {
      "command": "/home/YOU/.local/bin/uv",
      "args": [
        "--directory",
        "/absolute/path/to/pd-mcp-server",
        "run",
        "python",
        "-m",
        "puredata_mcp.server"
      ],
      "env": {
        "PD_HOST": "127.0.0.1",
        "PD_PORT": "3000"
      }
    }
  }
}
```

Use the **absolute** path to `uv` (`which uv` → that path). GUI launchers
don't reliably inherit the shell's `PATH`, so a bare `"uv"` sometimes
fails to resolve even when uv is installed.

Then fully quit Claude Desktop (right-click tray icon → Quit, or
`pkill claude-desktop` on Linux) and relaunch. Verify the server started:

```bash
grep puredata ~/.config/Claude/logs/mcp.log | tail -5
# expect: [puredata] Server started and connected successfully
```

This route bypasses the plugin/`.mcpb` mechanisms entirely. It's the
most reliable path when debugging or pinning to a local checkout, at
the cost of hardcoded paths you maintain manually. The skill won't
auto-load this way — the manual config only registers the MCP.

## Releasing a new `.mcpb` (maintainer only)

Tag a version matching `pyproject.toml` and push:

```bash
git tag v0.1.0
git push --tags
```

GitHub Actions (`.github/workflows/release-mcpb.yml`) validates the
manifest, packs the bundle via `mcpb pack`, and publishes a GitHub
release with `puredata-mcp.mcpb` attached. End users install it via
Settings → Extensions.

The `.mcpbignore` file controls what ships in the bundle (currently
10 files / ~23KB). Dev artifacts (`.venv/`, tests, plugin manifests,
CI config, etc.) are excluded. To preview locally:

```bash
npx --yes @anthropic-ai/mcpb pack . /tmp/puredata-mcp.mcpb
unzip -l /tmp/puredata-mcp.mcpb
```

## Tools

The agent must call **`pd_init` first** — every other tool refuses to run
until it has. `pd_init` returns the orientation guide (wire model, id
contract, common-object cheat sheet, cookbook, safety notes), so the LLM
has a single, server-authored source of truth instead of having to infer
conventions from individual tool docstrings.

| Tool | What it does |
|------|--------------|
| `pd_init` | **Mandatory first call.** Returns the orientation guide and unlocks the rest |
| `pd_create_object` | Create `[type args…]` at (x,y); returns its id |
| `pd_create_message` | Create a message box `[atoms…(` |
| `pd_create_comment` | Create a text comment (spaces preserved) |
| `pd_create_floatatom` | Create a typeable number atom |
| `pd_create_bang` | Create a bang button `[bng]` |
| `pd_create_toggle` | Create a toggle `[tgl]` |
| `pd_create_number_box` | Create a bounded number box `[nbx]` |
| `pd_create_slider` | Create a slider (horizontal `[hsl]` or vertical `[vsl]`) |
| `pd_create_python_object` | Write `<name>.pd_py` defining a `pd.NewObject` class and create `[<name>]` on the canvas (atomic, py4pd 1.2.3+) |
| `pd_update_python_script` | Rewrite an existing `<name>.pd_py` (re-create the object on the canvas to pick up changes) |
| `pd_connect` | Wire `source_id:outlet → target_id:inlet` |
| `pd_disconnect` | Remove a connection |
| `pd_set_dsp` | Start/stop global audio DSP |
| `pd_send_message` | Send atoms to a named `[receive]` in the patch |
| `pd_clear_canvas` | Delete everything; reset ids to 0 |
| `pd_resync_index` | Realign the id counter after a manual canvas edit |
| `pd_get_state` | List objects the server has created |

Python class files (`.pd_py`) written by `pd_create_python_object` live in
`pd/scripts/` by default, which the host patch declares via
`[declare -path scripts -lib py4pd]`. The location is overridable per call
(`scripts_dir` parameter) or globally (`PD_SCRIPTS_DIR` env var). The Python
tools require **py4pd ≥ 1.2.3** by Charles Neimog (Pd → Help → Find externals
→ "py4pd"), which itself requires **Pd ≥ 0.55** and **Python 3.14** on the
host system (the Deken build links against `libpython3.14.so.1.0`). Mental
model: one `.pd_py` file defines one `pd.NewObject` subclass and becomes one
custom Pd object; multiple objects = multiple files.

Object ids are Pd creation indices (0, 1, 2, …). `pd_connect` uses them.

### Example: 440 Hz sine to the DAC

```
pd_create_object  type="osc~"  args=["440"]   x=40 y=40   -> id 0
pd_create_object  type="*~"    args=["0.2"]   x=40 y=90   -> id 1
pd_create_object  type="dac~"                 x=40 y=140  -> id 2
pd_connect        0:0 -> 1:0
pd_connect        1:0 -> 2:0      (left channel)
pd_connect        1:0 -> 2:1      (right channel)
pd_set_dsp        on=true
```

## Testing

The wire format, FUDI escaping, and id bookkeeping are covered by tests that run
**without Pd**, using a mock `[netreceive]`:

```bash
pip install pytest
python -m pytest tests/ -v
```

## Known limitations (Pd vanilla)

- **No single-object delete.** Vanilla has no "delete object N" editing message;
  use `pd_clear_canvas` and rebuild, or restructure. (This is a Pd constraint,
  not a server bug.)
- **The server mirrors creation order, it does not read Pd back.** If you edit
  the `[pd canvas]` subpatch by hand while the server runs, ids can drift —
  call `pd_resync_index(next_index=N)` to realign the counter.
- **`pd_send_message` requires a matching `[receive <name>]`** already present in
  your patch.
- Default transport is **TCP**. For UDP, change the patch object to
  `netreceive -u 3000` and adjust the client accordingly.

## Layout

```
pd-mcp-server/
├── manifest.json               # .mcpb manifest (Desktop Extensions channel)
├── .mcpbignore                 # controls what ships in the .mcpb bundle
├── .github/workflows/
│   └── release-mcpb.yml        # CI: pack + publish .mcpb on tag push
├── .claude-plugin/
│   ├── plugin.json             # plugin manifest (Code + Cowork channel)
│   └── marketplace.json        # this repo is its own marketplace
├── .mcp.json                   # plugin's MCP server declaration (stdio)
├── skills/
│   └── puredata/
│       └── SKILL.md            # auto-triggers on Pd/synth/audio intents
├── bin/
│   └── puredata-mcp            # plugin launcher (finds uv across locations)
├── puredata_mcp/
│   ├── server.py               # FastMCP server + tools + init gate
│   ├── guide.py                # orientation guide returned by pd_init
│   ├── fudi.py                 # FUDI TCP client + escaping
│   └── patch_state.py          # creation-index mirror + init flag
├── pd/mcp_host.pd              # the Pd host patch (open this in Pd)
├── pd/scripts/                 # .pd_py files dropped by pd_create_python_object
├── tests/                      # mock-Pd round-trip + unit tests
├── pyproject.toml
└── claude_desktop_config.example.json  # legacy example -- see "Manual MCP install"
```

## License

MIT
