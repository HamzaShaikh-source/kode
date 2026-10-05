"""Edge cases: network drops, laptop / agent restarts, Brain restarts, flaky GitHub. Real Hub + real node processes + scripted agents.

Unit tests cover the decision logic (offline is not dead, transient errors back off and retry, a failed merge never strands a task);
the integration tests reproduce each failure for real and require the project to FINISH without a human.
"""
import asyncio
import json
import subprocess
import sys
import time

import pytest

import hub.project as hp
from hub.project import ProjectEngine
from llm import LLMResult
from tests.hubkit import Console, HubServer, until
from tests.test_project import StubHub, script, step

run = asyncio.run
win = pytest.mark.skipif(sys.platform != "win32", reason="demo fleet uses Windows shells")


# ───────────── unit: decisions ─────────────
class FakeNode:
    def __init__(self, online=True, connected_at=0.0):
        self.online, self.connected_at, self.name, self.member_name = online, connected_at, "Naman's laptop", "Naman"


class FakeSess:
    sid, label, state, bytes_out, last_out = "s1", "Claude Code", "offline", 10, 0.0
    def needs_input(self): return False


def engine_with(node):
    hub = StubHub()
    hub.nodes, hub.sessions = {"n1": node}, {"s1": FakeSess()}
    e = ProjectEngine(hub)
    p = {"id": "p", "name": "P", "cards": [], "log": [], "status": "running", "members": [{"node_id": "n1", "name": "Naman"}], "base": "main", "slug": "x", "permissions": "turbo"}
    t = {"id": "T1", "title": "x", "owner": "n1", "sid": "s1", "branch": "b", "status": "running", "attempts": 0, "nudges": 0, "min_commits": 2, "files_owned": ["a"], "weight": 1}
    return e, p, t


def test_offline_laptop_is_waited_for_not_restarted_or_blocked():
    async def go():
        e, p, t = engine_with(FakeNode(online=False))
        for _ in range(5):
            await e.tick_task(p, t)
            t["retry_at"] = 0                                                   # skip the backoff wait, tick again
        assert t["status"] == "running" and t["attempts"] == 0 and not t.get("restarts"), t
        assert "offline" in t["wait"] and not p["cards"]
        assert any(l["kind"] == "net" for l in p["log"])
    run(go())


def test_freshly_reconnected_laptop_gets_time_to_reannounce_its_sessions():
    async def go():
        e, p, t = engine_with(FakeNode(online=True, connected_at=time.time()))
        await e.tick_task(p, t)                                                   # session still 'offline' for a moment: not "dead"
        assert not t.get("restarts") and t["status"] == "running"
    run(go())


def test_transient_errors_back_off_and_only_block_after_the_grace_period(monkeypatch):
    async def go():
        e, p, t = engine_with(FakeNode())
        async def boom(): raise RuntimeError("git push failed: fatal: unable to access 'https://github.com/x': Could not resolve host")
        delays = []
        for _ in range(4):
            t["retry_at"] = 0
            await e._safe(p, t, boom())
            delays.append(round(t["retry_at"] - time.time()))
        assert t["status"] == "running" and t["attempts"] == 0 and delays == sorted(delays) and delays[-1] > delays[0], delays
        monkeypatch.setattr(hp, "NET_GRACE_S", 0.0)
        t["net_since"] = time.time() - 5
        await e._safe(p, t, boom())
        assert t["status"] == "blocked" and any(c["kind"] == "blocked" and "Retry" in c["text"] for c in p["cards"])
    run(go())


def test_real_failures_still_count_and_a_failed_merge_never_strands_a_task():
    async def go():
        e, p, t = engine_with(FakeNode())
        async def bad(): raise ValueError("the plan is nonsense")
        t["status"] = "review"
        await e._safe(p, t, bad())
        assert t["status"] == "running" and t["attempts"] == 1                       # was stuck in "review" forever before
        t["status"] = "review"
        async def net(): raise RuntimeError("gh pr merge failed: timed out")
        await e._safe(p, t, net())
        assert t["status"] == "running"
    run(go())


def test_coordinator_hands_over_when_its_laptop_stays_offline(monkeypatch):
    async def go():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s, *a: real_sleep(0))
        e, p, t = engine_with(FakeNode(online=False))
        p["coordinator"], p["forge"], p["repo"] = "n1", "local", "r"
        p["members"].append({"node_id": "n2", "name": "Ben", "gh_user": "ben"})
        e.hub.nodes["n2"] = FakeNode()
        calls = []
        async def must(p_, nid, name, timeout=0, **a): calls.append((nid, name)); return {"ok": True}
        e.must = must
        await e.ensure_coordinator(p)
        assert p["coordinator"] == "n2" and calls == [("n2", "git_prepare")]
        e.hub.nodes["n2"].online = False
        p["coordinator"] = "n1"
        with pytest.raises(RuntimeError, match="no laptop online"):
            await e.ensure_coordinator(p)
    run(go())


