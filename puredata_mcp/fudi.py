"""
FUDI transport for Pure Data.

FUDI is Pd's native message protocol. A message is a sequence of atoms
separated by whitespace and terminated by an (unescaped) semicolon ``;``.
This module speaks FUDI over a persistent TCP connection to a running
``[netreceive]`` object in Pure Data. No externals, no OSC, no daemon.

Design notes
------------
* Atoms that contain whitespace or FUDI control characters (``,`` ``;``
  ``\\``) are escaped per-character so the Pd parser keeps them as a single
  atom. Callers therefore pass *lists of atoms* and never have to think
  about quoting.
* The client is intentionally tiny and synchronous on the socket level
  (one short write per call); the MCP layer wraps it in async tools.
"""

from __future__ import annotations

import socket
import threading
from typing import List, Sequence

# Characters that must be backslash-escaped inside a single FUDI atom.
_FUDI_SPECIAL = {" ", "\t", "\n", "\r", ",", ";", "\\"}


def escape_atom(atom: str) -> str:
    """Escape a single FUDI atom so Pd parses it as one atom.

    Args:
        atom: Raw atom text (e.g. ``"osc~"``, ``"440"``, ``"hello world"``).

    Returns:
        The atom with FUDI-special characters backslash-escaped.
    """
    out: List[str] = []
    for ch in str(atom):
        if ch in _FUDI_SPECIAL:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def format_message(atoms: Sequence[object]) -> bytes:
    """Format a list of atoms into a single FUDI message terminated by ``;``.

    Args:
        atoms: Ordered atoms. Each is stringified and escaped individually.

    Returns:
        UTF-8 bytes ending in ``";\\n"`` ready to write to the socket.
    """
    parts = [escape_atom(a) for a in atoms]
    return (" ".join(parts) + ";\n").encode("utf-8")


class FudiError(RuntimeError):
    """Raised when the FUDI socket cannot connect or send."""


class FudiClient:
    """Persistent TCP FUDI client to a Pd ``[netreceive]`` listener.

    The connection is lazy: it is opened on first send and transparently
    re-opened if Pd was restarted. All public methods are thread-safe.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3000,
                 timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # -- connection management ------------------------------------------------

    def _ensure_connected(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
            # Small writes should go out immediately.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            raise FudiError(
                f"Could not connect to Pure Data at {self.host}:{self.port}. "
                f"Is Pd running with the host patch (netreceive {self.port}) "
                f"open? Underlying error: {exc}"
            ) from exc
        self._sock = sock
        return sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    # -- sending --------------------------------------------------------------

    def send_atoms(self, atoms: Sequence[object]) -> None:
        """Send one FUDI message. Reconnects once on a broken pipe."""
        payload = format_message(atoms)
        with self._lock:
            try:
                self._ensure_connected().sendall(payload)
            except OSError:
                # Pd may have been restarted: drop and retry once.
                if self._sock is not None:
                    try:
                        self._sock.close()
                    finally:
                        self._sock = None
                try:
                    self._ensure_connected().sendall(payload)
                except OSError as exc:
                    raise FudiError(
                        f"Lost connection to Pure Data while sending "
                        f"{atoms!r}: {exc}"
                    ) from exc

    def send_raw(self, text: str) -> None:
        """Send an already-formatted FUDI line (without trailing ``;``)."""
        with self._lock:
            data = (text.rstrip(";\n") + ";\n").encode("utf-8")
            self._ensure_connected().sendall(data)
