"""Test kit: a real Hub (uvicorn thread) + real kode-node subprocesses + an async console client."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

from app import create_app
from tests.conftest import make_settings

BACKEND = Path(__file__).resolve().parents[1]


class HubServer:
    def __init__(self, tmp: Path, port: int = 0):
        self.tmp = tmp
        self.settings = make_settings(tmp, kode_enable_hub=True, kode_hub_db=str(tmp / "hub.db"), kode_hub_key_file=str(tmp / "key"))
        self.nodes = []
        self._port = port
        self._boot()

    def _boot(self):
        self.app = create_app(self.settings)
        if not self._port:
            sk = socket.socket(); sk.bind(("127.0.0.1", 0)); self._port = sk.getsockname()[1]; sk.close()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self._port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def port(self):
        return self._port

    def _wait_up(self):
        for _ in range(150):
            try:
                if httpx.get(self.base + "/health").status_code == 200 and getattr(self.app.state, "hub", None):
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("hub did not start")

    def __enter__(self):
        self.thread.start()
        self._wait_up()
        return self

    def stop_hub(self):
        self.server.should_exit = True
        self.thread.join(10)

    def restart_hub(self):
        """Bring the Brain back on the SAME port with the SAME database and lead key (machines keep running)."""
        self.stop_hub()
        self._boot()
        self.thread.start()
        self._wait_up()

    def __exit__(self, *a):
        for p in self.nodes:
            p.terminate()
        for p in self.nodes:
            try:
                p.wait(5)
            except Exception:
                p.kill()
        self.stop_hub()

    @property
    def base(self):
        return f"http://127.0.0.1:{self._port}"

    @property
    def hub(self):
        return self.app.state.hub

    @property
    def lead_key(self):
        return self.hub.lead_key

    def spawn_node(self, name, code="", remote_start=True, workspace=None, config=None, accept_access=False, git=None):
        ws = workspace or (self.tmp / f"ws_{name}")
        ws.mkdir(exist_ok=True)
        cfg = config or (self.tmp / f"cfg_{name}.json")
        cmd = [sys.executable, "-m", "node.agent", "--hub", self.base, "--name", name, "--workspace", str(ws), "--config", str(cfg)]
        if code:
            cmd += ["--pair", code]
        if remote_start:
            cmd += ["--allow-remote-start"]
        if not accept_access:
            cmd += ["--access-limit", "ask"]
        if git:
            cmd += ["--git-name", git[0], "--git-email", git[1]]
        env = {**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONIOENCODING": "utf-8", "KODE_PROVIDER_HOME": str(self.tmp / "home")}
        p = subprocess.Popen(cmd, cwd=str(BACKEND), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.nodes.append(p)
        return p, cfg


class Console:
    def __init__(self, base, role, key):
        self.url = base.replace("http", "ws") + f"/hub/viewer?role={role}&key={key}"
        self.state, self.out, self.msgs, self.results = {}, {}, [], {}
        self.orch = {}
        self.projects = {}
        self.ref = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=2**22)
        self.task = asyncio.create_task(self._read())
        await until(lambda: self.state, 5)                       # first snapshot always arrives before use
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await self.ws.close()

    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                t = m.get("t")
                if t == "state":
                    self.state = m
                elif t == "out":
                    self.out[m["sid"]] = self.out.get(m["sid"], "") + m["d"]
                elif t == "replay":
                    self.out[m["sid"]] = m["d"]
                elif t == "result":
                    self.results[m.get("ref")] = m
                elif t == "orch":
                    self.orch = m
                elif t == "projects":
                    self.projects = {p["id"]: p for p in m.get("projects", [])}
                else:
                    self.msgs.append(m)
        except Exception:
            pass

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    async def call(self, timeout=10, **kw):
        self.ref += 1
        ref = f"c{self.ref}"
        await self.send(ref=ref, **kw)
        await until(lambda: ref in self.results, timeout)
        return self.results.pop(ref)

    def sessions(self):
        return {s["sid"]: s for s in self.state.get("sessions", [])}

    def nodes(self):
        return {n["name"]: n for n in self.state.get("nodes", [])}


async def until(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        await asyncio.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")