# ───────────── integration ─────────────
def plan(slow=2.5):
    def task(i, title, owner_files, steps, dep=()):
        return {"id": f"T{i}", "title": title, "weight": 1, "files_owned": owner_files, "depends_on": list(dep), "acceptance": "files exist",
                "description": title + script(steps).replace('"delay": 0.2', f'"delay": {slow}')}
    a = [step({f"a/f{i}.txt": f"a{i}\n"}, f"feat(a): file {i}") for i in range(1, 5)]
    b = [step({f"b/f{i}.txt": f"b{i}\n"}, f"feat(b): file {i}") for i in range(1, 4)]
    return {"summary": "two folders", "stack": "none", "setup_cmds": [],
            "test_cmds": ["python -c \"import glob; assert len(glob.glob('a/*.txt'))==4 and len(glob.glob('b/*.txt'))==3, 'missing files'\""],
            "contract_files": {"docs/CONTRACT.md": "# Contract\\nfolder a has 4 files, folder b has 3\\n"}, "tasks": [task(1, "folder a", ["a/**"], a), task(2, "folder b", ["b/**"], b)]}


class Plan:
    def __init__(self, slow): self.slow = slow
    async def complete(self, system, user, *, max_tokens=0): return LLMResult(json.dumps(plan(self.slow)), 1, 1, "stop")


async def start_project(hub, slow=2.5):
    names = {"Asha": "asha@bugged.dev", "Ben": "ben@bugged.dev"}
    cfgs = {}
    async with Console(hub.base, "lead", hub.lead_key) as lead:
        for n, mail in names.items():
            r = await lead.call(t="create_invite", member=n, access="full")
            _, cfgs[n] = hub.spawn_node(n, code=r["code"], accept_access=True, git=(n, mail))
        await until(lambda: len([x for x in lead.nodes().values() if x["online"]]) == 2, 40)
        hub.hub.projects.llm_override = Plan(slow)
        members = [{"node_id": x["id"], "provider": "demo-agent"} for x in lead.nodes().values()]
        r = await lead.call(t="project_create", name="Resilience", brief="two folders of files", forge="local", members=members, auto_approve_plan=True, permissions="turbo", planner={"kind": "default"})
        assert r["ok"], r
        return r["id"], cfgs, {x["name"]: x["id"] for x in lead.nodes().values()}


def proj(hub, pid):
    return hub.hub.projects.items[pid]


def texts(p): return [l["text"] for l in p["log"]]


