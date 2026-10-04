"""Real-world conditions: the Brain restarts, links flap, providers crash, output floods, unicode, scale, brute force.
Every test runs a real hub + real kode-node processes + real PTY shells."""
import asyncio
import json
import statistics
import sys
import time

import pytest
import websockets

from hub.protocol import RING_CHARS
from tests.hubkit import Console, HubServer, until

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="shell sessions use cmd.exe")
run = asyncio.run


async def pair(hub, name):
    async with Console(hub.base, "lead", hub.lead_key) as lead:
        code = (await lead.call(t="create_invite"))["code"]
        proc, cfg = hub.spawn_node(name, code=code)
        await until(lambda: lead.nodes().get(name, {}).get("online"), 25)
    return proc, cfg, json.loads(cfg.read_text())["member_key"]


async def start_shell(lead, name):
    sid = (await lead.call(t="start", node=lead.nodes()[name]["id"], provider="shell"))["sid"]
    await until(lambda: lead.sessions().get(sid, {}).get("state") == "live", 20)
    return sid


def test_brain_restart_machines_reattach_and_replay_survives(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            proc, cfg, mk = await pair(hub, "resilient")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "resilient")
                await m.send(t="sub", sid=sid)
                await m.send(t="in", sid=sid, d="echo BEFORE_RESTART_MARKER\r")
                await until(lambda: m.out.get(sid, "").count("BEFORE_RESTART_MARKER") >= 2, 10)
            hub.restart_hub()                                              # the Brain dies and comes back
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                await until(lambda: lead.nodes().get("resilient", {}).get("online"), 30)       # node reconnected by itself
                await until(lambda: lead.sessions().get(sid, {}).get("state") == "live", 15)   # and re-announced its session
                assert proc.poll() is None, "the node process must survive a Brain restart"
                await lead.send(t="sub", sid=sid)
                await until(lambda: "BEFORE_RESTART_MARKER" in lead.out.get(sid, ""), 10)       # history re-sent by the node
                assert lead.sessions()[sid]["mode"] == "watch"                                    # control grants do NOT survive a restart
    run(go())


def test_link_flap_node_reconnects_and_session_continues(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            proc, cfg, mk = await pair(hub, "flappy")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "flappy")
                await m.send(t="sub", sid=sid)
                nid = lead.nodes()["flappy"]["id"]
                fut = asyncio.run_coroutine_threadsafe(hub.hub.nodes[nid].ws.close(code=1011), hub.hub.loop)   # cut the link from the hub side
                fut.result(5)
                await until(lambda: not lead.nodes()["flappy"]["online"], 10)
                assert lead.sessions()[sid]["state"] == "offline"
                await until(lambda: lead.nodes()["flappy"]["online"], 30)
                await until(lambda: lead.sessions()[sid]["state"] == "live", 10)
                await m.send(t="in", sid=sid, d="echo STILL_ALIVE_AFTER_FLAP\r")
                await until(lambda: m.out.get(sid, "").count("STILL_ALIVE_AFTER_FLAP") >= 2, 15)
    run(go())


def test_provider_exit_is_visible_and_restart_works(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, mk = await pair(hub, "mortal")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "mortal")
                await m.send(t="in", sid=sid, d="exit\r")
                await until(lambda: lead.sessions()[sid]["state"] == "exited", 15)
                r = await lead.call(t="restart", sid=sid)
                assert r["ok"] and r["sid"] and r["sid"] != sid
                await until(lambda: lead.sessions().get(r["sid"], {}).get("state") == "live", 20)
                await until(lambda: sid not in lead.sessions(), 5)                          # the dead one was removed
    run(go())


def test_output_flood_is_bounded_and_keystrokes_stay_fast(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, mk = await pair(hub, "flood")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "flood")
                await lead.send(t="sub", sid=sid)
                await m.send(t="sub", sid=sid)
                t0 = time.time()
                await m.send(t="in", sid=sid, d="for /l %i in (1,1,12000) do @echo FLOODLINE %i the quick brown fox jumps over the lazy dog 0123456789\r")
                await until(lambda: "FLOODLINE 12000" in lead.out.get(sid, "") or "FLOODLINE 12000" in "".join(hub.hub.sessions[sid].ring), 90)
                secs = time.time() - t0
                s = hub.hub.sessions[sid]
                assert s.ring_len <= RING_CHARS + 8192, f"ring grew to {s.ring_len}"                # memory is bounded
                assert s.bytes_out > 600_000                                                          # ...even though >600 KB went through
                # after the flood, the shell still answers a keystroke quickly
                await until(lambda: "FLOODLINE 12000" in "".join(s.ring), 20)
                await asyncio.sleep(1.5)
                t1 = time.time()
                before = len(m.out.get(sid, ""))
                await m.send(t="in", sid=sid, d="z")
                await until(lambda: len(m.out.get(sid, "")) > before, 5)
                lat = (time.time() - t1) * 1000
                print(f"\nflood: {s.bytes_out/1024:.0f} KB relayed in {secs:.1f}s; ring capped at {s.ring_len} chars; post-flood keystroke {lat:.0f} ms")
                assert lat < 800
    run(go())


