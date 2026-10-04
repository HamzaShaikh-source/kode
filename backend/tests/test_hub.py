"""The Brain end to end: real hub, real kode-node processes, real PTY shells."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets

from hub.protocol import redact
from node.agent import Hosted, NodeAgent
from node.collectors import ClaudeCollector, CodexCollector, git_info
from tests.hubkit import Console, HubServer, until

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="shell sessions use cmd.exe")


async def pair(hub, name, remote_start=True):
    async with Console(hub.base, "lead", hub.lead_key) as lead:
        r = await lead.call(t="create_invite")
        proc, cfg = hub.spawn_node(name, code=r["code"], remote_start=remote_start)
        await until(lambda: lead.nodes().get(name, {}).get("online"), 20)
        return r["code"], proc, cfg


def run(coro):
    return asyncio.run(coro)


def test_pairing_flow_and_code_is_single_use(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            code, proc, cfg = await pair(hub, "alpha")
            saved = json.loads(cfg.read_text())
            assert saved["token"] and saved["member_key"] and saved["node_id"].startswith("n_")
            # the same code cannot pair a second machine: its process gives up and it never appears
            p2, _ = hub.spawn_node("beta", code=code)
            for _ in range(100):
                if p2.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            assert p2.poll() is not None, "node with a used code should exit, not retry forever"
            assert "pairing failed" in p2.stdout.read()
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                assert "beta" not in lead.nodes()
            # alpha reconnects later WITHOUT a code, using its saved credentials
            proc.terminate(); proc.wait(5)
            hub.spawn_node("alpha", config=cfg)
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                await until(lambda: lead.nodes().get("alpha", {}).get("online"), 20)
    run(go())


def test_viewer_auth_required(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            for role, key in (("lead", "wrong"), ("member", "nope"), ("lead", "")):
                with pytest.raises(Exception):
                    async with websockets.connect(hub.base.replace("http", "ws") + f"/hub/viewer?role={role}&key={key}") as ws:
                        await asyncio.wait_for(ws.recv(), 3)
            with pytest.raises(Exception):                       # node socket without credentials
                async with websockets.connect(hub.base.replace("http", "ws") + "/hub/node") as ws:
                    await ws.send(json.dumps({"t": "hello", "node_id": "x", "token": "y"}))
                    r = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    assert r["t"] == "error"
                    await asyncio.wait_for(ws.recv(), 3)
    run(go())


def test_remote_start_needs_consent_unless_member_allows(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            code, proc, cfg = await pair(hub, "gamma", remote_start=False)
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                nid = lead.nodes()["gamma"]["id"]
                r = await lead.call(t="start", node=nid, provider="shell")
                assert r["ok"] and r["applied"] is False and r["rid"]          # asked, not started
                assert not lead.sessions()
                mk = json.loads(cfg.read_text())["member_key"]
                async with Console(hub.base, "member", mk) as member:
                    await until(lambda: member.state.get("requests"), 5)
                    assert member.state["requests"][0]["kind"] == "start"
                    assert (await member.call(t="answer", rid=r["rid"], allow=True))["ok"]
                    await until(lambda: any(s["state"] == "live" for s in lead.sessions().values()), 15)
    run(go())


def test_consent_ladder_takeover_lock_killswitch_and_isolation(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, cfg_a = await pair(hub, "alice-pc")
            _, _, cfg_b = await pair(hub, "bob-pc")
            mk_a, mk_b = json.loads(cfg_a.read_text())["member_key"], json.loads(cfg_b.read_text())["member_key"]
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk_a) as alice, Console(hub.base, "member", mk_b) as bob:
                ids = {n: lead.nodes()[n]["id"] for n in ("alice-pc", "bob-pc")}
                sa = (await lead.call(t="start", node=ids["alice-pc"], provider="shell"))["sid"]
                sb = (await lead.call(t="start", node=ids["bob-pc"], provider="shell"))["sid"]
                await until(lambda: all(s["state"] == "live" for s in lead.sessions().values()) and len(lead.sessions()) == 2, 15)

                # isolation: each member sees only their own machine and sessions
                await until(lambda: alice.state.get("sessions"), 5)
                assert {s["sid"] for s in alice.state["sessions"]} == {sa} and {n["name"] for n in alice.state["nodes"]} == {"alice-pc"}
                assert {s["sid"] for s in bob.state["sessions"]} == {sb}
                await bob.send(t="sub", sid=sa)                                 # bob cannot attach to alice's terminal
                await asyncio.sleep(0.5)
                assert sa not in bob.out

                await lead.send(t="sub", sid=sa)
                await until(lambda: sa in lead.out, 5)

                # watch (default): the lead cannot type
                await lead.send(t="in", sid=sa, d="echo LEAD_BLOCKED\r")
                await until(lambda: any(m.get("t") == "denied" for m in lead.msgs), 5)
                await asyncio.sleep(0.8)
                assert "LEAD_BLOCKED" not in lead.out.get(sa, "").replace("echo LEAD_BLOCKED", "")

                # asking for co-pilot creates a request; the lead cannot grant it to themselves
                r = await lead.call(t="request_mode", sid=sa, mode="copilot")
                assert r["ok"] and r["applied"] is False
                assert lead.sessions()[sa]["mode"] == "watch"
                await until(lambda: alice.state.get("requests"), 5)
                assert not bob.state.get("requests")                            # bob is not asked about alice's machine
                assert not (await bob.call(t="answer", rid=r["rid"], allow=True))["ok"]       # and cannot answer it either
                assert (await alice.call(t="answer", rid=r["rid"], allow=True, minutes=5))["ok"]
                await until(lambda: lead.sessions()[sa]["mode"] == "copilot", 5)

                # co-pilot: lead input now executes in the member's real shell
                await lead.send(t="in", sid=sa, d="echo COPILOT_OK\r")
                await until(lambda: lead.out[sa].count("COPILOT_OK") >= 2, 10)

                # takeover: exclusive lock, the member is blocked while it lasts
                r = await lead.call(t="request_mode", sid=sa, mode="takeover")
                await alice.call(t="answer", rid=r["rid"], allow=True, minutes=5)
                await until(lambda: lead.sessions()[sa]["mode"] == "takeover", 5)
                await alice.send(t="in", sid=sa, d="echo MEMBER_DURING_TAKEOVER\r")
                await until(lambda: any(m.get("t") == "denied" for m in alice.msgs), 5)

                # kill switch: everything drops to watch instantly and lead input is refused again
                assert (await alice.call(t="revoke"))["ok"]
                await until(lambda: lead.sessions()[sa]["mode"] == "watch", 5)
                lead.msgs.clear()
                await lead.send(t="in", sid=sa, d="echo AFTER_REVOKE\r")
                await until(lambda: any(m.get("t") == "denied" for m in lead.msgs), 5)

                # the audit trail recorded the whole story
                acts = [a["action"] for a in hub.hub.store.audit_list(200)]
                for needed in ("pair", "start", "request", "answer", "mode", "revoke", "input"):
                    assert needed in acts, (needed, acts)
    run(go())


def test_takeover_expires_automatically(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, cfg = await pair(hub, "delta")
            mk = json.loads(cfg.read_text())["member_key"]
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                nid = lead.nodes()["delta"]["id"]
                sid = (await lead.call(t="start", node=nid, provider="shell"))["sid"]
                await until(lambda: lead.sessions().get(sid, {}).get("state") == "live", 15)
                await m.send(t="set_mode", sid=sid, mode="takeover", minutes=5)       # member may grant directly
                await until(lambda: lead.sessions()[sid]["mode"] == "takeover", 5)
                hub.hub.sessions[sid].mode_expires = time.time() - 1                    # force the clock
                await until(lambda: lead.sessions()[sid]["mode"] == "watch", 6)
    run(go())


def test_output_is_redacted_on_the_node_before_it_leaves(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, cfg = await pair(hub, "eps")
            mk = json.loads(cfg.read_text())["member_key"]
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = (await lead.call(t="start", node=lead.nodes()["eps"]["id"], provider="shell"))["sid"]
                await until(lambda: lead.sessions().get(sid, {}).get("state") == "live", 15)
                await lead.send(t="sub", sid=sid)
                await m.send(t="set_mode", sid=sid, mode="copilot", minutes=5)
                await until(lambda: lead.sessions()[sid]["mode"] == "copilot", 5)
                secret = "thk_live_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
                await lead.send(t="in", sid=sid, d=f"echo {secret}\r")
                await until(lambda: "[REDACTED]" in lead.out.get(sid, ""), 10)
                assert secret not in lead.out[sid] and "A1b2C3d4E5f6G7h8I9j0K1l2" not in lead.out[sid]
    run(go())


def test_redact_patterns():
    assert "[REDACTED]" in redact("key=sk-ant-api03-" + "x" * 30)
    assert "[REDACTED]" in redact("ghp_" + "a" * 36)
    assert "[REDACTED]" in redact("Authorization: Bearer " + "z" * 32)
    assert redact("hello world") == "hello world"


# ───────── node-side enforcement, independent of the hub ─────────
def test_node_drops_lead_input_unless_its_own_mode_allows(tmp_path):
    class FakePty:
        def __init__(self): self.writes = []
        def write(self, d): self.writes.append(d)
        def alive(self): return True
        def close(self): pass
    agent = NodeAgent("http://127.0.0.1:1", "n", tmp_path / "c.json", [str(tmp_path)], False)
    h = Hosted("s_1", "shell", "Shell", str(tmp_path), FakePty())
    agent.sessions["s_1"] = h

    async def go():
        await agent._on_hub({"t": "in", "sid": "s_1", "d": "x", "actor": "lead"})
        assert h.pty.writes == []                                        # watch: dropped even if a rogue hub sent it
        await agent._on_hub({"t": "mode", "sid": "s_1", "mode": "copilot"})
        await agent._on_hub({"t": "in", "sid": "s_1", "d": "y", "actor": "lead"})
        assert h.pty.writes == ["y"]
        await agent._on_hub({"t": "in", "sid": "s_1", "d": "m", "actor": "member"})
        assert h.pty.writes == ["y", "m"]
    run(go())


def test_node_refuses_workspace_escape(tmp_path):
    agent = NodeAgent("http://127.0.0.1:1", "n", tmp_path / "c.json", [str(tmp_path / "ws")], False)
    (tmp_path / "ws").mkdir()
    assert agent._resolve_cwd("") == str((tmp_path / "ws").resolve())
    assert agent._resolve_cwd("sub/dir").endswith("sub\\dir")
    assert agent._resolve_cwd("..") is None
    assert agent._resolve_cwd(str(tmp_path)) is None
    assert agent._resolve_cwd("C:\\Windows") is None


# ───────── collectors ─────────
def _write(p, recs):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def test_claude_collector_reads_real_transcript_shape(tmp_path, monkeypatch):
    import node.collectors as c
    monkeypatch.setattr(c, "HOME", tmp_path)
    cwd = r"D:\work\demo"
    f = tmp_path / ".claude" / "projects" / c._enc_claude(cwd) / "abc.jsonl"
    todos = [{"content": "Write tests", "status": "in_progress", "activeForm": "Writing tests"}, {"content": "Ship", "status": "pending"}]
    _write(f, [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "1", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
                                                       "content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}]}},
        {"type": "assistant", "uuid": "1b", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": []}},        # duplicate id: counted once
        {"type": "assistant", "uuid": "2", "message": {"id": "m2", "usage": {"input_tokens": 3, "output_tokens": 2},
                                                       "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}]}},
    ])
    col = c.ClaudeCollector(cwd, f.stat().st_mtime - 1)
    out = col.poll()
    assert out["usage"]["input_tokens"] == 13 and out["usage"]["output_tokens"] == 7 and out["usage"]["cache_read"] == 100
    assert out["usage"]["source"] == "claude-transcript" and out["usage"]["messages"] == 2
    assert out["todos"] == [{"text": "Write tests", "status": "doing"}, {"text": "Ship", "status": "pending"}]
    assert out["activity_text"] == "Edit: src/a.py"
    # incremental: appending a record updates totals without re-reading
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "uuid": "3", "message": {"id": "m3", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": []}}) + "\n")
    assert col.poll()["usage"]["input_tokens"] == 14


def test_codex_collector_reads_token_count_and_plan(tmp_path, monkeypatch):
    import node.collectors as c
    monkeypatch.setattr(c, "HOME", tmp_path)
    cwd = str(tmp_path / "proj")
    f = tmp_path / ".codex" / "sessions" / "2026" / "10" / "04" / "rollout-x.jsonl"
    _write(f, [
        {"type": "session_meta", "payload": {"cwd": cwd}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": json.dumps({"plan": [{"step": "Read code", "status": "completed"}, {"step": "Patch", "status": "in_progress"}]})}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 9, "reasoning_output_tokens": 4}}}},
    ])
    col = c.CodexCollector(cwd, f.stat().st_mtime - 1)
    out = col.poll()
    assert out["usage"]["input_tokens"] == 50 and out["usage"]["output_tokens"] == 9 and out["usage"]["source"] == "codex-transcript"
    assert [t["status"] for t in out["todos"]] == ["completed", "doing"]


def test_git_info_reports_changed_files(tmp_path):
    def git(*a): subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1"); git("add", "."); git("commit", "-qm", "init")
    (tmp_path / "a.txt").write_text("2"); (tmp_path / "b.txt").write_text("new")
    info = run(git_info(str(tmp_path)))
    assert info["repo"] and info["changed"] == 2 and {f["path"] for f in info["files"]} == {"a.txt", "b.txt"}
    assert run(git_info(str(tmp_path / "nowhere")))["repo"] is False
