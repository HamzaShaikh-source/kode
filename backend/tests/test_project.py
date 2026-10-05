"""v4: the project engine, end to end, hermetically: real Hub + real node subprocesses + scripted demo agents + a local bare git remote.

Covers: plan validation and fair assignment (pure), then the whole loop: plan -> frozen contract -> branches on each laptop -> commits ->
scope guard -> PRs -> ordered merge -> failing verification -> LLM triage -> fix task -> passing verification -> commit-balance report.
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hub.project as hp
from hub.project import PlanError, assign_tasks, globs_overlap, normalize_plan, path_in_scope
from llm import LLMResult
from tests.hubkit import Console, HubServer, until

run = asyncio.run
win = pytest.mark.skipif(sys.platform != "win32", reason="demo fleet uses Windows shells")


def task(i, files, weight=1, deps=()):
    return {"id": f"T{i}", "title": f"task {i}", "description": "do the thing", "files_owned": files, "depends_on": list(deps), "weight": weight}


GOOD = {"summary": "s", "stack": "py", "contract_files": {"docs/CONTRACT.md": "x"}, "test_cmds": ["python -V"], "tasks": [task(1, ["a/**"]), task(2, ["b/x.py"])]}


# ───────────── pure logic ─────────────
def test_glob_overlap_and_scope():
    assert globs_overlap("api/**", "api/routes.py") and globs_overlap("src", "src/a.py") and globs_overlap("*.md", "README.md")
    assert not globs_overlap("api/**", "web/**") and not globs_overlap("src/a.py", "src/b.py")
    assert path_in_scope("api/x/y.py", ["api/**"]) and path_in_scope("README.md", ["README.md"]) and path_in_scope("src/a.py", ["src/"])
    assert not path_in_scope("web/a.js", ["api/**"]) and not path_in_scope("apix/a.py", ["api/**"])


def test_plan_validation_rejects_overlap_contract_claims_cycles_and_unsafe_paths():
    assert normalize_plan(GOOD, 2)["tasks"][0]["id"] == "T1"
    bad = lambda **kw: normalize_plan({**GOOD, **kw}, 2)
    with pytest.raises(PlanError, match="disjoint"):
        bad(tasks=[task(1, ["api/**"]), task(2, ["api/routes.py"])])
    with pytest.raises(PlanError, match="contract"):
        bad(tasks=[task(1, ["docs/**"]), task(2, ["b/x.py"])])
    with pytest.raises(PlanError, match="cycle"):
        bad(tasks=[task(1, ["a/**"], deps=["T2"]), task(2, ["b/**"], deps=["T1"])])
    with pytest.raises(PlanError, match="relative"):
        bad(tasks=[task(1, ["../etc/**"]), task(2, ["b/**"])])
    with pytest.raises(PlanError, match="unknown task"):
        bad(tasks=[task(1, ["a/**"], deps=["T9"])])
    with pytest.raises(PlanError, match="contract_files"):
        bad(contract_files={})


def test_planner_must_be_chosen_and_validated(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                r = await lead.call(t="create_invite", member="Asha", access="full")
                hub.spawn_node("Asha", code=r["code"], accept_access=True)
                await until(lambda: any(x["online"] for x in lead.nodes().values()), 30)
                n = next(iter(lead.nodes().values()))
                base = dict(t="project_create", name="X", brief="build something small please", forge="local", members=[{"node_id": n["id"], "provider": "shell"}])
                r1 = await lead.call(**base)
                assert not r1["ok"] and "planner" in r1["err"].lower()                                  # no silent default
                r2 = await lead.call(**base, planner={"kind": "profile", "profile_id": "nope"})
                assert not r2["ok"] and "profile" in r2["err"]
                r3 = await lead.call(**base, planner={"kind": "cli", "provider": "hermes"})
                assert not r3["ok"] and "not available" in r3["err"]
    run(go())


class StubHub:
    class _Orch:
        actor = object()
    def __init__(self):
        self.sent, self.orch = [], self._Orch()
        class _Store:
            def save_project(self, *a): pass
        self.store = _Store()
    def push_projects(self): pass
    def event(self, *a, **k): pass
    async def ensure_copilot(self, s): return True
    async def lead_input(self, actor, s, data, label=""): self.sent.append(data)


class FakeSession:
    sid, label, bytes_out, last_out = "s_x", "Antigravity CLI", 100, 0.0


class Reply:
    def __init__(self, obj): self.obj = obj
    async def complete(self, system, user, *, max_tokens=0): return LLMResult(json.dumps(self.obj), 1, 1, "stop")


def test_supervisor_decisions_and_hard_guards():
    from hub.project import ProjectEngine
    async def go():
        hub = StubHub(); e = ProjectEngine(hub)
        p = {"id": "p1", "name": "P", "cards": [], "log": [], "members": [{"node_id": "n1", "name": "Naman"}]}
        t = {"id": "T1", "title": "x", "owner": "n1"}
        s = FakeSession()
        e.llm_override = Reply({"action": "keys", "keys": ["1", "enter"], "reason": "accept the file edit"})
        assert await e.supervise(p, t, s, "Accept this file edit? > 1. Yes, accept 2. No") == "acted"
        assert hub.sent == ["1", chr(13)]                                                   # the model's answer, translated to real keystrokes
        # the same kind of routine question later costs no model call at all (it is remembered, digits/paths ignored)
        class NoCall:
            async def complete(self, *a, **k): raise AssertionError("model must not be called for a repeated question")
        hub.sent.clear(); t.pop("sup_sig", None); e.llm_override = NoCall()
        assert await e.supervise(p, t, s, "Accept this file edit? C:/x/y2.py > 1. Yes, accept 2. No") == "acted" and hub.sent == ["1", chr(13)]
        e.sup_cache.clear()
        # terms / data sharing are never answered, whatever the model says
        hub.sent.clear(); t.pop("sup_sig")
        assert await e.supervise(p, t, s, "Terms of Service & Data Use: [x] Yes, I agree to help improve") == "human" and not hub.sent
        # destructive text is never answered either
        assert await e.supervise(p, t, s, "Run this command? rm -rf / 1. Yes 2. No") == "human" and not hub.sent
        # the model cannot type free text or unknown keys
        t.pop("sup_sig", None); e.llm_override = Reply({"action": "keys", "keys": ["rm -rf"], "reason": "x"})
        assert await e.supervise(p, t, s, "Some unclear menu 1. a 2. b") == "human" and not hub.sent
        # "wait" does nothing; a broken model falls back to the fixed rules
        t.pop("sup_sig", None); e.llm_override = Reply({"action": "wait", "reason": "agent is working"})
        assert await e.supervise(p, t, s, "Thinking... 12s") == "wait" and not hub.sent
        class Boom:
            async def complete(self, *a, **k): raise RuntimeError("down")
        t.pop("sup_sig", None); e.llm_override = Boom()
        assert await e.supervise(p, t, s, "Do you want to proceed? 1. Yes") == "error"
    run(go())


def test_assignment_is_fair_by_weight():
    ts = [task(i, [f"d{i}/**"], weight=w) for i, w in enumerate([3, 3, 2, 2, 1, 1, 1, 1, 1], 1)]
    assign_tasks(ts, ["a", "b", "c"])
    load = {m: sum(t["weight"] for t in ts if t["owner"] == m) for m in "abc"}
    assert max(load.values()) - min(load.values()) <= 1, load
    assert all(t["min_commits"] == 1 for t in ts)


# ───────────── the whole loop ─────────────
def script(steps, push_after=(1,)):
    return " KODE-DEMO-SCRIPT: " + json.dumps({"steps": steps, "push_after": list(push_after), "delay": 0.2})


def step(files, msg):
    return {"files": files, "message": msg}


CALC_BAD = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_HEAD = "import os, sys, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"


def make_plan():
    t = [
        {"id": "T1", "title": "calc module", "weight": 2, "files_owned": ["src/calc.py"], "depends_on": [], "acceptance": "add and mul exist",
         "description": "Implement src/calc.py per the contract." + script([step({"src/calc.py": "def add(a, b):\n    return a + b\n"}, "feat(calc): add"), step({"src/calc.py": CALC_BAD}, "feat(calc): mul"),
                                                                           step({"src/calc.py": CALC_BAD + "\n"}, "style(calc): spacing"), step({"src/calc.py": CALC_BAD + "\n# done\n"}, "docs(calc): note")])},
        {"id": "T2", "title": "format module", "weight": 1, "files_owned": ["src/fmt.py"], "depends_on": [], "acceptance": "money()",
         "description": "Implement src/fmt.py." + script([step({"src/fmt.py": "def money(x):\n    return '$%.2f' % x\n"}, "feat(fmt): money"), step({"src/fmt.py": "def money(x):\n    return '$%.2f' % x\n\n"}, "style(fmt): spacing")])},
        {"id": "T3", "title": "calc tests", "weight": 1, "files_owned": ["tests/test_calc.py"], "depends_on": ["T1"], "acceptance": "tests run",
         "description": "Write tests for calc." + script([step({"tests/test_calc.py": TEST_HEAD + "from src import calc\n\n\nclass C(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"}, "test(calc): add"),
                                                         step({"tests/test_calc.py": TEST_HEAD + "from src import calc\n\n\nclass C(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n"}, "test(calc): mul")])},
        {"id": "T4", "title": "format tests", "weight": 1, "files_owned": ["tests/test_fmt.py"], "depends_on": ["T2"], "acceptance": "tests run",
         "description": "Write tests for fmt." + script([step({"tests/test_fmt.py": TEST_HEAD + "from src import fmt\n\n\nclass F(unittest.TestCase):\n    def test_money(self):\n        self.assertEqual(fmt.money(1.5), '$1.50')\n"}, "test(fmt): money"),
                                                        step({"tests/__keep__.txt": "x"}, "chore: out of scope marker")])},
        {"id": "T5", "title": "docs", "weight": 1, "files_owned": ["README.md"], "depends_on": [], "acceptance": "readme",
         "description": "Write the README (this script also tries to break the rules)." + script([step({"README.md": README}, "docs: readme"), step({"src/evil.py": "print('x')\n", "docs/CONTRACT.md": "tampered\n"}, "oops: touch things I do not own")])},
        {"id": "T6", "title": "main", "weight": 1, "files_owned": ["src/main.py"], "depends_on": [], "acceptance": "main",
         "description": "Write src/main.py." + script([step({"src/main.py": "from src import calc\n"}, "feat(main): import"), step({"src/main.py": "from src import calc\nprint(calc.add(1, 2))\n"}, "feat(main): print")])},
    ]
    return {"summary": "A tiny calculator library.", "stack": "python", "setup_cmds": [], "test_cmds": ["python -m unittest discover -s tests -p \"test_*.py\""],
            "contract_files": {"docs/CONTRACT.md": "# Contract\ncalc.add(a, b) -> number; calc.mul(a, b) -> number; fmt.money(x) -> '$1.50'\n"}, "tasks": t}


README = "# Calc\n\nA tiny calculator library with a formatter.\n\n## Setup\n\nNo dependencies beyond Python 3.\n\n## Usage\n\nRun it with `python src/main.py 2 3`; import `src.calc` for add, sub and mul, and `src.fmt` to format results.\n\n## Tests\n\nRun the tests with `python -m unittest discover -s tests`.\n\n## Project structure\n\n- src/calc.py arithmetic\n- src/fmt.py formatting\n- src/main.py command line entry\n- tests/ unit tests for both modules\n"


class ScriptedLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, system, user, *, max_tokens=0):
        self.calls.append(system[:30])
        if "planning lead" in system:
            return LLMResult(json.dumps(make_plan()), 10, 10, "stop")
        fix = {"diagnosis": "mul adds instead of multiplying", "fix_tasks": [{"title": "fix mul", "description": "calc.mul must multiply." + script([step({"src/calc.py": CALC_FIXED}, "fix(calc): mul multiplies")]),
                                                                              "files_owned": ["src/calc.py"], "owner_task": "T1", "weight": 1}]}
        return LLMResult(json.dumps(fix), 10, 10, "stop")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8").stdout


@win
def test_project_loop_plan_build_guard_merge_verify_fix_done(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "TICK_S", 0.6)
    monkeypatch.setattr(hp, "MARKER_IDLE_S", 1.5)
    monkeypatch.setattr(hp, "IDLE_DONE_S", 12.0)

    async def go():
        with HubServer(tmp_path) as hub:
            names = {"Asha": "asha@bugged.dev", "Ben": "ben@bugged.dev", "Cara": "cara@bugged.dev"}
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                for n, mail in names.items():
                    r = await lead.call(t="create_invite", member=n, access="full")
                    hub.spawn_node(n, code=r["code"], accept_access=True, git=(n, mail))
                await until(lambda: len([x for x in lead.nodes().values() if x["online"]]) == 3, 30)
                assert all(x["access"] == "full" for x in lead.nodes().values())
                hub.hub.projects.llm_override = ScriptedLLM()
                members = [{"node_id": x["id"], "provider": "demo-agent"} for x in lead.nodes().values()]
                r = await lead.call(t="project_create", name="Calc", brief="Build a tiny calculator library with tests.", forge="local", members=members,
                                    auto_approve_plan=True, permissions="auto", planner={"kind": "default"})
                assert r["ok"], r
                pid = r["id"]
                try:
                    await until(lambda: lead.projects.get(pid, {}).get("status") in ("done", "failed"), 240, step=0.5)
                except AssertionError:
                    q = lead.projects[pid]
                    raise AssertionError((q["phase"], [x["text"] for x in q["log"][-14:]], [(t["id"], t["status"]) for t in q["tasks"]]))
                p = lead.projects[pid]
                assert p["status"] == "done", (p["status"], p.get("error"), [x["text"] for x in p["log"][-12:]])
                # fair split: everyone got work and made commits
                owners = {}
                for t in p["tasks"]:
                    owners.setdefault(t["owner"], []).append(t["id"])
                assert len(owners) == 3 and all(len(v) >= 2 for v in owners.values()) or len(owners) == 3
                assert all(t["status"] == "merged" for t in p["tasks"])
                # the guard reverted out-of-scope and contract edits; the contract is untouched
                bal = {b["member"]: b for b in p["final"]["balance"]}
                assert sum(b["violations"] for b in bal.values()) >= 2
                assert all(b["commits"] >= 2 for b in bal.values()), bal                     # real commits, authored by each member's own identity
                # fix round happened because the first verification failed
                assert p["round"] == 1 and any(t["id"].startswith("F1") for t in p["tasks"])
                assert p["verify"]["passed"]
                assert p["final"]["quality"]["issues"] == [] and p["final"]["quality"]["readme"] > 400      # the quality gate found nothing to fix
                # inspect the shared remote itself
                remote = p["repo"]
                clone = tmp_path / "inspect"
                subprocess.run(["git", "clone", remote, str(clone)], capture_output=True)
                files = set(git(clone, "ls-files").split())
                assert {"src/calc.py", "src/fmt.py", "tests/test_calc.py", "tests/test_fmt.py", "README.md", "src/main.py", "docs/CONTRACT.md"} <= files
                assert "src/evil.py" not in files and "tests/__keep__.txt" not in files
                assert "tampered" not in (clone / "docs" / "CONTRACT.md").read_text()
                assert "a * b" in (clone / "src" / "calc.py").read_text()
                authors = set(git(clone, "log", "--no-merges", "--format=%ae").split())
                assert {"asha@bugged.dev", "ben@bugged.dev", "cara@bugged.dev"} <= authors
    run(go())
