---
description: Use when the user wants to build, modify, or interact with Pure Data (Pd) patches — including sound synthesis (sine, FM, AM, subtractive, granular), audio routing, sequencing, control-rate logic, MIDI processing, or running Python code inside Pd via py4pd. Also use for help with FUDI, dynamic patching, [netreceive] / [netsend], or live message routing in a running Pd patch.
---

# Pure Data via MCP

You have access to the **puredata** MCP server. It speaks Pd's native FUDI protocol over TCP to a `[netreceive 3000]` in a host patch the user is expected to keep open. Through the server's tools you can create objects, wire them together, toggle audio DSP, drive named receivers, and write Python scripts that become live `[py4pd]` objects on the canvas — all in one round-trip per call.

## The one rule

**Call `pd_init` before any other tool.** Every other tool in this MCP refuses to run until you do. `pd_init` returns the technical contract (wire model, object-id system, gotchas of Pd vanilla, cookbook). Read it carefully on the first call of every session — it is the authoritative source on conventions. This SKILL.md just helps you decide *when* to reach for the MCP; `pd_init` tells you *how* to use it.

## When this skill applies

Look for any of these intents:

- "make a synth / drone / pad / bass / lead"
- "build me a Pd patch that…"
- "wire X to Y in Pure Data"
- "play a sine / FM / noise"
- "add a metronome / sequencer / envelope"
- "process audio with a filter / delay / reverb"
- "send / receive MIDI in Pd"
- "write a Python function that runs inside Pd" (uses py4pd)
- "explain / debug a Pd patch I'm running"
- references to specific Pd objects (`osc~`, `dac~`, `metro`, `r`, `s`, `*~`, etc.)

If the user is just *talking about* Pd conceptually with no patch-building intent (e.g., "what is Pd?", "compare Max vs Pd"), you don't need to invoke the MCP — answer from general knowledge.

## Prerequisites the user must satisfy

Before any MCP call can land:

1. **The host patch must be open in Pd.** It lives at `${CLAUDE_PLUGIN_ROOT}/pd/mcp_host.pd`. If the user reports "nothing is happening" or `pd_init` errors with a connection failure, the most likely cause is that this patch is not open. Tell them: *"Open `pd/mcp_host.pd` in Pure Data and keep it running — it listens on TCP port 3000."*
2. **For Python objects:** the `py4pd` external (≥ 1.2.3) must be installed (Pd → Help → Find externals → "py4pd"), and the patch must contain `[declare -lib py4pd]`. If `pd_create_python_object` succeeds but the `[<name>]` box stays empty/inert inside `[pd canvas]`, either py4pd is missing or the patch lacks the `-lib py4pd` declare.

## Typical workflow

For a build-from-scratch session:

