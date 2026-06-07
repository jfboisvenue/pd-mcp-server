"""
Pure FUDI atom builders -- the single source of truth for the wire format.

Each function maps a structured node description (the same fields the
``pd_create_*`` tools accept) to the exact list of atoms that must be sent
to Pd's ``[netreceive]`` to create that node. No socket, no state: pure
functions, trivially testable and replayable.

Two callers share these:
  * the creation tools in ``server.py`` (so a create == one builder call), and
  * the snapshot/restore replay (so re-rendering an IR re-emits byte-for-byte
    the same FUDI the original creation did).

The GUI argument vectors (bng/tgl/nbx/slider) are the long property lists Pd
expects for IEMgui objects; they are reproduced here verbatim from the
original inline ``server.py`` code so the wire output is unchanged.

``BUILDERS`` maps a node ``kind`` to its builder for replay dispatch: the
builder is called as ``BUILDERS[kind](**params)`` where ``params`` is exactly
the dict stored on the node in ``patch_state``.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Sequence

Atom = object  # str | int | float -- whatever FudiClient.format_message stringifies


def obj_atoms(type: str, args: Sequence[str], x: int, y: int) -> List[Atom]:
    """``[type args...]`` object box."""
    return ["obj", x, y, type, *args]


def msg_atoms(atoms: Sequence[str], x: int, y: int) -> List[Atom]:
    """``[atoms...(`` message box."""
    return ["msg", x, y, *atoms]


def comment_atoms(text: str, x: int, y: int) -> List[Atom]:
    """Canvas comment (text sent as a single escaped atom)."""
    return ["text", x, y, text]


def floatatom_atoms(width: int, min: float, max: float, x: int, y: int) -> List[Atom]:
    """``[floatatom]``. Trailing ``- - -`` = no send/receive/label."""
    return ["floatatom", x, y, width, min, max, 0, "-", "-", "-"]


def bng_atoms(size: int, x: int, y: int) -> List[Atom]:
    """``[bng]`` button.

    Arg vector: SIZE HOLD INTRRPT INIT SEND RECEIVE LABEL X_OFF Y_OFF
    FONT FONTSIZE BG FG LABEL_COLOR.
    """
    args = [size, 250, 50, 0, "empty", "empty", "empty",
            17, 7, 0, 10, "#fcfcfc", "#000000", "#000000"]
    return ["obj", x, y, "bng", *args]


def tgl_atoms(size: int, initial: bool, x: int, y: int) -> List[Atom]:
    """``[tgl]`` toggle.

    Arg vector: SIZE INIT SEND RECEIVE LABEL X_OFF Y_OFF FONT FONTSIZE
    BG FG LABEL_COLOR INIT_VAL DEFAULT.
    """
    init = 1 if initial else 0
    args = [size, 0, "empty", "empty", "empty",
            17, 7, 0, 10, "#fcfcfc", "#000000", "#000000", init, 1]
    return ["obj", x, y, "tgl", *args]


def nbx_atoms(width: int, min: float, max: float, x: int, y: int) -> List[Atom]:
    """``[nbx]`` number box.

    Arg vector: WIDTH HEIGHT MIN MAX LOG INIT SEND RECEIVE LABEL X_OFF
    Y_OFF FONT FONTSIZE BG FG LABEL_COLOR INIT_VAL LOG_HEIGHT.
    """
    args = [width, 14, min, max, 0, 0,
            "empty", "empty", "empty",
            0, -8, 0, 10, "#fcfcfc", "#000000", "#000000", 0, 256]
    return ["obj", x, y, "nbx", *args]


def slider_atoms(orientation: str, min: float, max: float, x: int, y: int) -> List[Atom]:
    """``[hsl]`` (horizontal) or ``[vsl]`` (vertical) slider.

    Arg vector: WIDTH HEIGHT MIN MAX LOG INIT SEND RECEIVE LABEL X_OFF
    Y_OFF FONT FONTSIZE BG FG LABEL_COLOR INIT_VAL STEADY.
    """
    kind = "hsl" if orientation == "horizontal" else "vsl"
    w, h = (128, 15) if kind == "hsl" else (15, 128)
    args = [w, h, min, max, 0, 0,
            "empty", "empty", "empty",
            0, -8, 0, 10, "#fcfcfc", "#000000", "#000000", 0, 1]
    return ["obj", x, y, kind, *args]


def py4pd_atoms(name: str, x: int, y: int) -> List[Atom]:
    """A py4pd class object box ``[name]`` (the class autoregisters)."""
    return ["obj", x, y, name]


def connect_atoms(src: int, src_outlet: int, dst: int, dst_inlet: int) -> List[Atom]:
    """A ``connect`` editing message between two creation ids."""
    return ["connect", src, src_outlet, dst, dst_inlet]


# Dispatch table for replay: kind -> builder. Sliders share one builder and
# recover orientation from the stored params, so both kinds point to it.
BUILDERS: Dict[str, Callable[..., List[Atom]]] = {
    "obj": obj_atoms,
    "msg": msg_atoms,
    "comment": comment_atoms,
    "floatatom": floatatom_atoms,
    "bng": bng_atoms,
    "tgl": tgl_atoms,
    "nbx": nbx_atoms,
    "hsl": slider_atoms,
    "vsl": slider_atoms,
    "py4pd": py4pd_atoms,
}


def atoms_for(kind: str, params: dict) -> List[Atom]:
    """Build the FUDI atoms for a stored node (``kind`` + ``params``)."""
    try:
        builder = BUILDERS[kind]
    except KeyError as exc:
        raise ValueError(f"no FUDI builder for node kind {kind!r}") from exc
    return builder(**params)
