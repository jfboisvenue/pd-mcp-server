"""
A tiny mock of Pd's [netreceive]: a TCP server that accepts one client,
reads FUDI text, and splits it into messages on unescaped ';'. Used to test
the wire format without a running Pure Data.
"""

from __future__ import annotations

import socket
import threading
from typing import List


def _split_fudi(buffer: str) -> List[str]:
    """Split a FUDI stream into messages on unescaped semicolons."""
    messages: List[str] = []
    current: List[str] = []
    escaped = False
    for ch in buffer:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            current.append(ch)
            escaped = True
        elif ch == ";":
            messages.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    return messages


class MockPd:
    """Context-managed mock Pd listener.

    Usage:
        with MockPd() as pd:
            client = FudiClient(port=pd.port)
            ...
            pd.wait_for(n_messages)
            assert pd.messages == [...]
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, 0))
        self._server.listen(1)
        self.host, self.port = self._server.getsockname()
        self.messages: List[str] = []
        self._buffer = ""
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "MockPd":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        try:
            self._server.close()
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            self._server.settimeout(5.0)
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(0.5)
            while not self._done.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._buffer += chunk.decode("utf-8")
                self.messages = _split_fudi(self._buffer)

    def wait_for(self, n: int, timeout: float = 3.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(_split_fudi(self._buffer)) >= n:
                break
            time.sleep(0.02)
        self.messages = _split_fudi(self._buffer)
