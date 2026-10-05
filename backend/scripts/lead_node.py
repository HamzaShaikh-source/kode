"""The lead's own PC joins the project as a teammate.

Without this the Brain's computer only orchestrates, so the lead would have no tasks and no commits. This starts a normal kode-node agent for this PC
(own git and gh identity, own coding agents), pairs it with a one-time invite, and keeps its login in ~/kode-lead so a restart reuses it.
"""
import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _git(*a) -> str:
    try:
        return subprocess.run(["git", *a], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def lead_name() -> str:
    return _git("config", "--global", "user.name") or getpass.getuser() or "Lead"


async def ensure(base: str, key: str, log=print):
    """Start (or find) this PC's node. Returns the subprocess to stop on exit, or None if a node for this PC is already online."""
    from demo_fleet import Lead
    home = Path.home() / "kode-lead"
    home.mkdir(exist_ok=True)
    cfg, ws = home / "node.json", home / "workspace"
    ws.mkdir(exist_ok=True)
    first = lead_name().split()[0]
    async with Lead(base, key) as lead:
        if any(n.get("online") and n.get("same_host") and n.get("member_name", "").lower().startswith(first.lower()) for n in lead.state.get("nodes", [])):
            log(f"  lead PC is already connected as a teammate ({first})")
            return None
        cmd = [sys.executable, "-m", "node.agent", "--hub", base, "--name", f"{first}'s PC (Lead)", "--workspace", str(ws), "--config", str(cfg), "--allow-remote-start"]
        if not cfg.exists():
            r = await lead.call(t="create_invite", member=first, access="full")
            if not r.get("ok"):
                log("  could not create the lead PC invite: " + str(r.get("err")))
                return None
            cmd += ["--pair", r["code"]]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(120):
            n = next((x for x in lead.state.get("nodes", []) if x["name"] == f"{first}'s PC (Lead)"), None)
            if n and n["online"]:
                await lead.call(t="set_access", node=n["id"], access="full")
                log(f"  this PC joined as a teammate: {n['name']} (it can be given tasks and makes its own commits)")
                return proc
            await asyncio.sleep(0.25)
        log("  the lead PC node did not connect in 30s (it keeps retrying; check the Team page)")
        return proc
