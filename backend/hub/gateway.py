"""The Brain (hub): pairs machines, tracks their provider sessions, relays live terminals to the console,
and enforces the consent ladder. The MEMBER owns control grants; the lead can only ask.

Endpoints: WS /hub/node (machines), WS /hub/viewer (console), REST /hub/state /hub/audit /hub/usage (lead key).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import re
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from hub.protocol import (MODE_RANK, MODES, RING_CHARS, ROLE_LEAD, ROLE_MEMBER, TAKEOVER_MAX_MIN, clip, is_terminal_reply,
                          jdump, now_ms, redact, valid_mode)
from hub.store import ACCESS_LEVELS, HubStore
from hub import apiprofiles
from hub.orchestrator import Orchestrator
from hub.project import ProjectEngine

log = logging.getLogger("kode.hub")
router = APIRouter()
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
MAX_FRAME = 64 * 1024


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]")
# Phrases providers print when they are blocked on a human. Heuristic: matched on the last screenful, only while output is quiet.
_NEEDS = re.compile(r"(\(y/n\)|\[y/N\]|\[Y/n\]|allow once|always allow|permission required|do you want to proceed|press enter to continue|"
                    r"do you trust the contents|approve this|waiting for (your )?(approval|confirmation)|\bconfirm\b.{0,20}\?)", re.I)


class Session:
    def __init__(self, sid: str, node_id: str, provider: str, label: str, cwd: str, cols: int, rows: int):
        self.sid, self.node_id, self.provider, self.label, self.cwd = sid, node_id, provider, label, cwd
        self.cols, self.rows = cols, rows
        self.state = "starting"                    # starting | live | exited | error | offline
        self.mode, self.mode_expires, self.mode_by = "watch", 0.0, ""
        self.created = time.time()
        self.ring: Deque[str] = deque()
        self.ring_len, self.seq, self.truncated = 0, 0, False
        self.bytes_out, self.last_out = 0, 0.0
        self.profile_id, self.profile_name, self.model = "", "", ""
        self.price_in, self.price_out = 0.0, 0.0
        self.tail = ""
        self.printed = 0
        self.prompt: Optional[tuple] = None                      # (phrase, printed-char offset where it ended)
        self.todos: List[dict] = []
        self.files: dict = {}
        self.usage: dict = {}
        self.activity_text, self.exit_code, self.error = "", None, ""
        self.viewers_n = 0
        self.responder = ""                       # id of the ONE viewer allowed to forward terminal handshake replies
        self.audit_acc, self.audit_last = "", 0.0

    def push(self, chunk: str) -> None:
        self.seq += 1
        self.ring.append(chunk)
        self.ring_len += len(chunk)
        while self.ring_len > RING_CHARS and len(self.ring) > 1:
            self.ring_len -= len(self.ring.popleft())
            self.truncated = True
        self.bytes_out += len(chunk)
        self.last_out = time.time()
        clean = _ANSI.sub("", chunk)
        ctx = self.tail[-60:] + clean                              # a phrase can straddle two chunks
        self.tail = (self.tail + clean)[-1200:]
        self.printed += len(clean)
        m = None
        for m in _NEEDS.finditer(ctx):
            pass
        if m:
            self.prompt = (m.group(0).strip()[:40], self.printed - (len(ctx) - m.end()))

    def needs_input(self) -> str:
        """The matched phrase if the session looks blocked on a human: a prompt-like phrase was printed, little text came after it,
        and the session has been quiet for 2.5s+ (so it is waiting, not still streaming)."""
        if self.state != "live" or not self.prompt:
            return ""
        quiet = time.time() - self.last_out
        if quiet < 2.5 or quiet > 900 or self.printed - self.prompt[1] > 160:
            return ""
        return self.prompt[0]

    def cost(self) -> Optional[float]:
        """ESTIMATE from the user's own per-million-token prices; None when tokens or prices are unknown."""
        u = self.usage or {}
        if not (self.price_in or self.price_out) or not u.get("source") or u.get("source") == "terminal":
            return None
        return round((u.get("input_tokens", 0) * self.price_in + u.get("output_tokens", 0) * self.price_out) / 1e6, 4)

    def public(self) -> dict:
        working = self.state == "live" and (time.time() - self.last_out) < 2.5
        return {"sid": self.sid, "node_id": self.node_id, "provider": self.provider, "label": self.label, "cwd": self.cwd,
                "state": self.state, "activity": "working" if working else ("idle" if self.state == "live" else self.state),
                "mode": self.mode, "mode_expires": self.mode_expires, "mode_by": self.mode_by, "created": self.created,
                "todos": self.todos, "files": self.files, "usage": self.usage, "bytes_out": self.bytes_out,
                "activity_text": self.activity_text, "exit_code": self.exit_code, "error": self.error,
                "viewers": self.viewers_n, "cols": self.cols, "rows": self.rows,
                "profile": {"id": self.profile_id, "name": self.profile_name, "model": self.model} if self.profile_id else None,
                "needs_input": self.needs_input(), "cost_estimate": self.cost()}


class NodeConn:
    def __init__(self, node_id: str, name: str, hostname: str, os_name: str, ws: Optional[WebSocket] = None):
        self.id, self.name, self.hostname, self.os, self.ws = node_id, name, hostname, os_name, ws
        self.providers: List[dict] = []
        self.roots: List[str] = []
        self.allow_remote_start = False
        self.online = ws is not None
        self.last_seen = time.time()
        self.sessions: Set[str] = set()
        self.version = ""
        self.member_name, self.access = "", "ask"
        self.accepted = "ask"
        self.info: dict = {}

    def public(self, hub_host: str) -> dict:
        return {"id": self.id, "name": self.name, "hostname": self.hostname, "os": self.os, "online": self.online,
                "providers": self.providers, "roots": self.roots, "allow_remote_start": self.allow_remote_start,
                "sessions": sorted(self.sessions), "last_seen": self.last_seen,
                "same_host": self.hostname.lower() == hub_host.lower(), "version": self.version,
                "member_name": self.member_name or self.name, "access": self.access, "info": self.info}


