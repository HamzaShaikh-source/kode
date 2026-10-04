"""PtySession: run any CLI in a pseudo-terminal (Windows ConPTY via pywinpty; Unix via ptyprocess) and expose
async read/write/resize. Keeps a bounded ring buffer so a late viewer can be sent a replay."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

if sys.platform == "win32":
    from winpty import PtyProcess                      # type: ignore
else:                                                  # pragma: no cover - exercised on macOS/Linux nodes
    from ptyprocess import PtyProcess                  # type: ignore

RING_BYTES = 256 * 1024


class PtySession:
    def __init__(self, proc, loop: asyncio.AbstractEventLoop, argv: List[str]):
        self.proc, self.loop, self.argv = proc, loop, argv
        self.queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self.ring: Deque[str] = deque()
        self.ring_len = 0
        self.seq = 0
        self.started = time.time()
        self.closed = False
        self._t = threading.Thread(target=self._reader, daemon=True, name="pty-reader")
        self._t.start()

    @classmethod
    def spawn(cls, argv: List[str], *, env: Dict[str, str], cwd: str, cols: int = 80, rows: int = 24) -> "PtySession":
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        else:                                          # pragma: no cover
            proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        return cls(proc, loop, argv)

    # ── reader thread -> asyncio queue ──
    def _reader(self) -> None:
        try:
            while True:
                chunk = self.proc.read(4096)
                if chunk == "" or chunk is None:
                    if not self.proc.isalive():
                        break
                    continue
                self.loop.call_soon_threadsafe(self._push, chunk)
        except (EOFError, OSError):
            pass
        except Exception:                              # reader must never crash the server
            pass
        finally:
            self.loop.call_soon_threadsafe(self._push, None)

    def _push(self, chunk: Optional[str]) -> None:
        if chunk is not None:
            self.seq += 1
            self.ring.append(chunk)
            self.ring_len += len(chunk)
            while self.ring_len > RING_BYTES and self.ring:
                self.ring_len -= len(self.ring.popleft())
        self.queue.put_nowait(chunk)

    # ── API ──
    def replay(self) -> str:
        return "".join(self.ring)

    def write(self, data: str) -> None:
        if not self.closed and self.proc.isalive():
            self.proc.write(data)

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
        if not self.closed and self.proc.isalive():
            self.proc.setwinsize(rows, cols)

    def alive(self) -> bool:
        return (not self.closed) and self.proc.isalive()

    @property
    def pid(self) -> int:
        return getattr(self.proc, "pid", 0) or 0

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.proc.isalive():
                self.proc.terminate(force=True)
        except Exception:
            pass
