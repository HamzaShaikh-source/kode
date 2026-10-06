"""Stuck-task handling, backups, who gets what, typing hygiene, context attachments, limits, JSON repair."""
import asyncio
import base64
import json
import time

import pytest

import hub.project as hp
from hub import context as cx
from hub.project import ProjectEngine, pick_skills, render_task
from llm import LLMResult, extract_json
from tests.test_project import StubHub

run = asyncio.run


# ───────── planner JSON is repaired locally, not re-asked ─────────
def test_json_with_raw_newlines_and_trailing_commas_is_repaired():
    raw = '{"summary": "line one\nline two\ttabbed", "tasks": [1, 2,], }'
    assert extract_json(raw)["summary"].startswith("line one")
    assert extract_json('Sure! ```json\n{"a": "x\ny"}\n```')["a"] == "x\ny"


# ───────── engine fixtures ─────────
class Sess:
    def __init__(self, text="", busy=False, last_out=None):
        self.ring, self.state, self.mode = [text], "live", "copilot"
        self.last_out = time.time() - 60 if last_out is None else last_out
        self.bytes_out, self.sid, self.label = len(text), "s1", "Claude Code"
        self.busy = busy

    def needs_input(self):
        return False


class FakeNode:
    def __init__(self, providers=(), online=True):
        self.member_name, self.name = "", "laptop"
        self.online, self.connected_at, self.providers = online, 0, [{"id": x, "available": True, "signed_in": True} for x in providers]


def engine():
    hub = StubHub()
    hub.nodes = {}
    hub.sessions = {}
    return ProjectEngine(hub)


def proj(members=None):
    members = members or [{"node_id": "n1", "name": "Asha", "provider": "claude", "pool": ["claude", "codex"]}, {"node_id": "n2", "name": "Ben", "provider": "codex", "pool": ["codex"]}]
    return {"id": "p1", "name": "X", "members": members, "tasks": [], "cards": [], "log": [], "base": "main", "round": 0, "backup": [], "status": "running", "phase": "build",
            "coordinator": "n1", "contract_paths": [], "repo": "o/r", "forge": "local", "permissions": "turbo", "slug": "x"}


def task(tid="T1", owner="n1", status="running", **kw):
    t = {"id": tid, "title": "t", "description": "d", "owner": owner, "status": status, "sid": "s1", "branch": "b", "files_owned": ["a"], "depends_on": [], "weight": 1, "nudges": 0,
         "attempts": 0, "min_commits": 1, "restarts": 0}
    t.update(kw)
    return t


def test_who_gets_a_task_is_decided_from_precision_and_speed():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(), "n2": FakeNode()}
    p = proj()
    now = time.time()
    # Asha: two clean tasks, fast. Ben: two tasks, both needed restarts, slow.
    p["tasks"] = [task("A", "n1", "merged", started=now - 600, done_at=now - 300), task("B", "n1", "merged", started=now - 500, done_at=now - 200),
                  task("C", "n2", "merged", started=now - 900, done_at=now - 100, restarts=1), task("D", "n2", "merged", started=now - 900, done_at=now - 50, restarts=2)]
    nid, why = e.pick_owner(p)
    assert nid == "n1" and "100% first-time-right" in why
    assert e.pick_owner(p, exclude={"n1"})[0] == "n2"                          # exclusion wins when the best laptop cannot take it
    e.hub.nodes["n1"].online = False
    assert e.pick_owner(p)[0] == "n2"                                          # an offline laptop is not chosen


def test_backups_follow_the_leads_order_and_skip_agents_already_tried():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(["claude", "codex", "commandcode", "agy", "shell"])}
    p = proj()
    m = p["members"][0]
    assert e.backups_for(p, m, ["claude"]) == ["codex"]                       # the laptop's own pool first ...
    p["backup"] = ["commandcode"]
    assert e.backups_for(p, m, ["claude"])[0] == "commandcode"                 # ... unless the lead chose a backup agent
    assert "shell" not in e.backups_for(p, m, [])