class Viewer:
    def __init__(self, ws: WebSocket, role: str, node_id: str = ""):
        self.ws, self.role, self.node_id = ws, role, node_id
        self.subs: Set[str] = set()
        self.id = secrets.token_hex(3)

    async def send(self, **kw: Any) -> bool:
        try:
            await self.ws.send_text(jdump(**kw))
            return True
        except Exception:
            return False


def lv_rank(a: str) -> int:
    return ACCESS_LEVELS.index(a) if a in ACCESS_LEVELS else 0


class Hub:
    def __init__(self, store: HubStore, lead_key: str):
        self.store, self.lead_key = store, lead_key
        self.hostname = socket.gethostname()
        self.nodes: Dict[str, NodeConn] = {}
        self.sessions: Dict[str, Session] = {}
        self.viewers: Set[Viewer] = set()
        self.requests: Dict[str, dict] = {}
        self.events: Deque[dict] = deque(maxlen=300)
        self._dirty = False
        self._fails: Dict[str, Deque[float]] = {}                # client ip -> recent auth failures (pairing / keys)
        self.orch = Orchestrator(self)
        self.projects = ProjectEngine(self)
        self._ops: Dict[str, asyncio.Future] = {}
        self._proj_task: Optional[asyncio.Task] = None
        self._orch_task: Optional[asyncio.Task] = None
        self._task: Optional[asyncio.Task] = None
        self._bg: List[asyncio.Task] = []
        self.projects.load()
        for n in store.list_nodes():                              # known machines show as offline until they reconnect
            c = NodeConn(n["id"], n["name"], n["hostname"], n["os"])
            c.online, c.last_seen = False, n["last_seen"] or 0
            c.member_name, c.access = n.get("member_name") or "", n.get("access") or "ask"
            self.nodes[n["id"]] = c

    # ───────────── lifecycle ─────────────
    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._bg = [asyncio.create_task(self._ticker())]

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        if self.orch.task and not self.orch.task.done():
            self.orch.task.cancel()

    async def _ticker(self) -> None:
        last_usage = 0.0
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for n in list(self.nodes.values()):                    # heartbeat timeout
                if n.online and now - n.last_seen > 25:
                    await self._node_offline(n, "heartbeat timeout")
            for s in list(self.sessions.values()):                 # takeover / grant expiry
                if s.mode_expires and now > s.mode_expires and s.mode != "watch":
                    await self._set_mode(s, "watch", "system", note="grant expired")
            if self.viewers:
                self.mark_dirty()                                  # refresh working/idle indicators
            if now - last_usage > 30:
                last_usage = now
                for s in self.sessions.values():
                    if s.usage.get("input_tokens") or s.usage.get("output_tokens"):
                        self.store.add_usage(s.node_id, s.sid, s.provider, s.usage)

    # ───────────── state out ─────────────
    def mark_dirty(self) -> None:
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        await asyncio.sleep(0.08)
        self._dirty = False
        for v in list(self.viewers):
            if not await v.send(t="state", **self.public_state(v)):
                self.viewers.discard(v)

    def push_orch(self) -> None:
        """Debounced push of the orchestrator's conversation/state to every lead console."""
        if self._orch_task is None or self._orch_task.done():
            self._orch_task = asyncio.create_task(self._flush_orch())

    async def _flush_orch(self) -> None:
        await asyncio.sleep(0.05)
        for v in list(self.viewers):
            if v.role == ROLE_LEAD and not await v.send(t="orch", **self.orch.public()):
                self.viewers.discard(v)

    async def suggest_to_owner(self, s: "Session", text: str) -> None:
        self.event("suggestion", f"suggestion for {s.label}: {clip(text, 120)}", s.node_id, s.sid, "orchestrator")
        for x in list(self.viewers):
            if x.role == ROLE_MEMBER and x.node_id == s.node_id:
                await x.send(t="suggestion", sid=s.sid, text=text, label=s.label)

    async def request_start(self, n: "NodeConn", provider: str, cwd: str, profile_id: str = "", model: str = "") -> str:
        rid = "r_" + secrets.token_hex(3)
        self.requests[rid] = {"rid": rid, "kind": "start", "node_id": n.id, "provider": provider, "cwd": cwd, "by": ROLE_LEAD, "profile_id": profile_id,
                              "model": model, "created": now_ms(), "label": next((p.get("label") for p in n.providers if p.get("id") == provider), provider)}
        self.event("request", f"lead asks to start {provider} on {n.name}", n.id, "", ROLE_LEAD)
        return rid

    def net_info(self) -> dict:
        """How teammates can reach this Brain: bind address, LAN addresses, and the URL the lead chose for invites."""
        import os
        bind = os.environ.get("KODE_BIND_HOST", "127.0.0.1")
        port = os.environ.get("KODE_PORT", "8000")
        lan = []
        if bind not in ("127.0.0.1", "localhost", "::1"):
            try:
                for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    ip = sa[0]
                    if not ip.startswith("127.") and not ip.startswith("169.254.") and f"http://{ip}:{port}" not in lan:
                        lan.append(f"http://{ip}:{port}")
            except OSError:
                pass
        public = os.environ.get("KODE_PUBLIC_URL", "").rstrip("/")
        team = self.store.get_setting("team_url") or public or (lan[0] if lan else "")
        return {"bind": bind, "port": port, "lan_urls": lan, "public_url": public, "team_url": team, "reachable": bool(team)}

    def public_state(self, v: Viewer) -> dict:
        lead = v.role == ROLE_LEAD
        nodes = [n.public(self.hostname) for n in self.nodes.values() if lead or n.id == v.node_id]
        sess = [s.public() for s in self.sessions.values() if lead or s.node_id == v.node_id]
        reqs = [r for r in self.requests.values() if lead or r["node_id"] == v.node_id]
        evs = [e for e in list(self.events)[-60:] if lead or e.get("node") == v.node_id]
        return {"role": v.role, "node_id": v.node_id, "hub_host": self.hostname, "nodes": nodes, "sessions": sess,
                "requests": reqs, "events": evs, "modes": list(MODES), "ts": now_ms()}

    def event(self, kind: str, text: str, node: str = "", sid: str = "", actor: str = "system", audit: bool = True) -> None:
        e = {"ts": now_ms(), "kind": kind, "text": text, "node": node, "sid": sid, "actor": actor}
        self.events.append(e)
        if audit:
            self.store.audit(actor, kind, node, sid, text)
        self.mark_dirty()

    async def _to_viewers(self, target: str, **msg: Any) -> None:
        """Send msg to every viewer subscribed to session `target` (msg carries its own 'sid' field)."""
        for v in list(self.viewers):
            if target in v.subs and not await v.send(**msg):
                self.viewers.discard(v)

    # ───────────── brute-force guard ─────────────
    def _blocked(self, ws: WebSocket) -> bool:
        return self.ip_blocked(ws.client.host if ws.client else "?")

    def ip_blocked(self, ip: str) -> bool:
        q = self._fails.setdefault(ip, deque(maxlen=50))
        now = time.time()
        while q and now - q[0] > 300:
            q.popleft()
        return len(q) >= 8                                         # 8 failures in 5 minutes -> locked out for the rest of the window

    def _fail(self, ws: WebSocket, what: str) -> None:
        self.ip_fail(ws.client.host if ws.client else "?", what)

    def ip_fail(self, ip: str, what: str) -> None:
        self._fails.setdefault(ip, deque(maxlen=50)).append(time.time())
        self.store.audit("system", "auth_fail", "", "", f"{what} from {ip}")

    # ───────────── node side ─────────────
    async def handle_node(self, ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin:                                                  # nodes are not browsers: refuse cross-site pages
            await ws.close(code=4403)
            return
        if self._blocked(ws):
            await ws.close(code=4429)
            return
        await ws.accept()
        node: Optional[NodeConn] = None
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            if first.get("t") == "pair":
                inv = self.store.consume_invite(str(first.get("code", "")))
                if not inv:
                    self._fail(ws, "bad pairing code")
                    await ws.send_text(jdump(t="error", d="invalid, used or expired invite code"))
                    await ws.close(code=4401)
                    return
                mname = inv["member_name"] or clip(first.get("name", "machine"), 60)
                made = self.store.create_node(clip(first.get("name") or mname, 60), clip(first.get("hostname", "?"), 80), clip(first.get("os", "?"), 40), mname, inv["access"])
                await ws.send_text(jdump(t="paired", member_name=mname, access=inv["access"], **made))
                self.event("pair", f"{mname}'s machine paired (access policy: {inv['access']})", made["node_id"], actor="system")
                first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
                first.update({"node_id": made["node_id"], "token": made["token"]})
            row = self.store.verify_node(str(first.get("node_id", "")), str(first.get("token", ""))) if first.get("t") == "hello" else None
            if first.get("t") != "hello" or not row:
                self._fail(ws, "bad node credentials")
                await ws.send_text(jdump(t="error", d="authentication failed"))
                await ws.close(code=4401)
                return
            node = self._register_node(ws, first)
            node.member_name = row["member_name"] or node.name
            want, ok = row["access"] or "ask", first.get("access_accepted", "ask")
            lv = ACCESS_LEVELS
            node.access = want if lv.index(ok if ok in lv else "ask") >= lv.index(want if want in lv else "ask") else (ok if ok in lv else "ask")    # the lower of what the lead set and what this machine allows
            node.accepted = ok if ok in lv else "ask"
            if want != node.access:
                self.event("access", f"{node.member_name}'s machine only allows '{node.access}' access (lead set '{want}')", node.id, actor="system")
            await ws.send_text(jdump(t="ready", node_id=node.id, hub_host=self.hostname))
            await self._reattach(node, first.get("sessions") or [])
            asyncio.create_task(self._readiness(node))
            self.event("online", f"{node.name} connected ({len(node.providers)} providers)", node.id, audit=False)
            while True:
                raw = await ws.receive_text()
                if len(raw) > MAX_FRAME * 8:
                    continue
                await self._on_node_msg(node, json.loads(raw))
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            if node and node.ws is ws:
                await self._node_offline(node, "disconnected")

    def _register_node(self, ws: WebSocket, hello: dict) -> NodeConn:
        nid = hello["node_id"]
        n = self.nodes.get(nid) or NodeConn(nid, "", "", "")
        n.name, n.hostname, n.os = clip(hello.get("name", n.name or "machine"), 60), clip(hello.get("hostname", "?"), 80), clip(hello.get("os", "?"), 40)
        n.providers = [p for p in (hello.get("providers") or []) if isinstance(p, dict)][:30]
        n.roots = [str(r) for r in (hello.get("roots") or [])][:10]
        n.allow_remote_start = bool(hello.get("allow_remote_start"))
        n.version = str(hello.get("version", ""))
        n.ws, n.online, n.last_seen = ws, True, time.time()
        self.nodes[nid] = n
        self.store.touch_node(nid, n.name, n.hostname, n.os)
        return n

    async def _reattach(self, node: NodeConn, reported: List[dict]) -> None:
        """A node reconnecting (e.g. after a hub restart) re-announces the sessions it still hosts."""
        seen = set()
        for r in reported:
            sid = str(r.get("sid", ""))
            if not sid:
                continue
            seen.add(sid)
            s = self.sessions.get(sid)
            if not s:
                s = Session(sid, node.id, str(r.get("provider", "?")), str(r.get("label", r.get("provider", "?"))), str(r.get("cwd", "")), int(r.get("cols", 80)), int(r.get("rows", 24)))
                self.sessions[sid] = s
            s.state = "live" if r.get("alive", True) else "exited"
            node.sessions.add(sid)
        for sid in list(node.sessions):
            if sid not in seen and sid in self.sessions and self.sessions[sid].state in ("offline", "live", "starting"):
                self.sessions[sid].state = "exited"
        self.mark_dirty()

    async def _node_offline(self, node: NodeConn, why: str) -> None:
        node.online = False
        node.ws = None
        for sid in node.sessions:
            s = self.sessions.get(sid)
            if s and s.state in ("live", "starting"):
                s.state = "offline"
                if s.mode != "watch":
                    s.mode, s.mode_expires = "watch", 0.0
        self.event("offline", f"{node.name} {why}", node.id, audit=False)

    async def _send_node(self, node_id: str, **msg: Any) -> bool:
        n = self.nodes.get(node_id)
        if not n or not n.online or not n.ws:
            return False
        try:
            await n.ws.send_text(jdump(**msg))
            return True
        except Exception:
            return False

    async def _on_node_msg(self, node: NodeConn, m: dict) -> None:
        t = m.get("t")
        node.last_seen = time.time()
        sid = str(m.get("sid", ""))
        s = self.sessions.get(sid) if sid else None
        if t == "hb":
            return
        if t == "out" and s and isinstance(m.get("d"), str) and s.node_id == node.id:
            if m.get("reset"):                                      # node re-announcing what it still holds after a reconnect
                s.ring.clear()
                s.ring_len, s.truncated = 0, False
            s.push(m["d"])
            await self._to_viewers(sid, t="out", sid=sid, d=m["d"], seq=s.seq)
        elif t == "opres":
            fut = self._ops.pop(str(m.get("id", "")), None)
            if fut and not fut.done():
                fut.set_result({k: v for k, v in m.items() if k not in ("t", "id")})
        elif t == "started" and s and s.node_id == node.id:
            s.state, s.error = ("live", "") if m.get("ok") else ("error", clip(m.get("err", "failed to start"), 200))
            if m.get("ok") and m.get("cwd"):
                s.cwd = clip(m["cwd"], 260)                          # the folder the node ACTUALLY resolved, not what was requested
            if m.get("ok") and node.access in ("copilot", "full") and s.mode == "watch":
                await self._set_mode(s, "copilot", ROLE_MEMBER, minutes=60, note=f"standing {node.access}-access policy the member accepted")
            self.event("session", f"{s.label} {'started' if m.get('ok') else 'failed: ' + s.error}", node.id, sid, "node", audit=False)
        elif t == "exit" and s and s.node_id == node.id:
            s.state, s.exit_code = "exited", m.get("code")
            if s.mode != "watch":
                s.mode, s.mode_expires = "watch", 0.0
            await self._to_viewers(sid, t="exit", sid=sid, code=s.exit_code)
            self.event("session", f"{s.label} exited", node.id, sid, "node", audit=False)
        elif t == "meta" and s and s.node_id == node.id:
            for k in ("todos", "files", "usage", "activity_text"):
                if k in m:
                    setattr(s, k, m[k])
            self.mark_dirty()

    # ───────────── sessions ─────────────
    async def start_session(self, node_id: str, provider: str, cwd: str, actor: str, cols: int = 100, rows: int = 28,
                            profile_id: str = "", model: str = "", extra_args: Optional[List[str]] = None) -> Optional[str]:
        n = self.nodes.get(node_id)
        if not n or not n.online:
            return None
        spec, prof = {"env": {}, "args": [], "secrets": []}, None
        if profile_id:
            prof = self.store.get_profile(profile_id, with_key=True)
            if not prof:
                raise apiprofiles.ProfileError("that API profile no longer exists")
            spec = apiprofiles.launch_spec(provider, prof, model)
        label = next((p.get("label", provider) for p in n.providers if p.get("id") == provider), provider)
        sid = "s_" + secrets.token_hex(4)
        s = Session(sid, node_id, provider, label, cwd, cols, rows)
        if prof:
            s.profile_id, s.profile_name, s.model = prof["id"], prof["name"], spec.get("model", "")
            s.price_in, s.price_out = prof.get("price_in") or 0.0, prof.get("price_out") or 0.0
        self.sessions[sid] = s
        n.sessions.add(sid)
        if not await self._send_node(node_id, t="start", sid=sid, provider=provider, cwd=cwd, cols=cols, rows=rows,
                                     env=spec["env"], args=list(spec["args"]) + list(extra_args or []), secrets=spec["secrets"]):
            del self.sessions[sid]
            n.sessions.discard(sid)
            return None
        self.event("start", f"{label} started on {n.name} by {actor}" + (f" using API profile '{prof['name']}' ({spec.get('model')})" if prof else ""), node_id, sid, actor)
        return sid

    async def _readiness(self, node: "NodeConn") -> None:
        """What this laptop can do for a project: git identity, GitHub CLI login. Shown on the Team page and checked before a build."""
        await asyncio.sleep(0.5)
        r = await self.node_op(node.id, "preflight", 60)
        if r.get("ok"):
            node.info = {k: r.get(k) for k in ("git", "gh", "gh_user", "git_name", "git_email")}
            node.info["checked"] = time.time()
            self.mark_dirty()

    async def node_op(self, node_id: str, name: str, timeout: float = 120.0, **args: Any) -> Dict[str, Any]:
        """Run a git / gh / shell operation on a teammate's machine (see node/ops.py) and wait for its result."""
        n = self.nodes.get(node_id)
        if not n or not n.online:
            return {"ok": False, "err": f"{n.name if n else node_id} is offline"}
        oid = "o_" + secrets.token_hex(5)
        fut = asyncio.get_running_loop().create_future()
        self._ops[oid] = fut
        if not await self._send_node(node_id, t="op", id=oid, name=name, args=args):
            self._ops.pop(oid, None)
            return {"ok": False, "err": f"{n.name} is unreachable"}
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._ops.pop(oid, None)
            return {"ok": False, "err": f"{name} timed out after {timeout:.0f}s on {n.name}"}

    async def ensure_copilot(self, s: "Session") -> bool:
        """Total access: under a copilot/full standing policy the lead can always type. Re-asserts an expired grant."""
        n = self.nodes.get(s.node_id)
        if s.state != "live" or not n:
            return False
        if s.mode in ("copilot", "takeover"):
            return True
        if n.access in ("copilot", "full"):
            await self._set_mode(s, "copilot", ROLE_MEMBER, minutes=240, note=f"standing {n.access}-access policy the member accepted")
            return True
        return False

    def push_projects(self) -> None:
        if self._proj_task is None or self._proj_task.done():
            self._proj_task = asyncio.create_task(self._flush_projects())

    async def _flush_projects(self) -> None:
        await asyncio.sleep(0.15)
        msg = self.projects.public_all()
        for v in list(self.viewers):
            if v.role == ROLE_LEAD and not await v.send(t="projects", **msg):
                self.viewers.discard(v)

    async def stop_session(self, sid: str, actor: str) -> None:
        s = self.sessions.get(sid)
        if s:
            await self._send_node(s.node_id, t="stop", sid=sid)
            self.event("stop", f"{s.label} stopped by {actor}", s.node_id, sid, actor)

    def forget_session(self, sid: str) -> None:
        s = self.sessions.get(sid)
        if s and s.state in ("exited", "error", "offline"):
            self.nodes.get(s.node_id, NodeConn("", "", "", "")).sessions.discard(sid)
            del self.sessions[sid]
            self.mark_dirty()

    # ───────────── control ladder ─────────────
    def can_type(self, v: Viewer, s: Session) -> bool:
        if s.state != "live":
            return False
        if v.role == ROLE_MEMBER:
            return v.node_id == s.node_id and s.mode != "takeover"          # exclusive lock during takeover
        return s.mode in ("copilot", "takeover")

    async def _set_mode(self, s: Session, mode: str, actor: str, minutes: Optional[float] = None, note: str = "") -> None:
        s.mode, s.mode_by = mode, actor
        if mode == "watch" or mode == "suggest":
            s.mode_expires = 0.0
        else:
            cap = TAKEOVER_MAX_MIN if mode == "takeover" else 60
            s.mode_expires = time.time() + 60 * min(float(minutes or cap), cap)
        await self._send_node(s.node_id, t="mode", sid=s.sid, mode=mode)
        n = self.nodes.get(s.node_id)
        self.event("mode", f"{s.label}: control -> {mode}" + (f" ({note})" if note else ""), s.node_id, s.sid, actor)
        await self._to_viewers(s.sid, t="mode", sid=s.sid, mode=mode)

    async def request_mode(self, v: Viewer, sid: str, mode: str, reason: str = "") -> dict:
        s = self.sessions.get(sid)
        if v.role != ROLE_LEAD or not s or not valid_mode(mode):
            return {"ok": False, "err": "not allowed"}
        if MODE_RANK[mode] <= MODE_RANK["watch"] or MODE_RANK[mode] <= MODE_RANK[s.mode]:
            await self._set_mode(s, mode, ROLE_LEAD, note="lead lowered control")      # lowering never needs consent
            return {"ok": True, "applied": True}
        if mode == "suggest":                                                          # suggestions are advisory text only
            await self._set_mode(s, mode, ROLE_LEAD)
            return {"ok": True, "applied": True}
        n = self.nodes.get(s.node_id)
        if n and ((mode == "copilot" and n.access in ("copilot", "full")) or (mode == "takeover" and n.access == "full")):
            await self._set_mode(s, mode, ROLE_LEAD, minutes=TAKEOVER_MAX_MIN if mode == "takeover" else 60,
                                 note=f"allowed by {n.member_name or n.name}'s standing {n.access}-access policy")
            return {"ok": True, "applied": True, "standing": True}
        rid = "r_" + secrets.token_hex(3)
        self.requests[rid] = {"rid": rid, "kind": "mode", "node_id": s.node_id, "sid": sid, "mode": mode,
                              "by": ROLE_LEAD, "reason": clip(reason, 160), "created": now_ms(), "label": s.label}
        self.event("request", f"lead asks for {mode} on {s.label}", s.node_id, sid, ROLE_LEAD)
        return {"ok": True, "applied": False, "rid": rid}

    async def answer(self, v: Viewer, rid: str, allow: bool, minutes: Optional[float] = None) -> dict:
        r = self.requests.get(rid)
        if not r or v.role != ROLE_MEMBER or v.node_id != r["node_id"]:
            return {"ok": False, "err": "not allowed"}
        del self.requests[rid]
        self.event("answer", f"member {'allowed' if allow else 'denied'} {r['kind']} request", r["node_id"], r.get("sid", ""), ROLE_MEMBER)
        if allow and r["kind"] == "mode":
            s = self.sessions.get(r["sid"])
            if s:
                await self._set_mode(s, r["mode"], ROLE_MEMBER, minutes, note="granted by member")
        elif allow and r["kind"] == "start":
            try:
                await self.start_session(r["node_id"], r["provider"], r.get("cwd", ""), ROLE_LEAD, profile_id=r.get("profile_id", ""), model=r.get("model", ""))
            except apiprofiles.ProfileError as e:
                self.event("error", f"could not start: {e}", r["node_id"], "", ROLE_LEAD)
        self.mark_dirty()
        return {"ok": True}

    async def revoke(self, v: Viewer) -> dict:
        """Member kill switch: every session on this machine drops to watch; pending requests are discarded."""
        if v.role != ROLE_MEMBER:
            return {"ok": False, "err": "not allowed"}
        n = 0
        for sid in list(self.nodes[v.node_id].sessions if v.node_id in self.nodes else []):
            s = self.sessions.get(sid)
            if s and s.mode != "watch":
                await self._set_mode(s, "watch", ROLE_MEMBER, note="KILL SWITCH")
                n += 1
        for rid in [k for k, r in self.requests.items() if r["node_id"] == v.node_id]:
            del self.requests[rid]
        node = self.nodes.get(v.node_id)
        if node and node.access != "ask":
            node.access = "ask"
            self.store.set_node_access(node.id, "ask")                  # a standing grant is withdrawn until the member accepts a new invite
        self.event("revoke", f"member pulled the kill switch ({n} sessions back to watch-only; standing access withdrawn)", v.node_id, "", ROLE_MEMBER)
        return {"ok": True, "n": n}

    async def lead_input(self, v: Viewer, s: Session, data: str, label: str = "") -> None:
        await self._send_node(s.node_id, t="in", sid=s.sid, d=data, actor=v.role)
        if v.role == ROLE_LEAD:                                       # audit lead keystrokes in aggregated, redacted slices
            s.audit_acc += data
            if label or time.time() - s.audit_last > 2.0:
                self.store.audit(label or ROLE_LEAD, "input", s.node_id, s.sid, f"{len(s.audit_acc)} chars: {clip(redact(s.audit_acc.replace(chr(13), '<enter>')), 100)}")
                s.audit_acc, s.audit_last = "", time.time()

    # ───────────── viewers ─────────────
    async def handle_viewer(self, ws: WebSocket, role: str, key: str) -> None:
        origin, host = ws.headers.get("origin"), ws.headers.get("host", "")
        if origin and urlparse(origin).netloc != host:
            await ws.close(code=4403)
            return
        v: Optional[Viewer] = None
        if self._blocked(ws):
            await ws.close(code=4429)
            return
        if role == ROLE_LEAD and hmac.compare_digest(key or "", self.lead_key):
            v = Viewer(ws, ROLE_LEAD)
        elif role == ROLE_MEMBER:
            row = self.store.node_for_member_key(key)
            if row:
                v = Viewer(ws, ROLE_MEMBER, row["id"])
        if v is None:
            self._fail(ws, f"bad {role} key")
            await ws.close(code=4401)
            return
        await ws.accept()
        self.viewers.add(v)
        await v.send(t="state", **self.public_state(v))
        if v.role == ROLE_LEAD:
            await v.send(t="orch", **self.orch.public())
            await v.send(t="projects", **self.projects.public_all())
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw) > MAX_FRAME:
                    continue
                await self._on_viewer_msg(v, json.loads(raw))
        except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            self.viewers.discard(v)
            for sid in list(v.subs):
                await self._unsub(v, sid)

    def _visible(self, v: Viewer, s: Optional[Session]) -> bool:
        return bool(s) and (v.role == ROLE_LEAD or s.node_id == v.node_id)

    async def _sub(self, v: Viewer, sid: str) -> None:
        s = self.sessions.get(sid)
        if not self._visible(v, s) or sid in v.subs:
            return
        v.subs.add(sid)
        s.viewers_n = sum(1 for x in self.viewers if sid in x.subs)
        if not s.responder:
            s.responder = v.id
        await v.send(t="replay", sid=sid, d="".join(s.ring), seq=s.seq, truncated=s.truncated, responder=(s.responder == v.id))
        await self._send_node(s.node_id, t="viewers", sid=sid, n=s.viewers_n)
        if s.provider != "shell" and (s.truncated or s.seq > 0):
            await self._send_node(s.node_id, t="redraw", sid=sid)      # make a TUI repaint so a late joiner sees a clean screen
        self.mark_dirty()

    async def _unsub(self, v: Viewer, sid: str) -> None:
        v.subs.discard(sid)
        s = self.sessions.get(sid)
        if s:
            s.viewers_n = sum(1 for x in self.viewers if sid in x.subs)
            if s.responder == v.id:                                   # hand the responder role to another attached viewer
                s.responder = ""
                for x in self.viewers:
                    if x is not v and sid in x.subs:
                        s.responder = x.id
                        await x.send(t="responder", sid=sid, on=True)
                        break
            await self._send_node(s.node_id, t="viewers", sid=sid, n=s.viewers_n)

    async def _on_viewer_msg(self, v: Viewer, m: dict) -> None:
        t, sid = m.get("t"), str(m.get("sid", ""))
        s = self.sessions.get(sid) if sid else None
        lead = v.role == ROLE_LEAD
        if t == "sub":
            await self._sub(v, sid)
        elif t == "unsub":
            await self._unsub(v, sid)
        elif t == "in" and s and self._visible(v, s) and isinstance(m.get("d"), str):
            if self.can_type(v, s):
                await self.lead_input(v, s, m["d"])
            else:
                await v.send(t="denied", sid=sid, why=f"input blocked: control mode is '{s.mode}'" if lead else "input blocked during takeover")
        elif t == "reply" and s and self._visible(v, s) and s.responder == v.id and isinstance(m.get("d"), str) and is_terminal_reply(m["d"]):
            await self._send_node(s.node_id, t="reply", sid=sid, d=m["d"])
        elif t == "resize" and s and self._visible(v, s) and (not lead or s.mode in ("copilot", "takeover")):
            await self._send_node(s.node_id, t="resize", sid=sid, cols=int(m.get("cols", 80)), rows=int(m.get("rows", 24)))
        elif t == "start":
            await self._viewer_start(v, m)
        elif t == "stop" and s and self._visible(v, s):
            if not lead or s.mode == "takeover":                       # the lead may stop a session only under takeover
                await self.stop_session(sid, v.role)
            else:
                await v.send(t="denied", sid=sid, why="stopping a member's session needs takeover consent")
        elif t == "forget" and self._visible(v, s):
            self.forget_session(sid)
        elif t == "restart" and s and self._visible(v, s) and s.state in ("exited", "error", "offline"):
            await self._viewer_start(v, {"node": s.node_id, "provider": s.provider, "cwd": s.cwd, "ref": m.get("ref")})
            self.forget_session(sid)
        elif t == "request_mode":
            await v.send(t="result", ref=m.get("ref"), **(await self.request_mode(v, sid, str(m.get("mode", "")), str(m.get("reason", "")))))
        elif t == "set_mode" and s and v.role == ROLE_MEMBER and v.node_id == s.node_id and valid_mode(m.get("mode")):
            await self._set_mode(s, m["mode"], ROLE_MEMBER, m.get("minutes"))
        elif t == "answer":
            await v.send(t="result", ref=m.get("ref"), **(await self.answer(v, str(m.get("rid", "")), bool(m.get("allow")), m.get("minutes"))))
        elif t == "revoke":
            await v.send(t="result", ref=m.get("ref"), **(await self.revoke(v)))
        elif t == "suggest" and s and lead and s.mode in ("suggest", "copilot", "takeover"):
            text = redact(str(m.get("text", "")))[:500]
            self.event("suggestion", f"lead suggests: {clip(text, 120)}", s.node_id, sid, ROLE_LEAD)
            for x in list(self.viewers):
                if x.role == ROLE_MEMBER and x.node_id == s.node_id:
                    await x.send(t="suggestion", sid=sid, text=text, label=s.label)
        elif t == "allow_remote_start" and v.role == ROLE_MEMBER and v.node_id in self.nodes:
            n = self.nodes[v.node_id]
            n.allow_remote_start = bool(m.get("on"))
            await self._send_node(n.id, t="config", allow_remote_start=n.allow_remote_start)
            self.event("config", f"remote start {'allowed' if n.allow_remote_start else 'blocked'} by member", n.id, "", ROLE_MEMBER)
        elif t == "create_invite" and lead:
            member = clip(str(m.get("member", "")).strip(), 60)
            access = m.get("access") if m.get("access") in ACCESS_LEVELS else "ask"
            ttl = {"10m": 600, "1h": 3600, "24h": 86400}.get(str(m.get("ttl", "1h")), 3600)
            code = self.store.create_invite(ttl_s=ttl, member_name=member, access=access)
            self.event("invite", f"invite for {member or 'a teammate'} created (access policy: {access}, valid 10 min)", actor=ROLE_LEAD)
            await v.send(t="result", ref=m.get("ref"), ok=True, code=code, member=member, access=access)
        elif t == "net_info" and lead:
            await v.send(t="result", ref=m.get("ref"), ok=True, **self.net_info())
        elif t == "set_team_url" and lead:
            url = str(m.get("url", "")).strip().rstrip("/")
            if url and not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]{1,255}", url):
                await v.send(t="result", ref=m.get("ref"), ok=False, err="enter a URL like http://192.168.1.20:8000 or https://name.trycloudflare.com")
            else:
                self.store.set_setting("team_url", url)
                self.event("config", f"team URL set to {url or '(automatic)'}", actor=ROLE_LEAD)
                await v.send(t="result", ref=m.get("ref"), ok=True, **self.net_info())
        elif t == "set_access" and lead and m.get("node") in self.nodes and m.get("access") in ACCESS_LEVELS:
            n = self.nodes[m["node"]]
            self.store.set_node_access(n.id, m["access"])
            n.access = m["access"] if lv_rank(m["access"]) <= lv_rank(getattr(n, "accepted", "ask")) else getattr(n, "accepted", "ask")                                  # takes effect now if this machine allows it; on reconnect the lower of both wins
            self.event("access", f"lead set {n.member_name or n.name}'s access to '{m['access']}'", n.id, actor=ROLE_LEAD)
            await v.send(t="result", ref=m.get("ref"), ok=True)
        elif t.startswith("project_") and lead:
            await self.projects.on_viewer(v, t, m)
        elif t == "orch_say" and lead:
            ok = await self.orch.say(str(m.get("text", "")))
            await v.send(t="result", ref=m.get("ref"), ok=ok)
        elif t == "orch_cancel" and lead:
            await self.orch.cancel()
        elif t == "orch_decide" and lead:
            self.orch.decide(str(m.get("aid", "")), bool(m.get("allow")))
        elif t == "orch_config" and lead:
            self.orch.configure(m.get("autonomy"), m.get("profile_id"), m.get("model"))
        elif t == "orch_clear" and lead:
            self.orch.clear()
        elif t == "list_profiles" and lead:
            await v.send(t="result", ref=m.get("ref"), ok=True, profiles=self.store.list_profiles())
        elif t == "save_profile" and lead:
            try:
                pid = self.store.save_profile(m.get("profile") or {}, str(m.get("id", "")))
                self.event("profile", f"API profile '{(m.get('profile') or {}).get('name', '')}' saved", actor=ROLE_LEAD)
                await v.send(t="result", ref=m.get("ref"), ok=True, id=pid, profiles=self.store.list_profiles())
            except (ValueError, RuntimeError) as e:
                await v.send(t="result", ref=m.get("ref"), ok=False, err=str(e))
        elif t == "delete_profile" and lead:
            self.store.delete_profile(str(m.get("id", "")))
            await v.send(t="result", ref=m.get("ref"), ok=True, profiles=self.store.list_profiles())
        elif t == "test_profile" and lead:
            prof = self.store.get_profile(str(m.get("id", "")), with_key=True) if m.get("id") else None
            if not prof:
                await v.send(t="result", ref=m.get("ref"), ok=False, err="save the profile first")
            else:
                await v.send(t="result", ref=m.get("ref"), **(await apiprofiles.test_profile(prof)))
        elif t == "remove_node" and lead and m.get("node") in self.nodes:
            nid = m["node"]
            n = self.nodes.pop(nid)
            for sid2 in list(n.sessions):
                self.sessions.pop(sid2, None)
            self.store.remove_node(nid)
            if n.ws:
                try:
                    await n.ws.close(code=4400)
                except Exception:
                    pass
            self.event("remove", f"machine {n.name} removed", nid, "", ROLE_LEAD)
        elif t == "ping":
            await v.send(t="pong", n=m.get("n"))

    async def _viewer_start(self, v: Viewer, m: dict) -> None:
        node_id = v.node_id if v.role == ROLE_MEMBER else str(m.get("node", ""))
        n = self.nodes.get(node_id)
        provider, cwd = str(m.get("provider", "")), str(m.get("cwd", ""))[:260]
        if not n or not n.online or not any(p.get("id") == provider and p.get("available") for p in n.providers):
            await v.send(t="result", ref=m.get("ref"), ok=False, err="machine offline or provider not available")
            return
        pid, model = str(m.get("profile_id", "")), str(m.get("model", ""))
        if pid:                                                       # fail early with a readable reason if this provider can't use the profile
            prof = self.store.get_profile(pid, with_key=True)
            try:
                if not prof:
                    raise apiprofiles.ProfileError("that API profile no longer exists")
                apiprofiles.launch_spec(provider, prof, model)
            except apiprofiles.ProfileError as e:
                await v.send(t="result", ref=m.get("ref"), ok=False, err=str(e))
                return
        if v.role == ROLE_MEMBER or n.allow_remote_start or n.access in ("copilot", "full"):
            sid = await self.start_session(node_id, provider, cwd, v.role, int(m.get("cols", 100)), int(m.get("rows", 28)), pid, model)
            await v.send(t="result", ref=m.get("ref"), ok=bool(sid), sid=sid, applied=True)
        else:
            rid = "r_" + secrets.token_hex(3)
            self.requests[rid] = {"rid": rid, "kind": "start", "node_id": node_id, "provider": provider, "cwd": cwd, "by": ROLE_LEAD,
                                  "profile_id": pid, "model": model,
                                  "created": now_ms(), "label": next((p.get("label") for p in n.providers if p.get("id") == provider), provider)}
            self.event("request", f"lead asks to start {provider} on {n.name}", node_id, "", ROLE_LEAD)
            await v.send(t="result", ref=m.get("ref"), ok=True, applied=False, rid=rid)

    # ───────────── aggregates ─────────────
    def usage_summary(self) -> dict:
        per_provider: Dict[str, dict] = {}
        per_node: Dict[str, dict] = {}
        for s in self.sessions.values():
            u = s.usage or {}
            for bucket, key in ((per_provider, s.provider), (per_node, s.node_id)):
                b = bucket.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "sessions": 0, "known": 0, "bytes_out": 0})
                b["sessions"] += 1
                b["bytes_out"] += s.bytes_out
                if u.get("source") and u.get("source") != "terminal":
                    b["known"] += 1
                    for k in ("input_tokens", "output_tokens", "cache_read"):
                        b[k] += int(u.get(k, 0) or 0)
        return {"by_provider": per_provider, "by_node": per_node, "history": self.store.usage_by_day()}


