"""
Server-side mirror of the Pd canvas object list.

Pure Data identifies objects on a canvas by their *creation index*
(0-based, in order of creation). The ``connect`` / ``disconnect`` editing
messages reference those indices. To let an agent connect objects by id,
we mirror Pd's indexing here: every object we create increments a counter,
and that counter value IS the object id we hand back. ``clear`` resets it.

This stays perfectly in sync with Pd as long as we only ever *append*
objects or *clear* everything -- which matches Pd vanilla's capabilities
(vanilla has no "delete single object" editing message). If a user edits
the canvas by hand, ``resync_to`` lets them realign the counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class PdObject:
    index: int
    kind: str          # "obj" | "msg" | "comment" | "floatatom" | "bng" | "tgl" | "nbx" | "hsl" | "vsl"
    text: str          # human-readable summary, e.g. "osc~ 440"


@dataclass
class PatchState:
    """Tracks created objects so connect/disconnect can use stable ids."""

    _next_index: int = 0
    objects: Dict[int, PdObject] = field(default_factory=dict)
    initialized: bool = False

    # -- init gate ------------------------------------------------------------

    def mark_initialized(self) -> None:
        """Record that the agent has read the init guide. Other tools gate on this."""
        self.initialized = True

    # -- creation-index mirror ------------------------------------------------

    def add(self, kind: str, text: str) -> int:
        """Register a newly created object and return its Pd index/id."""
        idx = self._next_index
        self.objects[idx] = PdObject(index=idx, kind=kind, text=text)
        self._next_index += 1
        return idx

    def clear(self) -> None:
        """Mirror a Pd ``clear``: drop everything and reset indexing."""
        self.objects.clear()
        self._next_index = 0

    def resync_to(self, next_index: int) -> None:
        """Realign the counter after the user hand-edits the canvas.

        ``next_index`` is the id that Pd will assign to the *next* object
        created. The mirror's object list is dropped because we can no
        longer trust prior text labels to match Pd's actual canvas.
        """
        if next_index < 0:
            raise ValueError("next_index must be >= 0")
        self.objects.clear()
        self._next_index = next_index

    def next_index(self) -> int:
        return self._next_index

    def exists(self, index: int) -> bool:
        return index in self.objects

    def count(self) -> int:
        return len(self.objects)

    def as_list(self) -> List[dict]:
        return [
            {"id": o.index, "kind": o.kind, "text": o.text}
            for o in sorted(self.objects.values(), key=lambda x: x.index)
        ]