def test_limit_failover_switches_agent_then_laptop_and_keeps_the_branch():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(["claude", "commandcode"]), "n2": FakeNode(["codex"])}
    p = proj()
    p["backup"] = ["commandcode"]
    t = task()
    p["tasks"] = [t]
    stopped = []

    async def stop(sid, who):
        stopped.append(sid)
    e.hub.stop_session = stop
    e.hub.sessions["s1"] = Sess()
    assert run(e.failover(p, t, p["members"][0], "hit its usage limit")) is True
    assert t["provider"] == "commandcode" and t["status"] == "pending" and t["owner"] == "n1" and stopped == ["s1"]
    assert any(l["kind"] == "failover" and "commandcode" in l["text"] for l in p["log"])
    assert t["branch"] == "b"                                                  # same branch: the new agent continues the existing work
    t.update(status="running", sid="s1")
    e.hub.sessions["s1"] = Sess()
    assert run(e.failover(p, t, p["members"][0], "hit its usage limit")) is True       # nothing left on that laptop: the task moves to a teammate
    assert t["owner"] == "n2" and t["provider"] is None


def test_retry_and_skip_work_on_stuck_cards():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(["claude"])}
    p = proj()
    t = task(status="running", nudges=3)
    p["tasks"] = [t]
    p["cards"] = [{"id": "c1", "kind": "stalled", "task": "T1", "text": "stuck", "sid": "s1"}, {"id": "c2", "kind": "stalled", "task": "T1", "text": "stuck", "sid": "s1"}]

    async def stop(sid, who):
        pass
    e.hub.stop_session = stop
    e.hub.sessions["s1"] = Sess()
    run(e.answer_card(p, p["cards"][0], "retry"))
    assert t["status"] == "pending" and t["sid"] == "" and t["restarts"] == 1 and p["cards"] == [], "retry restarts the task and clears its cards"
    t.update(status="running", sid="s1")
    p["cards"] = [{"id": "c3", "kind": "stalled", "task": "T1", "text": "stuck", "sid": "s1"}]
    run(e.answer_card(p, p["cards"][0], "skip"))
    assert t["status"] == "merged" and t["skipped"] and p["cards"] == []


def test_the_planner_decides_what_to_do_with_a_stuck_task():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(["claude"]), "n2": FakeNode(["codex"])}
    p = proj()
    t = task(nudges=2, last_progress=time.time() - 900)
    p["tasks"] = [t]
    e.hub.sessions["s1"] = Sess("working on the thing")
    sent = []

    async def op(nid, name, timeout=0, **a):
        return {"ok": True, "ahead": 0, "dirty": False, "pushed": False}
    e.op = op

    async def stop(sid, who):
        pass
    e.hub.stop_session = stop
    prompts = []

    class Planner:
        async def complete(self, system, user, *, max_tokens=0):
            prompts.append(user)
            return LLMResult(json.dumps({"action": "reassign", "to": "Ben", "reason": "Asha's agent is looping"}), 1, 1, "stop")
    e.llm_override = Planner()
    run(e.decide_stuck(p, t))
    assert t["owner"] == "n2" and t["status"] == "pending" and t["decisions"] == 1
    assert "AGENT SCREEN" in prompts[0] and "TEAM:" in prompts[0] and "working on the thing" in prompts[0]      # it saw the screen, the history and the team
    assert p["usage"]["decisions"]["calls"] == 1                                                              # and the call was counted


# ───────── typing into agents ─────────
def test_a_reminder_is_never_queued_twice_or_typed_into_a_busy_agent():
    e = engine()
    typed = []

    async def type_line(s, text):
        typed.append(text)
    e.type_line = type_line
    busy = Sess("Working (14s • esc to interrupt)", last_out=time.time())
    assert run(e.send_prompt(busy, "Status check: commit", "Status check:")) is False and typed == []
    waiting = Sess("› Status check: commit your work\n› Ask Codex to do anything")
    assert run(e.send_prompt(waiting, "Status check: commit", "Status check:")) is False and typed == []     # already sitting in the input box
    idle = Sess("done with that\n› ")
    assert run(e.send_prompt(idle, "Status check: commit", "Status check:")) is True and typed == ["Status check: commit"]


