"""
Orientation guide returned by ``pd_init``.

This is the *one* place an agent learns how this MCP server expects to be
used: the wire model, the id contract, the gotchas of Pd vanilla, and a
short cookbook. Keeping it server-side (instead of relying purely on tool
docstrings) means we can update conventions in one place and force every
session to start by reading them.
"""

from __future__ import annotations

GUIDE = """\
You are driving Pure Data (Pd vanilla) through this MCP server. Read this
guide once before calling any other tool -- the conventions below are not
obvious from individual tool docstrings.

============================================================
WIRE MODEL
============================================================
- This server speaks Pd's native FUDI protocol over TCP to a single
  [netreceive 3000] in the host patch (pd/mcp_host.pd). Pd vanilla only.
- Every object you create lands inside the [pd canvas] subpatch of the
  host patch. You can open it visually in Pd to watch your work.
- Three message shapes leave the server:
    * editing commands (obj/msg/floatatom/connect/disconnect/clear)
    * __dsp 1|0  -> toggles global audio
    * __send <receiver> <atoms...>  -> drives a named [receive] in Pd

============================================================
THE ID CONTRACT (critical)
============================================================
- Object ids are Pd's *creation index*: 0, 1, 2, ... in order of creation.
- pd_connect / pd_disconnect address objects by these ids.
- The server mirrors this index by counting create calls. It does NOT
  read Pd back. So:
    * Only create through this server, never hand-edit [pd canvas] while
      the server is running.
    * If you must hand-edit (or you restart only one side), call
      pd_resync_index with the new next-id to realign.
    * pd_clear_canvas is the only way to delete; vanilla has no
      "delete object N" message. Plan accordingly: build small, verify,
      grow -- or be ready to clear and rebuild.

============================================================
TYPICAL WORKFLOW
============================================================
1. pd_init(project_dir="...")           (you are here -- bind the project)
2. pd_get_state                         (check what is already there)
3. pd_create_object / pd_create_message / pd_create_<gui>
4. pd_connect    (use ids returned by step 3)
5. pd_set_dsp on=true                   (only when ready -- audio is loud)
6. pd_send_message                      (drive a [r <name>] in the patch)
7. pd_save_preset / pd_apply_preset     (name + recall parameter scenes)
8. pd_snapshot label="..."              (checkpoint a state worth keeping)

============================================================
COMMON OBJECTS BY TASK
============================================================
- Audio source:    osc~ FREQ       (sine), phasor~ FREQ, noise~
- Audio math:      *~, +~, -~, /~
- Audio output:    dac~            (inlet 0 = left, inlet 1 = right)
- Envelope:        line~, vline~, adsr (third-party)
- Filter:          lop~, hip~, bp~, vcf~
- Control rate:    metro MS, delay MS, counter, random N
- Glue:            send/s NAME, receive/r NAME, route, select, pack/unpack
- Math:            +, -, *, /, expr, moses
- GUI:             bng (button), tgl (toggle), nbx (number box),
                   hsl / vsl (sliders), floatatom (typeable number)

Audio objects end in '~'. Connecting an audio outlet to a control inlet
(or vice versa) is usually a mistake -- match the rate.

If you are unsure about a vanilla object's creation args, inlet/outlet
layout, or behavior, do NOT guess -- either:
  * ask the user to confirm, or
  * point them at the official docs:  https://puredata.info/docs
A silently-wrong patch is harder to debug than a clarifying question.

============================================================
DRIVING A LIVE PATCH (pd_send_message)
============================================================
- pd_send_message routes atoms to a named [receive <name>] in the patch.
- The receiver must already exist on the canvas. Create one with
  pd_create_object(type="r", args=["freq"]) and connect its outlet to
  whatever should react to the value.

============================================================
COOKBOOK
============================================================
1) 440 Hz sine -> stereo DAC at low volume
   pd_create_object("osc~", ["440"], 40, 40)        -> id 0
   pd_create_object("*~",   ["0.2"], 40, 90)        -> id 1
   pd_create_object("dac~",  [],     40, 140)       -> id 2
   pd_connect(0,0, 1,0)
   pd_connect(1,0, 2,0)   # left
   pd_connect(1,0, 2,1)   # right
   pd_set_dsp(on=True)

2) Knob-driven frequency
   pd_create_object("r", ["freq"], 40, 10)          -> id 0
   pd_create_object("osc~", ["220"], 40, 60)        -> id 1
   pd_create_object("*~",   ["0.2"], 40, 110)       -> id 2
   pd_create_object("dac~",  [],     40, 160)       -> id 3
   pd_connect(0,0, 1,0); pd_connect(1,0, 2,0)
   pd_connect(2,0, 3,0); pd_connect(2,0, 3,1)
   pd_set_dsp(on=True)
   pd_send_message(receiver="freq", atoms=["880"])

3) Metronome -> counter -> print
   pd_create_object("metro", ["500"], 40, 40)       -> id 0
   pd_create_object("+", ["1"], 40, 90)             -> id 1
   pd_create_object("print", [],     40, 140)       -> id 2
   pd_connect(0,0, 1,0); pd_connect(1,0, 2,0); pd_connect(1,0, 1,1)

============================================================
VERSIONING (snapshot / restore / list)
============================================================
This server keeps an AUTHORITATIVE in-memory model (IR) of the patch:
every object and connection you create is recorded as structured data.
The Pd canvas is a render target -- the server never reads Pd back. That
model is what gets versioned and re-rendered.

- pd_snapshot(label, branch?, checkpoints_dir?)
    Serializes the current patch to JSON and commits it as a checkpoint
    in a DEDICATED git repo (separate from any project repo). Use a
    descriptive label; restore by it later. Use `branch` for sound
    variants to A/B (e.g. branch="bright" vs branch="dark").
- pd_restore(ref, checkpoints_dir?)
    DESTRUCTIVE. Clears the canvas, then replays the checkpoint's objects
    and connections deterministically from the IR. `ref` is a label, a
    commit hash, or a branch name. Object ids are recompacted to 0..n-1
    on restore (edges are remapped automatically) -- after a restore,
    treat the ids from the restore as current.
- pd_list_checkpoints(checkpoints_dir?)
    Lists checkpoints across all branches (hash, label, date, refs).
- pd_export_pd(path, ref?, checkpoints_dir?)
    Writes a standalone, openable .pd file from the IR -- the current
    patch, or a checkpoint when `ref` is given. Use it to hand a patch to
    a human or to Claude Code. (Every pd_snapshot also writes a patch.pd
    inside the checkpoint, so each checkpoint is openable too.)
- pd_diff(from_ref, to_ref?, checkpoints_dir?)
    Graph-level diff: added/removed/changed objects and connections in
    musical terms, not coordinate noise. Compares two checkpoints, or a
    checkpoint vs the current patch (omit to_ref). Great for seeing what
    distinguishes two A/B variants on different branches.

Where checkpoints live -- ONE PROJECT, ONE REPO:
The best way to scope versioning per patch is to call pd_init with a
project_dir at the very start:
    pd_init(project_dir="/abs/path/to/this/patch")
That BINDS the session: checkpoints then default to
<project_dir>/checkpoints and .pd_py scripts to <project_dir>/scripts, so
this patch keeps its own git history -- no need to pass checkpoints_dir on
every call. ASK the user where their patch lives and pass it.

If you skip project_dir, the server falls back to the PD_CHECKPOINTS_DIR
env var, then a bundled default -- a SINGLE shared repo that mixes
unrelated patches' history (and collides branch names) across projects.
You can still pass `checkpoints_dir` per call to override the binding for
a one-off; pass the SAME dir across snapshot/restore/list/diff.

Editing discipline this enables:
- Structural change (add/remove object, rewire) -> snapshot, then build.
  To undo, pd_restore the previous checkpoint (clear + replay).
- Parameter change -> do NOT re-render. Keep the object live behind an
  [r <name>] and pd_send_message a new value. Re-rendering is only for
  structure.

Two limits to know:
- py4pd: restore recreates the [<name>] box but NOT the .pd_py file (it
  must still be on disk), and py4pd's module cache may require a Pd
  restart to pick up changed Python code.
- Single canvas: the IR is flat -- nested subpatches are not modeled.

Autosave & crash recovery (only when project_dir is bound):
The live IR is in memory and dies with the server process. When you bound
a project_dir at pd_init, the server ALSO autosaves the IR to
<project_dir>/.pd_session.json after every change -- a single rolling file,
not versioned history. After a server or Pd restart the canvas is empty but
that file survives, and pd_init will tell you a recoverable session exists.
- pd_recover()
    Reloads the autosaved IR and re-renders it (clear + replay, ids
    recompacted) so the canvas matches your model again. Then pd_apply_preset
    if you need the parameter values back. Requires a bound project.
This is unsaved-work recovery; pd_snapshot/pd_restore is for named,
versioned states. Without project_dir, nothing is autosaved -- snapshot
explicitly or the work is lost on restart.

============================================================
PRESETS / PARAMETER AUTOMATION (save / apply / list)
============================================================
The payoff of the [r <name>] discipline: a PRESET is a named bag of
parameter values -- {receiver: atoms} -- that you can recall instantly
without ever re-rendering. Applying one is pure pd_send_message under the
hood, so it is non-destructive and cheap.

Design your patch so every tweakable parameter is fed by an [r <name>]:
    pd_create_object("r", ["freq"])     -> drives an [osc~]
    pd_create_object("r", ["cutoff"])   -> drives a [lop~]
Then values become data you can name and snapshot, not structure.

- pd_save_preset(name, params)
    params is {receiver: [atoms]}, e.g.
      pd_save_preset("bright", {"freq": ["880"], "cutoff": ["4000"]})
    Re-saving a name overwrites it. Presets are a DURABLE PER-PROJECT
    LIBRARY: when a project is bound, each save also writes
    <project_dir>/presets.json (a human-readable {name: {receiver: atoms}}
    file the user can open), and pd_init reloads it automatically next
    session -- no pd_recover needed, because presets imply no canvas
    objects. They also ride in the IR, so pd_snapshot versions them too,
    and pd_clear_canvas keeps them (a library, not canvas state).
- pd_apply_preset(name)
    Re-sends every receiver->atoms pair into the live patch. The matching
    [r <name>] objects must exist (they do after a pd_restore, which
    re-renders the graph). Never re-renders.
- pd_list_presets()
    Shows saved presets and their values.

How presets and checkpoints compose:
- A pd_snapshot captures BOTH the graph AND the presets defined so far.
- A pd_restore rebuilds the graph and brings the preset DEFINITIONS back,
  but does NOT auto-apply them -- Pd does not persist sent parameter
  values, so after a restore call pd_apply_preset(name) to recall the
  sound. (Restoring the graph alone gives you the patch at its creation-
  arg defaults.)
- Branches are A/B *structure*; presets are A/B *values within one graph*.
  Use presets to morph between scenes; use branches for variant patches.

============================================================
TEMPLATES (reusable sub-graphs: save / apply / list)
============================================================
A PRESET recalls values; a TEMPLATE stamps STRUCTURE. A template is a
reusable fragment of the graph -- objects + their wiring -- that you build
once and instantiate many times (e.g. a synth voice, a filter+envelope
unit, a meter). Applying it creates real objects, APPENDED to the current
patch (never a clear).

Why this is safe and simple: Pd's creation index is append-only, so a
stamped instance just takes the next free ids. The tool returns an
`id_map` (template-local id -> new canvas id) so you can wire the new
instance's boundary into the rest of the patch.

Parameterize with ${token} substitution -- NOT a new language, just named
holes. Put ${name} in object args, message atoms, or comment text:
    pd_create_object("r", ["freq_${v}"])         -> [r freq_${v}]
    pd_create_object("delwrite~", ["buf_${v}", "1000"])
Then each instance gets UNIQUE, non-colliding names. (${tokens} are NOT
substituted in GUI numeric vectors or py4pd class names.)

- pd_save_template(name, description?, ids?)
    Captures the current patch, or just `ids` (see pd_get_state) -- only
    edges internal to that selection are kept; boundary edges are dropped
    and reported. Local ids renormalize to 0..k-1. Saved to a GLOBAL
    library, one <name>.json per template, in your Pd user folder
    (auto-detected: <pd>/templates, e.g. ~/Documents/Pd/templates;
    override with PD_TEMPLATES_DIR). It is reusable in EVERY patch with no
    project binding, and the response reports the exact path.
- pd_apply_template(name, params?, dx?, dy?)
    Substitutes ${tokens} from `params` (every token MUST be supplied),
    offsets positions by dx/dy so the copy doesn't overlap, creates the
    objects + internal wiring, and returns `id_map` + the new id range.
- pd_list_templates()
    Shows each template's description, object/connection counts, and the
    ${params} it requires.

Canonical workflow -- 2 detuned voices into one [dac~]:
    # build ONE voice with a ${v} hole, behind receivers
    pd_create_object("r", ["freq_${v}"], x=40, y=40)      # id 0
    pd_create_object("osc~", [], x=40, y=90)              # id 1
    pd_create_object("*~", ["0.1"], x=40, y=140)          # id 2
    pd_connect(0,0, 1,0); pd_connect(1,0, 2,0)
    pd_save_template("voice", ids=[0,1,2])
    pd_clear_canvas()
    # a shared output, then stamp two voices and wire each in
    pd_create_object("dac~", [], x=40, y=400)             # id 0
    r1 = pd_apply_template("voice", params={"v":"1"}, dx=0,   dy=0)
    r2 = pd_apply_template("voice", params={"v":"2"}, dx=200, dy=0)
    # r*.id_map[2] is each voice's [*~]; wire its outlet 0 -> dac~ inlets
    pd_connect(r1.id_map["2"], 0, 0, 0)
    pd_connect(r2.id_map["2"], 0, 0, 1)
    # drive them live (or save a preset)
    pd_send_message("freq_1", ["220"]); pd_send_message("freq_2", ["223"])

Template vs preset vs branch (note the scope difference):
- Template = reusable STRUCTURE (a generator). GLOBAL library (your Pd
  user folder), shared across ALL patches -- build a voice once, use it
  everywhere.
- Preset   = named VALUES for one patch. Rides in the IR + per-project
  presets.json.
- Branch   = a whole variant patch under per-project versioning.

============================================================
PYTHON IN PD (py4pd 1.2.3+ -- .pd_py classes)
============================================================
With py4pd installed (Pd menu Help -> Find externals -> "py4pd"), you
can ship a Python class file that becomes a custom Pd object.

Mental model: ONE .pd_py file = ONE Pd object class.
  - The file defines `class <name>(pd.NewObject)` whose `name = "<name>"`
    attribute matches the filename.
  - py4pd is loaded as a library by [declare -lib py4pd] in the host
    patch. When Pd encounters an unknown [<name>], py4pd scans the
    declared paths for <name>.pd_py and registers the class.
  - The object is created by its CLASS NAME, not via [py4pd ...].

py4pd 1.2.3 REMOVED the older "function mode" syntax [py4pd script
function]. Do not use it -- you will get inert objects with no
inlets/outlets.

Class anatomy (inlets, outlets, handlers, output):
  - `self.inlets = (pd.DATA, ...)` in __init__: tuple of inlet types.
    **ALWAYS use pd.DATA for inlets** (see warning below).
  - `self.outlets = (pd.DATA, ...)`: tuple of outlet types
  - Handler methods: `in_<idx>_<msgtype>(self, value)` -- inlet typed
    pd.DATA dispatches to the appropriate handler based on the actual
    message type, so you still write typed handlers:
        in_0_bang(self)                    bang at inlet 0
        in_0_float(self, f)                float at inlet 0
        in_0_list(self, l)                 list at inlet 0
        in_0_symbol(self, s)               symbol at inlet 0
        in_1_float(self, f)                float at inlet 1
  - Emit via `self.out(idx, pd.<TYPE>, value)`. Output types: pd.DATA,
    pd.FLOAT, pd.SYMBOL, pd.LIST, pd.BANG, pd.SIGNAL (audio rate).

  ⚠️ INLET TYPE WARNING (py4pd 1.2.3 + Python 3.14, confirmed on
  Windows, behavior on Linux/macOS uncertain at time of writing):
  declaring an inlet as pd.FLOAT, pd.SYMBOL, or pd.LIST **segfaults
  Pd at object instantiation**, before any handler runs. Symptom:
  Pd log shows "1 objects found inside <name>.pd_py" then the
  process dies. **Use pd.DATA for every inlet** -- the typed
  handlers (in_0_float etc.) still receive the right type via
  internal dispatch. pd.BANG and pd.SIGNAL are safe for inlets.
  Output types are unaffected.

Canonical template (give this to the user as the starting point):

  import puredata as pd

  class myobj(pd.NewObject):
      name = "myobj"                       # MUST match filename + Pd object name

      def __init__(self, args):
          self.inlets = (pd.DATA,)         # one any-type inlet
          self.outlets = (pd.DATA,)        # one any-type outlet
          # Persistent state belongs on self:
          # self.count = 0

      def in_0_list(self, l):
          result = [x * 2 for x in l]      # do the work
          self.out(0, pd.LIST, result)     # send out outlet 0

Where the .pd_py file lands:
  If you bound the session with pd_init(project_dir=...), scripts default
  to <project_dir>/scripts automatically -- nothing more to pass.
  Otherwise pd_create_python_object and pd_update_python_script accept an
  optional `scripts_dir` argument (absolute path): ASK THE USER where
  their Pd patch lives and pass <patch-dir>/scripts, then reuse that value
  on every subsequent Python-tool call in the session.

  The user's patch must contain BOTH:
    [declare -path <scripts dir>]    so py4pd finds <name>.pd_py
    [declare -lib py4pd]             so py4pd is loaded as a library
  The bundled mcp_host.pd already has both. A custom host patch needs
  both for the class autoregistration to fire.

  If you omit `scripts_dir`, the server falls back to PD_SCRIPTS_DIR env
  or its bundled pd/scripts/ -- only correct when the user is running
  the plugin's mcp_host.pd unmodified.

Workflow:
  0) Ask the user where their patch lives -- e.g. "I'll save Python
     classes next to your patch. Where is it?"
  1) pd_create_python_object(
         name='doubler',
         scripts_dir='/home/user/music/granular/scripts',
         code='''import puredata as pd

class doubler(pd.NewObject):
    name = "doubler"
    def __init__(self, args):
        self.inlets = (pd.DATA,)
        self.outlets = (pd.DATA,)
    def in_0_list(self, l):
        self.out(0, pd.LIST, [2*x for x in l])
''',
         x=40, y=40)                       -> id 0; writes doubler.pd_py
                                              and creates [doubler]
  2) Drive id 0's inlet with [list 1 2 3(  ->  outlet emits [2 4 6(

  3) To iterate on the code:
     pd_update_python_script(
         name='doubler', scripts_dir=<same dir as above>,
         code=<new code>)

     ⚠️ py4pd 1.2.3 caches every .pd_py in sys.modules and does NOT
     re-import on object re-creation. Re-creating the [doubler]
     object on the canvas (pd_clear_canvas + rebuild, or hand-delete
     in Pd) **still runs the OLD bytecode** -- you'll see your new
     source if a traceback fires, but the executing code is stale.

     The only reliable way to pick up the new file: **restart Pd**
     (close + reopen mcp_host.pd). After restart, the new .pd_py is
     imported fresh on first use of [<name>]. Tell the user this
     explicitly when they ask you to iterate -- pd_update_python_script
     writes the file, but Pd needs a restart to actually pick it up.

Environment prerequisites (warn the user if unmet -- you cannot check
this yourself but the symptoms below help diagnose):
  - Pd vanilla >= 0.55 (py4pd 1.2.3 uses pd_snprintf, absent in 0.54.x;
    "undefined symbol: pd_snprintf" / "couldn't create" = Pd too old).
  - py4pd's Deken build links against Python 3.14 (libpython3.14.so.1.0).
    If [<name>] silently fails to instantiate, libpython is likely
    missing -- direct the user to install Python 3.14.

If [<name>] creates an empty inert box (no error, no outlet activity),
the .pd_py file is most likely not on the declared path, or the host
patch lacks [declare -lib py4pd]. The tool response repeats both
requirements so you can quote them back to the user.

============================================================
SAFETY
============================================================
- Audio is LOUD by default. Always go through a [*~ 0.1] or smaller
  before [dac~] when prototyping, and only call pd_set_dsp(on=True)
  once the chain is connected end-to-end.
- pd_clear_canvas is destructive and resets ids to 0. Confirm with the
  user before calling it unless they just asked you to start over.

You are now initialized. Call pd_get_state to see what's on the canvas
and proceed.
"""
