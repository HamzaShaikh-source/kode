"""P0 spike: stream a real provider CLI session from this machine to a browser xterm tile.

SECURITY (this endpoint starts programs on the host, so it is locked down):
  * OFF unless KODE_ENABLE_BRIDGE=1
  * page + websocket only for loopback clients
  * websocket needs the per-start token embedded in the served page, AND a same-origin Origin header if one is sent
  * the browser sends only a provider ID; the command comes from a server-side manifest allowlist
  * sessions run with a scrubbed environment (no exported API keys)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from bridge.manifests import load_manifests, scrubbed_env
from bridge.pty_session import PtySession

log = logging.getLogger("kode.bridge")
router = APIRouter()
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


class BridgeState:
    def __init__(self, workdir: Path):
        self.token = secrets.token_urlsafe(24)
        self.manifests = load_manifests()
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, PtySession] = {}


def _bridge(request_or_ws) -> BridgeState:
    b = getattr(request_or_ws.app.state, "bridge", None)
    if b is None:
        raise HTTPException(404, "terminal bridge is disabled (set KODE_ENABLE_BRIDGE=1)")
    return b


@router.get("/spike", response_class=HTMLResponse, include_in_schema=False)
async def spike_page(request: Request):
    b = _bridge(request)
    if request.client and request.client.host not in LOOPBACK:
        raise HTTPException(403, "loopback only")
    html = (STATIC / "spike.html").read_text(encoding="utf-8").replace("__TOKEN__", b.token)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/spike/providers", include_in_schema=False)
async def spike_providers(request: Request, token: str = ""):
    b = _bridge(request)
    if not hmac.compare_digest(token, b.token):
        raise HTTPException(403, "bad token")
    return [m.public() for m in b.manifests.values()]


@router.websocket("/spike/ws")
async def spike_ws(ws: WebSocket, provider: str = "", token: str = "", cols: int = 80, rows: int = 24):
    b = getattr(ws.app.state, "bridge", None)
    origin, host = ws.headers.get("origin"), ws.headers.get("host", "")
    if (b is None or not hmac.compare_digest(token, b.token)
            or (ws.client and ws.client.host not in LOOPBACK)
            or (origin and urlparse(origin).netloc != host)):
        await ws.close(code=4403)                          # unauthenticated / cross-origin: never accept
        return
    m = b.manifests.get(provider)
    if not m or not m.available:
        await ws.close(code=4404)
        return
    await ws.accept()
    try:
        sess = PtySession.spawn(m.argv(), env=scrubbed_env(m), cwd=str(b.workdir), cols=cols, rows=rows)
    except Exception as e:                                 # spawn failures are reported, not hidden
        await ws.send_text(json.dumps({"t": "error", "d": f"could not start {m.id}: {e}"}))
        await ws.close()
        return
    sid = secrets.token_hex(4)
    b.sessions[sid] = sess
    log.info("bridge session %s started: %s pid=%s", sid, m.id, sess.pid)
    await ws.send_text(json.dumps({"t": "hello", "provider": m.id, "label": m.label, "pid": sess.pid, "sid": sid}))

    async def pump_out():
        while True:
            chunk = await sess.queue.get()
            if chunk is None:
                await ws.send_text(json.dumps({"t": "exit"}))
                return
            await ws.send_text(json.dumps({"t": "out", "d": chunk, "seq": sess.seq}))

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "in" and isinstance(msg.get("d"), str):
                sess.write(msg["d"])
            elif msg.get("t") == "resize":
                sess.resize(msg.get("cols", 80), msg.get("rows", 24))
            elif msg.get("t") == "ping":
                await ws.send_text(json.dumps({"t": "pong", "n": msg.get("n")}))
            if out_task.done():
                break
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    finally:
        out_task.cancel()
        sess.close()
        b.sessions.pop(sid, None)
        log.info("bridge session %s closed", sid)