def test_limit_and_api_error_screens_are_recognised():
    assert hp.LIMIT_RX.search("You've hit your usage limit. Try again at 9:40 PM")
    assert hp.LIMIT_RX.search("Error: 429 Too Many Requests")
    assert hp.API_ERR.search("API error · Retrying in 0s · attempt 1/10")
    assert not hp.LIMIT_RX.search("wrote tests for the login flow")


# ───────── skills in the task, context attachments ─────────
def test_task_lists_only_relevant_skills():
    skills = [{"name": "test-driven-development", "description": "TDD"}, {"name": "baking-bread", "description": "bread"}, {"name": "design-taste-frontend", "description": "frontend design landing pages"}]
    got = [s["name"] for s in pick_skills(skills, "build the frontend landing page with tests")]
    assert "design-taste-frontend" in got and "test-driven-development" in got and "baking-bread" not in got


def test_context_files_links_and_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(cx, "DATA", tmp_path)
    p = {"id": "p_ctx"}
    it = cx.add_file(p, "rules.md", "text/markdown", base64.b64encode(b"# Rules\nJudged on impact and demo.").decode())
    assert it["kind"] == "file" and "impact" in it["text"]
    img = cx.add_file(p, "logo.png", "image/png", base64.b64encode(b"\x89PNG....").decode())
    assert img["kind"] == "image" and not img["text"]
    cx.add_note(p, "Prize: best use of voice")
    assert "Rules" in cx.summary(p) and "open it in .kode/context/" in cx.summary(p) and "best use of voice" in cx.summary(p)
    payload = cx.files_payload(p)
    assert "INDEX.md" in payload and any(k.endswith("rules.md") for k in payload)
    with pytest.raises(cx.ContextError):
        cx.add_file(p, "huge.bin", "x", base64.b64encode(b"0" * (cx.MAX_FILE + 1)).decode())
    cx.remove(p, it["id"])
    assert all(i["id"] != it["id"] for i in p["context"])
    assert [i["name"] for i in cx.public(p)] and "text" not in cx.public(p)[0]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/x", "http://localhost/", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "ftp://example.com/x", "file:///etc/passwd"])
def test_links_to_private_addresses_are_refused(url):
    with pytest.raises(cx.ContextError):
        cx.check_public_url(url)


def test_task_file_carries_the_do_and_do_not_lists_and_context():
    p = proj()
    p["brief"], p["context"] = "Build X", [{"id": "x1", "kind": "note", "name": "Rules", "text": "Judged on demo", "file": ""}]
    t = task(must=["export score()"], must_not=["edit app/main.py"])
    md = render_task(p, t, "kode/t1", [], [{"name": "test-driven-development", "description": "tdd"}])
    assert "## You must" in md and "export score()" in md and "## You must not" in md and "edit app/main.py" in md and "Judged on demo" in md and "test-driven-development" in md


# ───────── usage limits read from the agents' own logs ─────────
def test_limits_come_from_codex_rollouts_and_claude_session_logs(tmp_path, monkeypatch):
    from node import ops
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "05"
    day.mkdir(parents=True)
    (day / "rollout-x.jsonl").write_text(json.dumps({"payload": {"info": {}, "rate_limits": {"primary": {"used_percent": 41.0, "window_minutes": 300, "resets_at": 1}, "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": 2}, "plan_type": "plus"}}}) + "\n")
    cl = tmp_path / ".claude" / "projects" / "proj"
    cl.mkdir(parents=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 600))
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3 * 86400))
    (cl / "s.jsonl").write_text(json.dumps({"timestamp": ts, "message": {"usage": {"input_tokens": 10, "output_tokens": 200, "cache_read_input_tokens": 5}}}) + "\n" +
                                json.dumps({"timestamp": old, "message": {"usage": {"input_tokens": 1, "output_tokens": 50}}}) + "\n")
    out = ops.scan_limits()
    assert out["codex"]["primary"]["used_percent"] == 41.0 and out["codex"]["secondary"]["window_minutes"] == 10080 and out["codex"]["plan"] == "plus"
    assert out["claude"]["last_5h"]["out"] == 200 and out["claude"]["last_7d"]["out"] == 250 and out["claude"]["messages_5h"] == 1
    assert "agy" in out and "does not expose" in out["agy"]["note"]


