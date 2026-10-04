"""Demo fleet: starts N REAL kode-node processes on THIS PC, pairs them to the Brain, and starts real provider sessions.

They are genuinely separate processes talking the real protocol, but they share one computer, so the console badges them
"same PC as the Brain". Nothing here is scripted agent output: tiles show whatever the real providers print.

  python backend/scripts/demo_fleet.py [--base http://127.0.0.1:8000] [--nodes 3]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import websockets  # noqa: E402

PLAN = [
    ("Naman's laptop", ["claude", "shell"]),
    ("Nishitha's MacBook", ["hermes"]),
    ("Jyoti's desktop", ["codex", "opencode"]),
]
# Synthetic commit identities for the demo (real laptops simply use their own git config).
IDENT = {"Naman's laptop": ("Naman Raghav", "naman@bugged.dev"), "Nishitha's MacBook": ("Nishitha Lokesh", "nishitha@bugged.dev"),
         "Jyoti's desktop": ("Jyoti Rodrigrez", "jyoti@bugged.dev")}
LIVE_CMD = "for /l %i in (1,1,400) do @(echo build step %i of 400 ... ok & ping -n 3 127.0.0.1 >nul)\r"


def git_workspace(name: str) -> Path:
    ws = BACKEND / "demo_workspaces" / "".join(c for c in name.split("'")[0].lower() if c.isalnum())
    if not (ws / ".git").exists():
        ws.mkdir(parents=True, exist_ok=True)
        run = lambda *a: subprocess.run(["git", "-C", str(ws), *a], capture_output=True)
        run("init", "-q")
        run("config", "user.email", "demo@example.invalid"); run("config", "user.name", "Demo")
        (ws / "README.md").write_text(f"# {name} workspace\n\nA scratch repo for the K.O.D.E. demo.\n", encoding="utf-8")
        run("add", "."); run("commit", "-qm", "init")
    return ws


class Lead:
    def __init__(self, base, key):
        self.url = base.replace("http", "ws") + f"/hub/viewer?role=lead&key={key}"
        self.state, self.results, self.n = {}, {}, 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=2**22)
        self.task = asyncio.create_task(self._read())
        for _ in range(100):
            if self.state:
                break
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *a):
        self.task.cancel(); await self.ws.close()

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m["t"] == "state":
                self.state = m
            elif m["t"] == "result":
                self.results[m.get("ref")] = m

    async def call(self, **kw):
        self.n += 1
        ref = f"d{self.n}"
        await self.ws.send(json.dumps({**kw, "ref": ref}))
        for _ in range(200):
            if ref in self.results:
                return self.results.pop(ref)
            await asyncio.sleep(0.05)
        raise TimeoutError(kw.get("t"))

    def node_named(self, name):
        return next((n for n in self.state.get("nodes", []) if n["name"] == name), None)


async def member_type(base, member_key, sid, text):
    async with websockets.connect(base.replace("http", "ws") + f"/hub/viewer?role=member&key={member_key}") as ws:
        await ws.recv()
        await ws.send(json.dumps({"t": "sub", "sid": sid})); await asyncio.sleep(0.6)
        await ws.send(json.dumps({"t": "in", "sid": sid, "d": text})); await asyncio.sleep(0.8)


async def setup(base: str, key: str, count: int = 3, log=print):
    procs = []
    cfg_dir = BACKEND / ".demo_nodes"
    cfg_dir.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONIOENCODING": "utf-8"}
    async with Lead(base, key) as lead:
        for name, provs in PLAN[:count]:
            cfg = cfg_dir / (name.split("'")[0].lower() + ".json")
            ws = git_workspace(name)
            for attempt in (1, 2):
                cmd = [sys.executable, "-m", "node.agent", "--hub", base, "--name", name, "--workspace", str(ws), "--config", str(cfg), "--allow-remote-start",
                       "--git-name", IDENT[name][0], "--git-email", IDENT[name][1]]
                if not cfg.exists():
                    cmd += ["--pair", (await lead.call(t="create_invite", member=IDENT[name][0].split()[0], access="full"))["code"]]
                procs.append(subprocess.Popen(cmd, cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                for _ in range(120):
                    n = lead.node_named(name)
                    if n and n["online"]:
                        break
                    await asyncio.sleep(0.25)
                else:
                    log(f"  {name}: did not connect (attempt {attempt}); re-pairing")
                    procs[-1].terminate(); cfg.unlink(missing_ok=True)
                    continue
                break
            n = lead.node_named(name)
            if not n or not n["online"]:
                continue
            await lead.call(t="set_access", node=n["id"], access="full")        # the lead has total access to every demo laptop
            member_key = json.loads(cfg.read_text())["member_key"]
            have = {s["provider"] for s in lead.state.get("sessions", []) if s["node_id"] == n["id"] and s["state"] == "live"}
            for p in provs:
                if p in have or not any(x["id"] == p and x["available"] for x in n["providers"]):
                    continue
                r = await lead.call(t="start", node=n["id"], provider=p)
                log(f"  {name}: started {p} -> {'ok' if r.get('ok') else r.get('err')}")
                if p == "shell" and r.get("sid"):
                    await asyncio.sleep(1.5)
                    await member_type(base, member_key, r["sid"], LIVE_CMD)       # the MEMBER runs a long real command in their own shell
            log(f"  {name}: member console  {base}/console?member={member_key}")
    return procs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--nodes", type=int, default=3)
    a = ap.parse_args()
    key = (BACKEND / ".hub_key").read_text().strip()
    procs = asyncio.run(setup(a.base, key, a.nodes))
    print("fleet running; Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