# ───────────── FastAPI wiring ─────────────
def _hub(obj) -> Hub:
    h = getattr(obj.app.state, "hub", None)
    if h is None:
        raise HTTPException(404, "hub disabled (set KODE_ENABLE_HUB=1)")
    return h


@router.websocket("/hub/node")
async def ws_node(ws: WebSocket):
    h = getattr(ws.app.state, "hub", None)
    if h is None:
        await ws.close(code=4404)
        return
    await h.handle_node(ws)


@router.websocket("/hub/viewer")
async def ws_viewer(ws: WebSocket, role: str = "lead", key: str = ""):
    h = getattr(ws.app.state, "hub", None)
    if h is None:
        await ws.close(code=4404)
        return
    await h.handle_viewer(ws, role, key)


def _lead_only(request: Request) -> Hub:
    h = _hub(request)
    if not hmac.compare_digest(request.headers.get("x-kode-key", ""), h.lead_key):
        raise HTTPException(403, "lead key required")
    return h


@router.get("/hub/audit", include_in_schema=False)
async def audit(request: Request, node: str = "", limit: int = 200):
    return _lead_only(request).store.audit_list(min(limit, 500), node)


@router.get("/hub/usage", include_in_schema=False)
async def usage(request: Request):
    return _lead_only(request).usage_summary()


def load_or_create_key(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(24)
    path.write_text(key, encoding="utf-8")
    return key