def test_unicode_round_trip(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, mk = await pair(hub, "unicode")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "unicode")
                await m.send(t="sub", sid=sid)
                await m.send(t="in", sid=sid, d="chcp 65001 >nul & echo héllo ✓ 日本語 ünï\r")
                await until(lambda: "日本語" in m.out.get(sid, "") and "héllo" in m.out.get(sid, ""), 10)
    run(go())


def test_many_sessions_across_machines(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            for n in ("m1", "m2", "m3"):
                await pair(hub, n)
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                sids = []
                t0 = time.time()
                for n in ("m1", "m2", "m3"):
                    for _ in range(4):
                        sids.append((await lead.call(t="start", node=lead.nodes()[n]["id"], provider="shell"))["sid"])
                await until(lambda: all(lead.sessions().get(s, {}).get("state") == "live" for s in sids), 60)
                took = time.time() - t0
                for s in sids:
                    await lead.send(t="sub", sid=s)
                await until(lambda: all(len(lead.out.get(s, "")) > 20 for s in sids), 20)
                print(f"\n12 sessions on 3 machines live and streaming in {took:.1f}s")
                assert len(sids) == 12
    run(go())


def test_bruteforce_pairing_is_locked_out(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            uri = hub.base.replace("http", "ws") + "/hub/node"
            codes = []
            for i in range(10):
                try:
                    async with websockets.connect(uri) as ws:
                        await ws.send(json.dumps({"t": "pair", "code": f"AAAA-BBBB-{i:04d}", "name": "x", "hostname": "x", "os": "x"}))
                        codes.append(json.loads(await asyncio.wait_for(ws.recv(), 3))["t"])
                except websockets.exceptions.ConnectionClosed as e:
                    codes.append(f"closed:{e.rcvd.code if e.rcvd else '?'}")
                except Exception as e:
                    codes.append("exc:" + type(e).__name__)
            assert codes[:8] == ["error"] * 8, codes
            assert any(str(c).startswith("closed:4429") or "exc" in str(c) for c in codes[8:]), codes     # locked out after 8 failures
            # a locked-out address also cannot guess lead keys
            with pytest.raises(Exception):
                async with websockets.connect(hub.base.replace("http", "ws") + f"/hub/viewer?role=lead&key={hub.lead_key}") as ws:
                    await asyncio.wait_for(ws.recv(), 3)
    run(go())


def test_hub_viewer_rejects_cross_site_origin(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            with pytest.raises(Exception):
                async with websockets.connect(hub.base.replace("http", "ws") + f"/hub/viewer?role=lead&key={hub.lead_key}",
                                              additional_headers={"Origin": "https://evil.example"}) as ws:
                    await asyncio.wait_for(ws.recv(), 3)
    run(go())


def test_end_to_end_keystroke_latency_through_the_brain(tmp_path):
    """lead keystroke -> hub -> node -> PTY echo -> node -> hub -> lead screen, in co-pilot."""
    async def go():
        with HubServer(tmp_path) as hub:
            _, _, mk = await pair(hub, "latency")
            async with Console(hub.base, "lead", hub.lead_key) as lead, Console(hub.base, "member", mk) as m:
                sid = await start_shell(lead, "latency")
                await lead.send(t="sub", sid=sid)
                await m.send(t="set_mode", sid=sid, mode="copilot", minutes=5)
                await until(lambda: lead.sessions()[sid]["mode"] == "copilot", 5)
                await asyncio.sleep(1.5)
                await lead.send(t="in", sid=sid, d="a")                           # warm up (first ConPTY write is slow)
                await asyncio.sleep(1.5)
                rtts = []
                for ch in "bcdefghijklmnopqrstuvwxyzbcdefghijklmnopqrstuvwxyz":
                    before = len(lead.out.get(sid, ""))
                    t0 = time.perf_counter()
                    await lead.send(t="in", sid=sid, d=ch)
                    await until(lambda: len(lead.out.get(sid, "")) > before, 5, step=0.001)
                    rtts.append((time.perf_counter() - t0) * 1000)
                    await asyncio.sleep(0.05)
                p50, p95 = statistics.median(rtts), sorted(rtts)[int(0.95 * len(rtts)) - 1]
                print(f"\nE2E lead->hub->node->PTY->hub->lead keystroke latency: p50={p50:.1f}ms p95={p95:.1f}ms max={max(rtts):.1f}ms (n={len(rtts)})")
                assert p95 < 150
    run(go())