@win
def test_wifi_blip_keeps_the_agent_session_and_the_project_finishes(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            pid, cfgs, ids = await start_project(hub)
            p = proj(hub, pid)
            await until(lambda: any(t["status"] == "running" and t.get("sid") and t["commits"] >= 1 for t in p["tasks"]), 90, step=0.5)
            t = next(t for t in p["tasks"] if t["status"] == "running" and t.get("sid"))
            sid, nid = t["sid"], t["owner"]
            node = hub.hub.nodes[nid]
            asyncio.run_coroutine_threadsafe(node.ws.close(code=1012), hub.hub.loop).result(5)          # the connection drops mid-task
            await until(lambda: not hub.hub.nodes[nid].online or hub.hub.nodes[nid].connected_at > node.connected_at, 10, step=0.1)
            await until(lambda: hub.hub.nodes[nid].online, 30)                                          # the agent reconnects by itself
            await until(lambda: p["status"] in ("done", "failed"), 200, step=1)
            assert p["status"] == "done", (p["status"], p.get("error"), texts(p)[-8:])
            assert t["sid"] == sid, "the SAME terminal session must survive a network blip"
            assert not any("restarting" in x for x in texts(p)), texts(p)
            assert all(x["status"] == "merged" for x in p["tasks"]) and t["attempts"] == 0
    run(go())


@win
def test_killed_agent_process_is_recovered_on_the_same_branch(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            pid, cfgs, ids = await start_project(hub)
            p = proj(hub, pid)
            await until(lambda: any(t["status"] == "running" and t.get("sid") and t["commits"] >= 1 for t in p["tasks"]), 90, step=0.5)
            t = next(t for t in p["tasks"] if t["status"] == "running" and t.get("sid"))
            name = next(n for n, i in ids.items() if i == t["owner"])
            branch = t["branch"]
            proc = hub.nodes[0] if name == "Asha" else hub.nodes[1]
            proc.kill()                                                                                  # the laptop's agent dies (crash, reboot, closed lid)
            await until(lambda: not hub.hub.nodes[t["owner"]].online, 40, step=0.5)
            await asyncio.sleep(3)
            newp, _ = hub.spawn_node(name, config=cfgs[name], git=(name, f"{name.lower()}@bugged.dev"), accept_access=True)   # the teammate starts it again
            hub.nodes.append(newp)
            await until(lambda: hub.hub.nodes[t["owner"]].online, 40)
            await until(lambda: p["status"] in ("done", "failed"), 240, step=1)
            assert p["status"] == "done", (p["status"], p.get("error"), texts(p)[-10:])
            assert t["branch"] == branch and t["status"] == "merged"
            assert any("restarting" in x or "back online" in x for x in texts(p))
    run(go())


@win
def test_brain_restart_resumes_the_running_project_by_itself(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            pid, cfgs, ids = await start_project(hub, slow=3.0)
            p = proj(hub, pid)
            await until(lambda: any(t["status"] == "running" and t.get("sid") and t["commits"] >= 1 for t in p["tasks"]), 90, step=0.5)
            await asyncio.get_running_loop().run_in_executor(None, hub.restart_hub)                      # the lead's PC restarts the Brain
            await until(lambda: pid in hub.hub.projects.items, 10)
            p2 = proj(hub, pid)
            assert p2["status"] in ("paused", "running")
            await until(lambda: all(n.online for n in hub.hub.nodes.values()), 60)                       # laptops reconnect on their own
            await until(lambda: p2["status"] == "running" or p2["status"] == "done", 90, step=0.5)       # ... and the project resumes with no click
            await until(lambda: p2["status"] in ("done", "failed"), 240, step=1)
            assert p2["status"] == "done", (p2["status"], p2.get("error"), texts(p2)[-10:])
            assert any("resuming" in x.lower() for x in texts(p2))
    run(go())


@win
def test_starting_the_agent_twice_leaves_exactly_one_running(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                r = await lead.call(t="create_invite", member="Asha", access="full")
                first, cfg = hub.spawn_node("Asha", code=r["code"], accept_access=True)
                await until(lambda: lead.nodes().get("Asha", {}).get("online"), 30)
                second, _ = hub.spawn_node("Asha", config=cfg, accept_access=True)                   # the teammate double-clicks start.ps1
                hub.nodes.append(second)
                await until(lambda: first.poll() is not None, 30, step=0.3)                             # the OLD agent steps aside instead of fighting
                assert second.poll() is None and lead.nodes()["Asha"]["online"]
                await asyncio.sleep(8)
                assert second.poll() is None and lead.nodes()["Asha"]["online"], "the newer agent must stay connected"
    run(go())


@win
def test_agent_with_a_revoked_token_exits_with_the_new_invite_code(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                r = await lead.call(t="create_invite", member="Asha", access="full")
                proc, cfg = hub.spawn_node("Asha", code=r["code"], accept_access=True)
                await until(lambda: lead.nodes().get("Asha", {}).get("online"), 30)
                await lead.send(t="remove_node", node=lead.nodes()["Asha"]["id"])                    # the lead removes the machine
                await until(lambda: proc.poll() is not None, 40, step=0.5)
                assert proc.returncode == 3, proc.returncode                                           # start scripts stop looping on 3: only a new invite helps
    run(go())


# ───────────── a teammate sets things up AFTER joining ─────────────
def test_tool_installed_after_the_agent_started_is_picked_up(tmp_path, monkeypatch):
    """The reported bug: the agent only looked at what was installed when it connected, and a running Windows program never sees a new PATH."""
    import os
    from bridge.manifests import load_manifests, refresh_path, scan
    monkeypatch.setenv("PATH", os.environ["PATH"])                                                      # refresh_path() edits PATH: restore it afterwards
    mdir, tools = tmp_path / "manifests", tmp_path / "tools"
    mdir.mkdir(); tools.mkdir()
    (mdir / "fake.yaml").write_text("id: fake\nlabel: Fake Agent\ncommand: [kode-fake-tool]\n", encoding="utf-8")
    monkeypatch.setenv("KODE_MANIFEST_DIR", str(mdir))
    monkeypatch.setenv("KODE_EXTRA_PATH_FILE", str(tmp_path / "extra.txt"))
    import bridge.manifests as bm
    monkeypatch.setattr(bm, "MANIFEST_DIR", mdir)
    ms = load_manifests(mdir)
    assert [p["available"] for p in scan(ms, probe_auth=False)] == [False]
    exe = tools / ("kode-fake-tool.cmd" if sys.platform == "win32" else "kode-fake-tool")
    exe.write_text("@echo off\r\n" if sys.platform == "win32" else "#!/bin/sh\n", encoding="utf-8")
    if sys.platform != "win32":
        exe.chmod(0o755)
    (tmp_path / "extra.txt").write_text(str(tools) + "\n", encoding="utf-8")                         # "installed" + PATH changed elsewhere
    assert refresh_path() is True
    assert [p["available"] for p in scan(ms, probe_auth=False)] == [True]
    assert refresh_path() is False                                                                      # idempotent


@win
def test_agent_reports_a_newly_installed_tool_without_a_restart_or_new_invite(tmp_path, monkeypatch):
    async def go():
        mdir, tools = tmp_path / "manifests", tmp_path / "tools"
        mdir.mkdir(); tools.mkdir()
        (mdir / "shell.yaml").write_text("id: shell\nlabel: Shell\ncommand: [cmd.exe]\n", encoding="utf-8")
        (mdir / "fake.yaml").write_text("id: fake\nlabel: Fake Agent\ncommand: [kode-fake-tool]\n", encoding="utf-8")
        monkeypatch.setenv("KODE_MANIFEST_DIR", str(mdir))
        monkeypatch.setenv("KODE_EXTRA_PATH_FILE", str(tmp_path / "extra.txt"))
        monkeypatch.setenv("KODE_RESCAN_S", "1.5")
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                r = await lead.call(t="create_invite", member="Friend", access="full")
                hub.spawn_node("Friend", code=r["code"], accept_access=True)
                await until(lambda: lead.nodes().get("Friend", {}).get("online"), 30)
                prov = lambda: {p["id"]: p["available"] for p in lead.nodes()["Friend"]["providers"]}
                assert prov().get("fake") is False                                                       # not installed yet
                (tools / "kode-fake-tool.cmd").write_text("@echo off\r\n", encoding="utf-8")               # the friend installs it later...
                (tmp_path / "extra.txt").write_text(str(tools) + "\n", encoding="utf-8")
                await until(lambda: prov().get("fake") is True, 20, step=0.3)                            # ...and it just appears
                assert any("now available" in e["text"] for e in lead.state["events"])
                await lead.call(t="rescan_node", node=lead.nodes()["Friend"]["id"])                       # the Rescan button works too
    run(go())


@win
def test_fresh_invite_beats_a_stale_saved_login_and_replaces_the_old_entry(tmp_path):
    """The reported 'auth failed': rerunning the installer with a NEW invite used the old saved token and ignored the code."""
    async def go():
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                cfg = tmp_path / "stale.json"
                cfg.write_text(json.dumps({"node_id": "n_gone", "token": "stale-token", "member_key": "x", "access_accepted": "full"}), encoding="utf-8")
                r = await lead.call(t="create_invite", member="Friend", access="full")
                hub.spawn_node("Friend", code=r["code"], config=cfg, accept_access=True)
                await until(lambda: lead.nodes().get("Friend", {}).get("online"), 30)                    # joined despite the stale token
                assert json.loads(cfg.read_text())["token"] != "stale-token"
                # the same person rejoins later from the same machine with a brand-new registration: one entry, not two
                hub.nodes[-1].kill()
                await until(lambda: not lead.nodes()["Friend"]["online"], 40, step=0.5)
                r2 = await lead.call(t="create_invite", member="Friend", access="full")
                hub.spawn_node("Friend", code=r2["code"], config=tmp_path / "fresh.json", accept_access=True)
                await until(lambda: any(n["online"] for n in lead.nodes().values()), 30)
                await until(lambda: len(lead.state["nodes"]) == 1, 10)
    run(go())


@win
def test_a_bad_invite_falls_back_to_the_saved_registration(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                r = await lead.call(t="create_invite", member="Friend", access="full")
                p1, cfg = hub.spawn_node("Friend", code=r["code"], accept_access=True)
                await until(lambda: lead.nodes().get("Friend", {}).get("online"), 30)
                p1.kill()
                await until(lambda: not lead.nodes()["Friend"]["online"], 40, step=0.5)
                hub.spawn_node("Friend", code="DEAD-BEEF-0000", config=cfg, accept_access=True)         # an expired / wrong code must not lock them out
                await until(lambda: lead.nodes()["Friend"]["online"], 30)
    run(go())