1. `pd_init` — read the orientation guide. **Mandatory.**
2. `pd_get_state` — confirm the canvas is empty (or see what's already there).
3. Plan the signal chain end-to-end *before* creating objects. Sketch source → processing → output mentally. Don't enable DSP until the chain is complete.
4. Create objects with `pd_create_object` (audio/control) or `pd_create_<gui>` (bng, tgl, nbx, hsl/vsl, floatatom). Place them on the canvas with sensible x/y so the user can visually follow.
5. Wire them with `pd_connect`, using the ids returned from step 4.
6. **Only then** call `pd_set_dsp(on=True)`. Audio is loud by default — make sure there's a `[*~ <small>]` (e.g. 0.1–0.2) before `[dac~]`.
7. Drive the patch live with `pd_send_message` if you wired up named `[r <name>]` receivers.

For Python-augmented patches (py4pd 1.2.3+): use `pd_create_python_object` to write a `.pd_py` file defining a `pd.NewObject` subclass, and instantiate `[<name>]` on the canvas in one call. One file = one Pd object class. To iterate on the code, call `pd_update_python_script` then **re-create** the object on the canvas (`pd_clear_canvas` + rebuild, or hand-delete + recreate) — py4pd 1.2.3 does not expose a per-instance reload message.

**Do not use the obsolete `[py4pd script function]` syntax.** That mode was removed in py4pd 1.2.3 and produces inert objects with no inlets/outlets. The current API is class-based: see `pd_init` for the canonical template (`import puredata as pd` + `class <name>(pd.NewObject)` with `name = "<name>"`, `self.inlets`, `self.outlets`, and `in_<idx>_<msgtype>` handlers).

**Important — where the .pd_py file lands.** Both Python tools take an optional `scripts_dir` argument. On the FIRST Python call of a session, ask the user where their Pd patch lives and pass `<patch-dir>/scripts` (or a path they specify) as `scripts_dir`. Remember that value and reuse it for every subsequent Python tool call in the session. Then tell the user their patch must contain BOTH `[declare -path <that-dir>]` AND `[declare -lib py4pd]` so py4pd is loaded and can autoregister the class — the tool response repeats both requirements you should quote back to them. If you skip `scripts_dir`, the server writes to the plugin's bundled `pd/scripts/`, which is only correct when the user is running the unmodified `mcp_host.pd` from the plugin install dir. **Do not assume** this is their setup — ask.

**Environment prerequisites for the Python branch** (warn the user up-front if their patch is on an older Pd, or you'll waste a round-trip on a silent failure):
- Pd vanilla **≥ 0.55** (py4pd 1.2.3 uses a symbol absent in 0.54.x; symptom: `undefined symbol: pd_snprintf` or `couldn't create`).
- Python **3.14** on the system (py4pd Deken build links against `libpython3.14.so.1.0`; Ubuntu 24.04 ships 3.12 by default, so the user may need to install 3.14 separately).

## Patterns to reach for

These are the bread-and-butter shapes. The full cookbook is in `pd_init`'s response — these are reminders so you know what's possible:

- **Basic tone**: `[osc~ FREQ]` → `[*~ GAIN]` → `[dac~]` (mono) or `[dac~]` with both inlets (stereo).
- **FM synthesis**: modulator `[osc~ MOD_FREQ]` → `[*~ MOD_DEPTH]` → carrier `[+~ CARRIER_FREQ]` → `[osc~]` → output.
- **Subtractive**: `[noise~]` or `[saw~]` → `[lop~ CUTOFF]` / `[bp~]` → output. Drive cutoff via `[line~]` for sweeps.
- **Envelope (ADSR-ish)**: `[bang(` → message `[A_VAL, A_TIME(` → `[line~]` → `[*~]` on the signal.
- **Sequencer**: `[metro MS]` → `[counter N]` → `[mod K]` → branching `[select 0 1 2…]` → triggering.
- **Live control**: `[r freq]` somewhere in the patch, then `pd_send_message(receiver="freq", atoms=["440"])` to update it.
- **GUI knob**: `pd_create_slider(orientation="horizontal", min=20, max=2000)` → connect its outlet to whatever should respond (often via `[r/s]` for clarity).

## Safety

- **Audio is loud.** Never enable DSP with the speakers up, no gain stage, or an open-ended source like `[noise~]` straight to `[dac~]`. Always insert `[*~ 0.1]` (or smaller) before `[dac~]` when prototyping.
- **`pd_clear_canvas` is destructive** and resets ids to 0. Confirm with the user before calling it unless they just asked for a fresh start.
- **Vanilla has no single-object delete.** Plan small; build, verify, grow. If a patch goes wrong, `pd_clear_canvas` and rebuild is the cleanest path.
- **The server mirrors creation order; it does not read Pd back.** If the user hand-edits `[pd canvas]` mid-session, ids drift — call `pd_resync_index(next_index=N)` to realign (the user can read N off the canvas or you ask them).

## When you're unsure

If you don't remember the exact creation arguments, inlet/outlet layout, or behavior of a Pd object, **do not guess**. Either:
- Ask the user to confirm, or
- Point them at the official docs: https://puredata.info/docs

A silently-wrong patch (empty `[osc~ garbage]`, mismatched audio/control rates) is harder to debug than a clarifying question. Pd vanilla doesn't surface those errors back to this server.

## What this skill does NOT cover

- Externals other than `py4pd` (e.g. `mrpeach` for OSC, `iemlib`, `zexy`). The cookbook in `pd_init` lists vanilla objects only.
- Recording / file I/O — `pd_init`'s cookbook stays in the real-time domain.
- Reading or analyzing existing `.pd` files on disk. The server only writes to the live canvas via FUDI; it does not parse `.pd` source.

For any of those, fall back to general knowledge and tell the user what's out of scope.
