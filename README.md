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

## Install as a Claude plugin (recommended)

This repo is structured as a Claude plugin (`.claude-plugin/plugin.json`,
`.mcp.json`, `skills/puredata/SKILL.md`) so it can be installed on Claude
Desktop and Claude Code in one step. The plugin bundles the MCP server
**and** a skill that auto-triggers Claude on Pd-related intents (synth,
FM, audio routing, py4pd, etc.) without requiring the user to mention
the MCP by name.

For local development / testing:
```bash
claude --plugin-dir /absolute/path/to/pd-mcp-server
```

This repo is its own marketplace (see `.claude-plugin/marketplace.json`), so
end users install it directly from GitHub — by two different routes depending
on the client:

**Claude Cowork / Claude Desktop (UI):** open the plugins panel →
**"Add from repository"** → paste this repo's URL
(`https://github.com/jfboisvenue/pd-mcp-server`). It installs the bundle
(skill + MCP server) in one step. Note: there is **no `/plugin` slash command
in the Cowork chat** — typing `/plugin …` there fails with "unknown skill".

**Claude Code (CLI REPL):** run these as slash commands inside the `claude`
session:

```bash
/plugin marketplace add jfboisvenue/pd-mcp-server
/plugin install puredata@puredata-marketplace
```

After install, the skill auto-loads when relevant, the MCP tools become
available, and your shell can launch Claude (Desktop or Code) without
any further config. **You still need to open `pd/mcp_host.pd` in Pd** for
the server to have something to talk to.

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

## Manual install (without the plugin system)

If you can't use the plugin install for some reason, you can register
the server manually in your client's MCP config (`claude_desktop_config.json`
for Claude Desktop, or `.mcp.json` in any Claude Code workspace):

```json
{
  "mcpServers": {
    "puredata": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/pd-mcp-server",
               "run", "python", "-m", "puredata_mcp.server"],
      "env": { "PD_HOST": "127.0.0.1", "PD_PORT": "3000" }
    }
  }
}
```

With plain pip instead of uv: `pip install -e .` then use
`"command": "python", "args": ["-m", "puredata_mcp.server"]`.

Note: manual install only gives you the MCP. The skill (intent-triggered
orientation, cookbook hints) lives in the plugin's `skills/puredata/SKILL.md`
and won't auto-load this way.

## Known issue: Linux Claude Desktop

The Linux build of Claude Desktop is **community-maintained** (Debian
package), not maintained by Anthropic — macOS and Windows are. On Linux,
marketplace plugin installs land in **"cowork" mode**
(`~/.config/Claude/local-agent-mode-sessions/.../rpm/plugin_*/`), which
loads the plugin's skill but **does not launch the plugin's MCP server**.
Symptom: the `puredata:puredata` skill auto-triggers correctly, but
Claude reports no `pd_*` tools available, and there is no
`mcp-server-puredata.log` in `~/.config/Claude/logs/`.

**Workaround** — register the MCP directly in
`~/.config/Claude/claude_desktop_config.json`, alongside any other MCPs
you already have:

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
on Linux don't reliably inherit the shell's `PATH`, so a bare `"uv"`
sometimes fails to resolve even when it's installed.

Then fully quit Claude Desktop (right-click tray icon → Quit, or
`pkill claude-desktop`) and relaunch. Verify it started:

```bash
grep puredata ~/.config/Claude/logs/mcp.log | tail -5
# expect: [puredata] Server started and connected successfully
```

Claude Code and macOS/Windows Desktop are unaffected — the plugin install
works there end-to-end.

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
pd-mcp-server/                  # plugin root
├── .claude-plugin/
│   └── plugin.json             # plugin manifest (name=puredata, v0.1.0)
├── .mcp.json                   # declares the puredata MCP server (stdio)
├── skills/
│   └── puredata/
│       └── SKILL.md            # auto-triggers on Pd/synth/audio intents
├── puredata_mcp/
│   ├── server.py               # FastMCP server + tools + init gate
│   ├── guide.py                # orientation guide returned by pd_init
│   ├── fudi.py                 # FUDI TCP client + escaping
│   └── patch_state.py          # creation-index mirror + init flag
├── pd/mcp_host.pd              # the Pd host patch (open this in Pd)
├── pd/scripts/                 # .pd_py files dropped by pd_create_python_object
├── tests/                      # mock-Pd round-trip + unit tests
├── pyproject.toml
└── claude_desktop_config.example.json  # legacy manual config (plugin install preferred)
```

## License

MIT
