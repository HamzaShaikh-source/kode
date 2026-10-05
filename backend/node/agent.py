"""kode-node: the agent on each teammate's machine. OUTBOUND-only WebSocket to the Brain (works behind NAT).

  python -m node.agent --hub http://127.0.0.1:8000 --pair 9F3A-0C11-B7D2 --name "Naman's laptop" --workspace D:\\code\\proj
  python -m node.agent --hub http://127.0.0.1:8000 --name "Naman's laptop"          # later runs reuse the saved pairing

It hosts provider sessions (PTYs), redacts secrets BEFORE output leaves the machine, answers terminal handshake queries
when nobody is viewing, collects git / plan / token data, and enforces the consent mode locally as well as on the hub.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.manifests import load_manifests, refresh_path, scan, scrubbed_env   # noqa: E402
from bridge.pty_session import PtySession                          # noqa: E402
from hub.protocol import MODES, is_terminal_reply, jdump, redact, valid_mode   # noqa: E402
from node.collectors import COLLECTORS, Activity, git_info          # noqa: E402
from hub.apiprofiles import validate_launch                       # noqa: E402
from node.ops import Ops                                          # noqa: E402

VERSION = "0.2"
LEVELS = ("ask", "copilot", "full")


class Hosted:
    def __init__(self, sid: str, provider: str, label: str, cwd: str, pty: PtySession):
        self.sid, self.provider, self.label, self.cwd, self.pty = sid, provider, label, cwd, pty
        self.mode, self.viewers = "watch", 0
        self.cols, self.rows = 100, 28
        self.started = time.time()
        self.activity = Activity()
        cls = COLLECTORS.get(provider)
        self.collector = cls(cwd, self.started) if cls else None
        self.last_meta = ""
        self.secrets: List[str] = []
        self.pump: Optional[asyncio.Task] = None
        self.exit_sent = False


class NodeAgent:
    def __init__(self, hub: str, name: str, config: Path, roots: List[str], allow_remote_start: bool, pair: str = "", accept_access: bool = True, access_limit: str = "full",
                 git_name: str = "", git_email: str = ""):
        u = hub.rstrip("/")
        self.http = u.replace("ws://", "http://").replace("wss://", "https://")
        self.ws_url = self.http.replace("http://", "ws://").replace("https://", "wss://") + "/hub/node"
        self.name, self.config_path, self.pair_code = name, config, pair
        self.provs: List[dict] = []
        self.pair_done = False
        self.hubs = [self.ws_url]                                         # the address we were given first, then any the Brain told us about
        self.hub_i = 0
        self.need_invite = False
        self.access_limit = access_limit if access_limit in LEVELS else "full"
        if not accept_access:
            self.access_limit = "ask"
        self.git_env = {}
        if git_name:                                                 # demo fleets only: real laptops use their own git config
            self.git_env = {"GIT_AUTHOR_NAME": git_name, "GIT_COMMITTER_NAME": git_name, "GIT_AUTHOR_EMAIL": git_email, "GIT_COMMITTER_EMAIL": git_email}
        self.roots = [str(Path(r).resolve()) for r in (roots or [os.getcwd()])]
        self.allow_remote_start = allow_remote_start
        self.ops = Ops(self.roots, self.git_env)
        self.manifests = load_manifests()
        self.sessions: Dict[str, Hosted] = {}
        self.cfg: Dict[str, str] = {}
        self.ws = None
        self.stop = asyncio.Event()
        self.log = lambda *a: print("[kode-node]", *a, flush=True)

    # ── config ──
    def load(self) -> None:
        try:
            self.cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            self.cfg = {}

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    # ── main loop ──
    async def run(self) -> None:
        self.load()
        for u in self.cfg.get("alt_hubs", []):
            w = u.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/hub/node"
            if w not in self.hubs:
                self.hubs.append(w)
        refresh_path()
        self.provs = await asyncio.to_thread(scan, self.manifests)
        delay, fails = 1.0, 0
        while not self.stop.is_set():
            try:
                # ping_interval: a half-open connection (Wi-Fi dropped, laptop slept) is noticed in ~35 s instead of "never"
                async with websockets.connect(self.hubs[self.hub_i % len(self.hubs)], max_size=2**22, ping_interval=15, ping_timeout=20, open_timeout=10) as ws:
                    self.ws = ws
                    ok = await self._handshake(ws)
                    if ok is None:
                        continue
                    if not ok:
                        return
                    delay, fails = 1.0, 0
                    tasks = [asyncio.create_task(self._reader(ws)), asyncio.create_task(self._hb(ws)), asyncio.create_task(self._collect(ws)), asyncio.create_task(self._rescan_loop())]
                    done, pend = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pend:
                        t.cancel()
                    for t in done:
                        t.exception()
                    if getattr(ws, "close_code", None) == 4409:                   # another agent for this machine took over
                        self.log("another K.O.D.E. agent started for this machine and took over; this one is stopping.")
                        self.stop.set()
                        return
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                fails += 1
                if fails % 3 == 0 and len(self.hubs) > 1:                     # the Brain's address may have changed (new hotspot IP): try the others
                    self.hub_i += 1
                self.log(f"hub unreachable ({type(e).__name__}); retrying in {delay:.0f}s" + (f" (next address: {self.hubs[self.hub_i % len(self.hubs)]})" if len(self.hubs) > 1 else ""))
            finally:
                self.ws = None
            if self.stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    def _hello(self) -> dict:
        provs = self.provs
        sess = [{"sid": h.sid, "provider": h.provider, "label": h.label, "cwd": h.cwd, "alive": h.pty.alive(),
                 "cols": h.cols, "rows": h.rows} for h in self.sessions.values()]
        return {"t": "hello", "node_id": self.cfg.get("node_id"), "token": self.cfg.get("token"), "name": self.name,
                "hostname": socket.gethostname(), "os": f"{platform.system()} {platform.release()}", "providers": provs,
                "roots": self.roots, "allow_remote_start": self.allow_remote_start, "version": VERSION, "sessions": sess,
                "access_accepted": self.access_limit}

    async def _handshake(self, ws) -> bool:
        want_pair = bool(self.pair_code) and not self.pair_done
        if want_pair and self.cfg.get("token"):
            self.log("A new invite code was given: joining again with it (the old saved registration is replaced).")
        if want_pair or not self.cfg.get("token"):
            if not self.pair_code:
                self.log("not paired: run with --pair <code> (create a code in the console: Add machine)")
                self.need_invite = True
                return False
            await ws.send(jdump(t="pair", code=self.pair_code, name=self.name, hostname=socket.gethostname(), os=platform.system()))
            r = json.loads(await ws.recv())
            if r.get("t") != "paired":
                self.log("pairing failed:", r.get("d", r))
                if self.cfg.get("token"):                         # the invite was bad but we were registered before: fall back to the saved login
                    self.log("Using the previous registration instead.")
                    self.pair_done = True
                    return None
                self.need_invite = True
                return False
            self.pair_done = True
            self.cfg = {"node_id": r["node_id"], "token": r["token"], "member_key": r["member_key"], "hub": self.http,
                        "member_name": r.get("member_name", ""), "access_accepted": "ask"}
            access = r.get("access", "ask")
            self.cfg["access_accepted"] = access if LEVELS.index(access) <= LEVELS.index(self.access_limit) else self.access_limit
            if access != "ask":                                     # joining with this invite IS the acceptance; the kill switch stays yours
                terms = {"copilot": "type into your provider sessions (co-pilot) without asking each time",
                         "full": "type into your sessions, start sessions and run git/test commands in your workspace without asking"}[access]
                self.log(f"Joined with the '{access}' access policy: the lead may {terms}. You can pull the kill switch at any time to withdraw it.")
            self.save()
            self.log(f"paired as {r['node_id']}")
        await ws.send(jdump(**self._hello()))
        r = json.loads(await ws.recv())
        if r.get("t") != "ready":
            self.log("rejected by hub:", r.get("d", r))
            if "authentication" in str(r.get("d", "")):
                self.cfg = {}                                   # token no longer valid (machine was removed): need a new pairing
                self.need_invite = True
                self.log("This machine was removed from the team or the Brain was reset. Ask your lead for a new invite link and run its command again.")
            return False
        alts = [str(u) for u in (r.get("alt_hubs") or []) if re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]{1,255}", str(u))][:6]
        if alts and alts != self.cfg.get("alt_hubs"):
            self.cfg["alt_hubs"] = alts
            self.save()
            for u in alts:
                w = u.replace("http://", "ws://").replace("https://", "wss://") + "/hub/node"
                if w not in self.hubs:
                    self.hubs.append(w)
        mk = self.cfg.get("member_key")
        self.log(f"connected to {self.http}  |  member console: {self.http}/console?member={mk}")
        for h in self.sessions.values():                         # push what we still host so the hub can rebuild its replay buffer
            await ws.send(jdump(t="out", sid=h.sid, d=h.pty.replay(), reset=True))
        return True

    async def _rescan_loop(self) -> None:
        """Re-check what is installed / signed in, so a tool the teammate sets up AFTER joining shows up on its own (no re-invite, no restart)."""
        every = float(os.environ.get("KODE_RESCAN_S", "15"))
        while True:
            await asyncio.sleep(every)
            await self.rescan()

    async def rescan(self) -> None:
        refresh_path()
        new = await asyncio.to_thread(scan, self.manifests)
        if json.dumps(new, sort_keys=True) != json.dumps(self.provs, sort_keys=True):
            self.provs = new
            await self._send(t="providers", providers=new)
            self.log("providers changed: " + ", ".join(f"{p['id']}{'' if p['available'] else ' (missing)'}" for p in new if p["available"]))

    async def _hb(self, ws) -> None:
        while True:
            await asyncio.sleep(5)
            await ws.send(jdump(t="hb"))

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                await self._on_hub(json.loads(raw))
            except Exception as e:
                self.log("bad hub message:", type(e).__name__, e)

    # ── commands from the hub ──
    async def _on_hub(self, m: dict) -> None:
        t, sid = m.get("t"), str(m.get("sid", ""))
        h = self.sessions.get(sid)
        if t == "start":
            await self._start(sid, str(m.get("provider", "")), str(m.get("cwd", "")), int(m.get("cols", 100)), int(m.get("rows", 28)),
                              m.get("env") or {}, [str(a) for a in (m.get("args") or [])], [str(x) for x in (m.get("secrets") or []) if len(str(x)) >= 8])
        elif t == "in" and h:
            actor = m.get("actor")
            if actor == "lead" and h.mode not in ("copilot", "takeover"):
                self.log(f"dropped lead input on {sid}: local mode is '{h.mode}'")     # local enforcement, independent of the hub
                return
            h.pty.write(str(m.get("d", ""))[:16384])
        elif t == "reply" and h and is_terminal_reply(str(m.get("d", ""))):      # terminal handshake answer from the elected viewer
            h.pty.write(m["d"])
        elif t == "resize" and h:
            h.cols, h.rows = max(20, min(int(m.get("cols", 80)), 500)), max(5, min(int(m.get("rows", 24)), 200))
            h.pty.resize(h.cols, h.rows)
        elif t == "mode" and h and valid_mode(m.get("mode")):
            h.mode = m["mode"]
        elif t == "viewers" and h:
            h.viewers = int(m.get("n", 0))
        elif t == "redraw" and h:                                   # nudge a TUI to repaint for a late-joining viewer
            h.pty.resize(h.cols + 1, h.rows)
            await asyncio.sleep(0.05)
            h.pty.resize(h.cols, h.rows)
        elif t == "stop" and h:
            h.pty.close()
        elif t == "op":
            asyncio.create_task(self._do_op(m))
        elif t == "rescan":
            asyncio.create_task(self.rescan())
        elif t == "config":
            self.allow_remote_start = bool(m.get("allow_remote_start"))

    async def _do_op(self, m: dict) -> None:
        res = await self.ops.run(str(m.get("name", "")), m.get("args") or {})
        await self._send(t="opres", id=m.get("id"), **{k: v for k, v in res.items() if k != "t"})

    def _resolve_cwd(self, want: str) -> Optional[str]:
        base = Path(self.roots[0])
        p = Path(want) if want else base
        if not p.is_absolute():
            p = base / p
        try:
            p = p.resolve()
        except OSError:
            return None
        for r in self.roots:                                         # sessions may only run inside the member's declared workspace roots
            try:
                p.relative_to(Path(r))
                p.mkdir(parents=True, exist_ok=True)
                return str(p)
            except ValueError:
                continue
        return None

    async def _start(self, sid: str, provider: str, cwd: str, cols: int, rows: int, extra_env=None, extra_args=None, secrets=None) -> None:
        m = self.manifests.get(provider)
        run_cwd = self._resolve_cwd(cwd)
        if not m or not m.available:
            await self._send(t="started", sid=sid, ok=False, err=f"provider '{provider}' not available on this machine")
            return
        if run_cwd is None:
            await self._send(t="started", sid=sid, ok=False, err="working directory is outside this machine's allowed workspace roots")
            return
        extra_env, extra_args = {str(k): str(v) for k, v in (extra_env or {}).items()}, list(extra_args or [])
        bad = validate_launch(extra_env, extra_args) if (extra_env or extra_args) else None
        if bad:                                                         # a hub may only inject the documented endpoint settings
            await self._send(t="started", sid=sid, ok=False, err=f"refused launch settings from the hub: {bad}")
            return
        try:
            env = scrubbed_env(m)
            env.update(extra_env)
            env.update(self.git_env)
            pty = PtySession.spawn(m.argv() + extra_args, env=env, cwd=run_cwd, cols=cols, rows=rows)
        except Exception as e:
            await self._send(t="started", sid=sid, ok=False, err=f"could not start: {e}")
            return
        h = Hosted(sid, provider, m.label, run_cwd, pty)
        h.cols, h.rows = cols, rows
        h.secrets = list(secrets or [])
        self.sessions[sid] = h
        h.pump = asyncio.create_task(self._pump(h))
        await self._send(t="started", sid=sid, ok=True, cwd=run_cwd)
        self.log(f"session {sid}: {m.label} in {run_cwd}")

    async def _pump(self, h: Hosted) -> None:
        while True:
            chunk = await h.pty.queue.get()
            if chunk is None:
                break
            h.activity.feed(len(chunk))
            if h.viewers == 0:                                       # nobody to answer the terminal handshake -> we do it
                if "\x1b[6n" in chunk:
                    h.pty.write("\x1b[1;1R")
                if re.search(r"\x1b\[0?c", chunk):
                    h.pty.write("\x1b[?1;2c")
            out = redact(chunk)
            for sec in h.secrets:                                       # the exact API key of this session's profile must never reach the console
                if sec in out:
                    out = out.replace(sec, "[REDACTED]")
            await self._send(t="out", sid=h.sid, d=out)
        h.exit_sent = True
        await self._send(t="exit", sid=h.sid, code=getattr(h.pty.proc, "exitstatus", None))

    async def _send(self, **msg) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(jdump(**msg))
            except Exception:
                pass

    # ── structured data ──
    async def _collect(self, ws) -> None:
        while True:
            await asyncio.sleep(4)
            for h in list(self.sessions.values()):
                meta: dict = {}
                if h.collector:
                    try:
                        meta.update(h.collector.poll())
                    except Exception as e:
                        self.log("collector error:", type(e).__name__)
                meta["files"] = await git_info(h.cwd)
                u = dict(meta.get("usage") or {})
                u.setdefault("source", "terminal")
                u.update({"bytes_out": h.activity.bytes_out, "active_seconds": round(h.activity.active_s)})
                meta["usage"] = u
                blob = json.dumps(meta, sort_keys=True, default=str)
                if blob != h.last_meta:
                    h.last_meta = blob
                    await self._send(t="meta", sid=h.sid, **meta)

    def shutdown(self) -> None:
        self.stop.set()
        for h in self.sessions.values():
            h.pty.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="K.O.D.E. node agent")
    ap.add_argument("--hub", default="http://127.0.0.1:8000")
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--pair", default="", help="one-time invite code from the console")
    ap.add_argument("--workspace", action="append", default=[], help="allowed working directory root (repeatable)")
    ap.add_argument("--allow-remote-start", action="store_true", help="let the lead start sessions here without asking")
    ap.add_argument("--accept-access", action="store_true", help="(kept for compatibility: joining with an invite is the acceptance)")
    ap.add_argument("--access-limit", default="full", choices=LEVELS, help="the most access this machine will ever grant the lead (default: full)")
    ap.add_argument("--git-name", default="", help="author name for commits made on this node (demo fleets; default: your own git config)")
    ap.add_argument("--git-email", default="")
    ap.add_argument("--config", default="")
    a = ap.parse_args()
    cfg = Path(a.config) if a.config else Path.home() / ".kode-node" / (re.sub(r"[^A-Za-z0-9_-]", "_", a.name) + ".json")
    agent = NodeAgent(a.hub, a.name, cfg, a.workspace, a.allow_remote_start, a.pair, True, a.access_limit, a.git_name, a.git_email)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        agent.shutdown()
    if agent.need_invite:
        sys.exit(3)                                              # start scripts do not restart-loop on this: only a new invite helps


if __name__ == "__main__":
    main()