# ───────── menus, swallowed prompts, planner visibility ─────────
def test_agent_menus_are_not_mistaken_for_work():
    assert hp.NAVDLG.search("Select model\n↑/↓ Navigate · enter Confirm")
    assert hp.DISMISS.search("┃ Gemini 3.6 & 3.7 Flash Deprecation\n┃ esc to dismiss")
    assert not hp.NAVDLG.search("Working (14s • esc to interrupt)")


def test_an_agent_that_swallowed_its_first_prompt_gets_it_again(monkeypatch):
    e = engine()
    p = proj()
    t = task(sid="s1", mark0=0)
    s = Sess("┃ esc to dismiss\n> ")
    e.hub.sessions["s1"] = s
    keys, typed = [], []

    async def lead_input(actor, sess, text, label=""):
        keys.append(text)

    async def ensure_copilot(sess):
        pass

    async def type_line(sess, text):
        typed.append(text)
    e.hub.lead_input, e.hub.ensure_copilot, e.type_line = lead_input, ensure_copilot, type_line
    e.hub.orch = type("O", (), {"actor": "orchestrator"})()
    real = asyncio.sleep

    async def fast(x, *a):
        await real(0)
    monkeypatch.setattr(hp.asyncio, "sleep", fast)
    run(e.ensure_started(p, t, s, "Read .kode/TASK.md and do the task"))
    assert keys and keys[0] == hp.KEYMAP["esc"] and typed[0] == "Read .kode/TASK.md and do the task" and len(typed) <= 2
    assert any("had not started" in l["text"] for l in p["log"])


def test_the_console_is_told_which_model_works_on_which_prompt():
    e = engine()
    e.hub.nodes = {"n1": FakeNode(["claude"])}
    e.hub.nodes["n1"].member_name = "Asha"
    p = proj()
    seen = []

    class Inner:
        provider, node_id = "claude", "n1"

        async def complete(self, system, user, *, max_tokens=0):
            seen.append([c["title"] for c in p["live_calls"]])                # visible while the model is thinking
            return LLMResult("answer", 5, 5, "stop")
    run(e.metered(p, Inner(), "plan").complete("sys", "user text"))
    assert seen == [["Writing the plan and the contract"]] and p["live_calls"] == []
    c = p["planner_calls"][-1]
    assert c["ok"] and c["who"] == "Claude Code on Asha" and c["in_chars"] == 12 and c["out_chars"] == 6

    class Bad(Inner):
        async def complete(self, system, user, *, max_tokens=0):
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        run(e.metered(p, Bad(), "triage").complete("a", "b"))
    assert p["planner_calls"][-1]["ok"] is False and "boom" in p["planner_calls"][-1]["err"] and p["live_calls"] == []


def test_planner_conversation_survives_a_brain_restart():
    from hub.project import CliClient
    e = engine()
    calls = []

    async def node_op(nid, name, timeout=0, **a):
        calls.append(a)
        if not a.get("resume") and len([c for c in calls if not c.get("resume")]) >= 1 and len(calls) > 1 and a.get("session_id") == calls[0]["session_id"]:
            return {"ok": False, "err": "claude failed (exit 1): Error: Session ID x is already in use."}
        if len(calls) == 1:
            return {"ok": False, "err": "claude failed (exit 1): Error: Session ID x is already in use."}
        return {"ok": True, "text": "{}"}
    e.hub.node_op = node_op
    c = CliClient(e, "claude", "n1", session="p1")
    r = run(c.complete("sys", "user"))
    assert r.text == "{}" and calls[0]["resume"] is False and calls[1]["resume"] is True and calls[1]["session_id"] == calls[0]["session_id"]
    assert e.psess["p1"] is True                                                           # later calls resume straight away


def test_codex_never_updates_itself_in_the_middle_of_a_task():
    from hub import apiprofiles
    args = apiprofiles.project_args("codex", True, True)
    assert "check_for_update_on_startup=false" in args and apiprofiles.validate_launch({}, args) is None
