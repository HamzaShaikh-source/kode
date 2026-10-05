"""Project engine: turns one project brief into a coordinated, contract-bound team build.

  plan  ->  bootstrap  ->  build  ->  merge  ->  verify  ->  fix  ->  done

  plan       an LLM drafts tasks with DISJOINT file ownership, a frozen interface contract and test commands; code assigns them so
             every teammate gets an equal share of work (weighted), and rejects overlapping ownership.
  bootstrap  the coordinator's laptop pushes the contract + plan + team rules (CLAUDE.md / AGENTS.md) to the base branch.
  build      for each ready task: the owner's laptop clones/branches with its own `gh`/git identity, the provider session starts, and the
             agent is pointed at .kode/TASK.md. The engine watches git (not just the screen): commits, push, files touched.
  merge      out-of-scope or contract edits are reverted by code; a PR is opened from the owner's laptop and merged (merge commit, so
             individual commits and authorship survive) in dependency order.
  verify     the coordinator pulls the base branch and runs the test commands. Failures go to an LLM triage that produces fix tasks
             for the owners of the failing areas, up to a fixed number of rounds.

Where the guarantees live: ownership/contract checks, scope reverts, merge order and the commit-balance report are CODE; the LLM only
proposes the plan and triages failures. The lead has total access (standing policy), so nothing here waits for per-action approval,
except genuinely dangerous terminal prompts, which become cards.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import re
import secrets
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from hub import ideate, judge as jd
from hub.protocol import ROLE_LEAD, clip, now_ms
from llm import LLMOutputError, extract_json

MAX_FIX_ROUNDS = 3
MAX_POLISH_ROUNDS = 2
MAX_REUSE = 4                                     # one agent session per teammate handles up to this many tasks before a fresh one replaces it (context stays small)
MAX_TASKS = 40
TICK_S = 3.0
IDLE_DONE_S = 40.0            # agent quiet this long after pushing >= min commits => task finished (marker shortens it)
MARKER_IDLE_S = 6.0
STALL_S = 480.0
# Resilience: how long a transient problem (laptop offline, GitHub unreachable, Wi-Fi blip) is retried before a human is asked.
NET_GRACE_S = 1200.0        # keep retrying for 20 minutes
NET_BACKOFF = (3, 6, 12, 25, 45, 60)
REATTACH_S = 20.0           # after a laptop reconnects, its sessions are re-announced within this window
TRANSIENT = re.compile(r"offline|unreachable|timed out|timeout|unable to access|could not resolve|connection (reset|refused|closed|aborted)|network|early eof|rpc failed|"
                       r"tls|ssl|remote end hung up|fetch origin|failed to connect|temporary failure|no laptop online|try again|rate limit|502|503|504", re.I)
NUDGE_IDLE_S = 45.0          # a slow model thinks for a while: only remind an agent that has been silent AND commit-less for a few minutes
NUDGE_QUIET_S = 180.0
PROMPT_COOLDOWN_S = 5.0
DANGEROUS = re.compile(r"rm\s*-rf|--force|push\s*-f|\bsudo\b|curl[^\n|]*\|\s*(ba)?sh|format\s*[a-z]:|del\s*/[sq]|drop\s*(table|database)|chmod\s*-R\s*777|\.ssh|credential|password", re.I)
# Start-up dialogs that block a TUI until someone presses Enter (folder trust, theme picker, "press Enter to continue"). Never matched against destructive text.
STARTUP = re.compile(r"trust\s*(this|the)\s*(folder|directory|workspace)|Yes,\s*I\s*trust|Do\s*you\s*trust|Choose\s*your\s*color\s*scheme|Choose\s*(a|the)?\s*(text\s*style|theme)|Press\s*Enter\s*to\s*continue|Enter\s*to\s*confirm|\(Y/n\)|\[Y/n\]", re.I)
# Terms, privacy/telemetry choices and logins are the USER's decisions: the engine never answers them, it asks the lead (a card) and waits.
CONSENT = re.compile(r"Terms\s*of\s*Service|Privacy\s*Policy|I\s*agree|I\s*accept|collect\s*and\s*use|improve\s*.{0,40}by\s*allowing|opt[-\s]*out|data\s*collection|telemetry|please\s*(sign|log)[-\s]*in|(sign|log)[-\s]*in\s*(to|with|required)|authenticat(e|ion\s*required)|paste\s*.{0,20}(code|key)|open\s*(the\s*)?browser", re.I)
# Tool-permission questions from the agent ("Do you want to proceed? 1. Yes ..."): the first option is the affirmative one in every supported TUI.
PERMISSION = re.compile(r"Do\s*you\s*want\s*to\s*(proceed|allow|make\s*this\s*edit|create|run)|Allow\s*(once|this|command|creation|edit|write|the)|Yes,\s*allow|Yes,\s*and\s*don.t\s*ask\s*again|\(y/n\)|Approve\s*(this|the)|Run\s*this\s*command\?", re.I)
# An agent offering to update itself must be told to SKIP: its default option is usually "Update now", which would install software on the teammate's machine.
UPDATE = re.compile(r"Update\s*available|Update\s*now|new\s*version\s*(is\s*)?available|upgrade\s*to", re.I)
# Review screens that wait for a human decision on a file change (Antigravity's diff viewer). Keys typed into them are interpreted as commands, so never nudge into one.
DIFFVIEW = re.compile(r"next/prev\s*diff|cycle\s*file|delete\s*comment", re.I)
DONE_MARK = re.compile(r"KODE_DONE \d+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]")


class PlanError(ValueError):
    pass


def slug(s: str, n: int = 28) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "x"


def _base(g: str) -> str:
    g = g.strip().replace("\\", "/").lstrip("./")
    return re.sub(r"(/?\*+.*)$", "", g).rstrip("/")


def globs_overlap(a: str, b: str) -> bool:
    """Conservative: two ownership patterns overlap if one's static prefix contains the other's, or either matches the other."""
    ba, bb = _base(a), _base(b)
    if ba == bb or ba.startswith(bb + "/") or bb.startswith(ba + "/") or not ba or not bb:
        return True
    return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def path_in_scope(path: str, globs: List[str]) -> bool:
    path = path.replace("\\", "/")
    for g in globs:
        g = g.replace("\\", "/").lstrip("./")
        b = _base(g)
        if path == g or fnmatch.fnmatch(path, g) or (b and (path == b or path.startswith(b + "/")) and ("*" in g or g.endswith("/"))):
            return True
        if "*" not in g and not g.endswith("/") and path.startswith(g.rstrip("/") + "/"):
            return True
    return False


def _safe_rel(p: str) -> bool:
    q = PurePosixPath(p.replace("\\", "/"))
    return bool(p) and not q.is_absolute() and ".." not in q.parts and ".git" not in q.parts and ".kode" not in q.parts and not re.match(r"^[A-Za-z]:", p)


def normalize_plan(obj: dict, n_members: int) -> dict:
    """Validate an LLM plan. Raises PlanError with a message the model can act on."""
    if not isinstance(obj, dict):
        raise PlanError("the plan must be a JSON object")
    tasks = obj.get("tasks")
    if not isinstance(tasks, list) or not (1 <= len(tasks) <= MAX_TASKS):
        raise PlanError(f"'tasks' must be a list of 1..{MAX_TASKS} tasks")
    out, ids = [], set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise PlanError(f"task {i} is not an object")
        tid = str(t.get("id") or f"T{i+1}").strip()
        if tid in ids or not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", tid):
            raise PlanError(f"task id '{tid}' is duplicated or malformed")
        ids.add(tid)
        files = [str(f).strip() for f in (t.get("files_owned") or []) if str(f).strip()]
        if not files or any(not _safe_rel(f.replace("*", "x")) for f in files):
            raise PlanError(f"task {tid}: 'files_owned' needs relative paths/globs (no '..', no absolute paths, nothing under .git or .kode)")
        title, desc = clip(str(t.get("title", "")).strip(), 120), clip(str(t.get("description", "")).strip(), 3000)
        if not title or not desc:
            raise PlanError(f"task {tid}: needs a title and a description")
        weight = max(1, min(3, int(t.get("weight") or 1)))
        out.append({"id": tid, "title": title, "description": desc, "acceptance": clip(str(t.get("acceptance", "")).strip(), 1200),
                    "files_owned": files, "depends_on": [str(d) for d in (t.get("depends_on") or [])], "weight": weight})
    for t in out:
        for d in t["depends_on"]:
            if d not in ids or d == t["id"]:
                raise PlanError(f"task {t['id']} depends on unknown task '{d}'")
    # cycles
    state: Dict[str, int] = {}

    def visit(x: str) -> None:
        if state.get(x) == 1:
            raise PlanError("dependency cycle involving " + x)
        if state.get(x) == 2:
            return
        state[x] = 1
        for d in next(t for t in out if t["id"] == x)["depends_on"]:
            visit(d)
        state[x] = 2
    for t in out:
        visit(t["id"])
    # disjoint ownership
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            for ga in a["files_owned"]:
                for gb in b["files_owned"]:
                    if globs_overlap(ga, gb):
                        raise PlanError(f"tasks {a['id']} and {b['id']} both claim '{ga}' / '{gb}': ownership must be disjoint")
    cf = obj.get("contract_files") or {}
    if not isinstance(cf, dict) or not cf or len(cf) > 12:
        raise PlanError("'contract_files' must be an object of 1..12 files (path -> content) describing the shared interfaces")
    contract = {}
    for p, c in cf.items():
        if not _safe_rel(str(p)) or not isinstance(c, str) or not c.strip() or len(c) > 24000:
            raise PlanError(f"contract file '{p}' is invalid")
        contract[str(p).replace("\\", "/")] = c
    for t in out:
        for g in t["files_owned"]:
            for cp in contract:
                if globs_overlap(g, cp):
                    raise PlanError(f"task {t['id']} claims '{g}' which overlaps contract file '{cp}': contract files belong to nobody")
    cmds = [str(c).strip() for c in (obj.get("test_cmds") or []) if str(c).strip()][:5]
    return {"summary": clip(str(obj.get("summary", "")).strip(), 1500), "stack": clip(str(obj.get("stack", "")).strip(), 400),
            "tasks": out, "contract_files": contract, "test_cmds": cmds, "setup_cmds": [str(c).strip() for c in (obj.get("setup_cmds") or []) if str(c).strip()][:4]}


def assign_tasks(tasks: List[dict], member_ids: List[str]) -> None:
    """Weighted longest-processing-time assignment: everybody owns real work (not an equal commit count). Stable for equal weights."""
    load = {m: 0 for m in member_ids}
    cnt = {m: 0 for m in member_ids}
    for t in sorted(tasks, key=lambda x: (-x["weight"], x["id"])):
        pick = min(member_ids, key=lambda m: (load[m], cnt[m], member_ids.index(m)))
        t["owner"] = pick
        load[pick] += t["weight"]
        cnt[pick] += 1
    for t in tasks:
        t["min_commits"] = 1                                   # a simple task is ONE good commit; bigger work commits as often as it needs to


PLAN_SYSTEM = """You are the planning lead of a hackathon team. Several developers, each with their own AI coding agent on their own laptop, will build ONE
project in parallel from one git repository. Produce a build plan as ONE JSON object, nothing else.

{
 "summary": "2-4 sentences: what we are building and how the pieces fit",
 "stack": "languages/frameworks and how to run it",
 "contract_files": {"<path>": "<full file content>"},   // the frozen INTERFACE between everyone's parts: API routes + JSON shapes, function signatures, file formats,
                                                        // env vars, ports. Concrete and complete (e.g. docs/CONTRACT.md and/or openapi.yaml, types). Nobody may edit these later.
 "setup_cmds": ["<shell command to install dependencies, if any>"],
 "test_cmds": ["<shell command(s) that exit 0 only when the whole project works>"],
 "tasks": [{"id": "T1", "title": "...", "description": "what to build, precisely, referring to the contract", "acceptance": "observable checks",
            "files_owned": ["api/**"], "depends_on": [], "weight": 1}]
}

RULES
- At least {min_tasks} tasks and at most {max_tasks}. Each task is 10-25 minutes of work for one AI agent. weight is 1 (small), 2 or 3 (large).
- files_owned: relative paths/globs. They must be DISJOINT across all tasks (two tasks never edit the same file) and must not overlap contract_files.
  Include tests, docs and the README as their own owned files so work stays balanced.
- depends_on: only when a task truly needs another task's CODE merged first. Prefer contract-first design so most tasks start immediately in parallel.
- Keep contract files compact (about 60 lines in total) and task descriptions to 2-4 sentences each: the whole reply must stay short.
- The project must be small enough to finish in the available time, run locally without paid services, and be verifiable by test_cmds (use only tools
  that are commonly installed: python, node/npm). The tests must really exercise the code, not just print OK.
- Every one of the {n} teammates must own SUBSTANTIAL, meaningful work (a real feature or module, not busywork). Shares need not be identical.
- The finished repository will be judged like a professional project, by engineers and by AI reviewers. Plan for that: one task owns a complete README.md
  (what it is, features, prerequisites, setup, how to run, how to test, project structure), another owns real tests, the code is clean and consistent,
  nothing is a placeholder, and the app runs from a fresh clone with the documented commands.
Reply with ONLY the JSON object."""

REVIEW_SYSTEM = """You are a strict senior reviewer evaluating a FINISHED hackathon project the way an experienced engineer or an AI judge would.
You are given the brief, what each task had to deliver, a hygiene audit, the README, the tail of the passing test run and sample source files.
Check: (1) does it satisfy the brief and every acceptance criterion; (2) README quality (purpose, setup, run, test, structure); (3) are tests meaningful;
(4) code quality: structure, naming, dead code, duplication, error handling, no placeholders or TODOs; (5) repository hygiene; (6) would it run from a clean clone with the documented commands.
Reply with ONLY one JSON object:
{"verdict": "pass" | "fix", "issues": [{"title": "...", "description": "specific and actionable, naming the file", "files": ["paths a fix may edit"], "severity": "blocker" | "major" | "minor"}]}
Only report problems the evidence shows; never invent. At most 5 issues; blockers and majors are things that would lose points in an evaluation. Never put contract files in "files"."""

TRIAGE_SYSTEM = """You are the tech lead. The integrated project failed its verification commands. Decide which fix tasks are needed.
Reply with ONE JSON object only:
{"diagnosis": "1-3 sentences", "fix_tasks": [{"title": "...", "description": "what exactly to change and why, quoting the failing output", "files_owned": ["path or glob the fix may edit"],
  "owner_task": "<id of the original task whose area contains the bug>", "weight": 1}]}
Rules: 1-4 fix tasks, files_owned disjoint between fix tasks, never include contract files, choose the smallest edit that makes the commands pass."""


def render_rules(p: dict) -> str:
    return f"""# Project conventions for {p['name']}

Several developers are building this project in parallel, each with their own assistant on their own laptop, from one repository.
These are the working conventions for everyone, including you.

1. Your own assignment is in `.kode/TASK.md` in your working folder. Read it and the contract files it lists before writing code.
   The contract files define the shared interfaces and are frozen: do not edit them.
2. Only create or edit the files listed as yours in TASK.md. Teammates own everything else; edits elsewhere are reverted automatically.
3. Commit sensibly: a small task is one clear commit; bigger work commits at natural steps, as many as it needs. Messages like `feat(api): add GET /ideas`. No padding commits, no giant mixed ones.
4. Work on your task branch only, never on `{p['base']}`. Push it with `git push -u origin <your-branch>` after your first commit and again at the end.
5. Work on your own: the team lead is not available for questions during the task. If something is unclear, follow the contract and pick the simplest sensible option.
6. When everything is committed and pushed, print one line `KODE_DONE <number of commits>` so the lead's dashboard can see that you finished.
7. If your agent has skills or plugins installed (testing, code review, security review, frontend design, debugging), use the ones that fit your task: TASK.md lists the relevant ones. Prefer them to improvising.
8. You are one long-lived session that may be given several tasks one after another: finish each task completely, then wait for the next one; never start other sessions or sub-agents unless the task needs them.
"""


ALWAYS_SKILLS = ("test-driven-development", "tdd-workflow", "systematic-debugging", "code-review", "requesting-code-review", "security-review", "simplify")
_WORD = re.compile(r"[a-z][a-z0-9]{3,}")


def pick_skills(skills: List[dict], text: str, limit: int = 8) -> List[dict]:
    """The installed skills most relevant to this task: keyword overlap with the task text, plus the general quality skills when they exist."""
    words = set(_WORD.findall(text.lower()))
    scored = []
    for s in skills or []:
        hay = set(_WORD.findall((s["name"].replace("-", " ") + " " + s.get("description", "")).lower()))
        score = len(words & hay) + (3 if s["name"] in ALWAYS_SKILLS else 0) + (2 if any(w in words for w in _WORD.findall(s["name"].replace("-", " "))) else 0)
        if score >= 3:
            scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [s for _, s in scored[:limit]]


def render_task(p: dict, t: dict, branch: str, contract_paths: List[str], skills: Optional[List[dict]] = None) -> str:
    deps = ", ".join(t["depends_on"]) or "none"
    use = pick_skills(skills or [], " ".join([t["title"], t["description"], " ".join(t["files_owned"])]))
    skill_block = ("## Skills installed on this laptop (use the ones that fit; invoke them instead of improvising)" + chr(10) + chr(10).join(f"- {s['name']}: {s['description']}" for s in use) + chr(10) * 2) if use else ""
    return f"""# Task {t['id']}: {t['title']}

Project: {p['name']}
Brief: {p['brief']}

## What to build
{t['description']}

## Acceptance
{t.get('acceptance') or 'It does what the description says and works with the contract.'}

## Your scope (the ONLY files you may create or edit)
{chr(10).join('- ' + g for g in t['files_owned'])}

## Frozen contract (read, never edit)
{chr(10).join('- ' + c for c in contract_paths)}

{skill_block}## Git
- Branch: `{branch}` (already checked out). Base branch: `{p['base']}`. Dependencies already merged: {deps}.
- Commit at sensible steps: one good commit for a small task, more when the work has natural stages. Push with `git push -u origin {branch}`.
- Quality bar: this repository will be reviewed by engineers and AI reviewers. Clean code, real error handling, no placeholders or TODOs, tests that mean something.
- When everything is committed and pushed, print exactly one line: `KODE_DONE <number of commits>` and stop.
{('## Extra instructions' + chr(10) + t['extra']) if t.get('extra') else ''}
"""


SUP_SYSTEM = """You supervise AI coding agents that run in terminals on a team's laptops. You are shown the latest text of ONE agent's terminal.
Decide how to unblock it, if it needs unblocking. Reply with ONLY one JSON object:
{"state": "working" | "waiting_for_input" | "dialog" | "finished", "action": "wait" | "keys" | "ask_human", "keys": ["enter"], "reason": "<one short sentence>"}

Rules
- If the agent is working, thinking, or running a command (spinners, "Working", "Thinking", streaming output): action "wait".
- If a dialog asks permission for something routine inside the project folder (accept a file edit or file creation, run a git/python/node/test command, trust this folder,
  continue, theme or layout choice): choose the AFFIRMATIVE option. Numbered menu: send the number, then "enter" (e.g. ["1","enter"]). Yes/No prompt: ["y","enter"].
  A highlighted default that is already the affirmative option: ["enter"].
- Decline update or upgrade offers (choose "Skip"/"No"); never install or update software.
- Never approve anything destructive or sensitive: deleting files outside the project, force pushes, sudo, reading credentials or ssh keys, sending data elsewhere,
  or any terms of service, privacy, telemetry, data-sharing, account login or payment screen. For those answer action "ask_human".
- If the screen shows the agent idle at its normal input prompt with nothing to answer: action "wait".
- If you cannot tell what is being asked, answer "ask_human". Never invent text to type: keys may only be "enter", "esc", "tab", "space", "up", "down", "y", "n", or a single digit 1-9."""
KEYMAP = {"enter": chr(13), "esc": chr(27), "tab": chr(9), "space": " ", "up": chr(27) + "[A", "down": chr(27) + "[B", "y": "y", "n": "n", **{str(i): str(i) for i in range(1, 10)}}


class CliClient:
    """Planner backed by a teammate's own logged-in CLI, run on that laptop through the node (see node/ops.py op_cli_complete)."""

    def __init__(self, engine: "ProjectEngine", provider: str, node_id: str):
        self.engine, self.provider, self.node_id = engine, provider, node_id

    async def complete(self, system: str, user: str, *, max_tokens: int = 0):
        from llm import LLMResult
        r = await self.engine.hub.node_op(self.node_id, "cli_complete", 900.0, tool=self.provider, cmd_timeout=840,
                                          prompt=system + chr(10) * 2 + user + chr(10) * 2 + "Reply with the JSON object only, no commentary and no code fences.")
        if not r.get("ok"):
            raise RuntimeError(f"planner ({self.provider} CLI): {r.get('err', 'failed')}")
        return LLMResult(r["text"], 0, 0, "stop")


class ProjectEngine:
    def __init__(self, hub):
        self.hub = hub
        self.items: Dict[str, dict] = {}
        self.runners: Dict[str, asyncio.Task] = {}
        self.llm_override = None
        self.locks: Dict[str, asyncio.Lock] = {}
        self.sup_cache: Dict[str, Dict[str, dict]] = {}       # project id -> normalised dialog -> the supervisor's earlier answer (no model call for repeats)
        self.sup_calls: Dict[str, int] = {}
        self.sup_down: Dict[str, float] = {}

    # ───────────── persistence / state out ─────────────
    def load(self) -> None:
        for d in self.hub.store.load_projects():
            if d.get("status") == "running":
                d["status"], d["auto_resume"] = "paused", True
                d["note"] = "The Brain restarted. Resuming automatically as the laptops reconnect."
            elif d.get("status") == "planning":
                d["status"] = "failed"
                d["error"] = "The Brain restarted while the plan was being written. Choose a planner and plan again."
            self.items[d["id"]] = d

    # ───────────── resilience ─────────────
    def node_name(self, nid: str) -> str:
        n = self.hub.nodes.get(nid)
        return (n.member_name or n.name) if n else nid

    def node_ok(self, nid: str) -> bool:
        n = self.hub.nodes.get(nid)
        return bool(n and n.online)

    def node_gone(self, nid: str) -> None:
        for p in self.items.values():
            if p["status"] == "running" and any(m["node_id"] == nid for m in p["members"]):
                self.log(p, "net", f"{self.node_name(nid)}'s laptop went offline. Their tasks wait and continue when it is back; nothing is lost.")

    def node_back(self, nid: str) -> None:
        for p in self.items.values():
            if p["status"] in ("running", "paused") and any(m["node_id"] == nid for m in p["members"]):
                self.log(p, "net", f"{self.node_name(nid)}'s laptop is back online.")
                for t in p["tasks"]:
                    if t["owner"] == nid:
                        t["retry_at"], t["net_fail"], t["wait"] = 0, 0, ""

    async def auto_resume(self) -> None:
        """After a Brain restart: wait briefly for the laptops to reconnect, then continue every project that was running."""
        await asyncio.sleep(1.0)
        todo = [p for p in self.items.values() if p.get("auto_resume") and p["status"] == "paused"]
        if not todo:
            return
        deadline = time.time() + 45.0
        while time.time() < deadline and not all(self.node_ok(m["node_id"]) for p in todo for m in p["members"]):
            await asyncio.sleep(1.0)
        for p in todo:
            if p["status"] == "paused" and p.get("auto_resume"):                  # not cancelled or resumed by hand meanwhile
                p["auto_resume"] = False
                self.log(p, "net", "Brain is back: resuming the project" + ("" if all(self.node_ok(m["node_id"]) for m in p["members"]) else " (some laptops are still offline; their tasks will wait)"))
                self.start(p)

    def net_wait(self, p: dict, t: dict, why: str) -> None:
        """Record a transient problem: back off and retry. Only after NET_GRACE_S of continuous trouble is a human asked."""
        now = time.time()
        t["net_since"] = t.get("net_since") or now
        t["net_fail"] = t.get("net_fail", 0) + 1
        delay = NET_BACKOFF[min(t["net_fail"] - 1, len(NET_BACKOFF) - 1)]
        t["retry_at"] = now + delay
        t["wait"] = clip(why, 120)
        if t["net_fail"] in (1, 5, 20):
            self.log(p, "net", f"{t['id']}: {clip(why, 160)}. Retrying automatically (next in {delay}s).", t["id"])
        if now - t["net_since"] > NET_GRACE_S and not any(c.get("task") == t["id"] and c["kind"] == "blocked" for c in p.get("cards", [])):
            t["status"] = "blocked"
            self.card(p, "blocked", f"Task {t['id']} ({t['title']}) has been unable to reach {self.node_name(t['owner'])}'s laptop or GitHub for {int((now - t['net_since']) / 60)} minutes: {clip(why, 200)}. Press Retry when the network is back.", task=t["id"])

    def net_ok(self, t: dict) -> None:
        if t.get("net_since") or t.get("net_fail"):
            t["net_since"], t["net_fail"], t["retry_at"], t["wait"] = 0, 0, 0, ""

    async def ensure_coordinator(self, p: dict) -> None:
        """The coordinator laptop merges PRs and runs the tests. If it is offline, hand the role to another online teammate."""
        cn = p.get("coordinator")
        if self.node_ok(cn):
            return
        for _ in range(8):                                                        # a short blip is not worth a handover
            await asyncio.sleep(2.0)
            if self.node_ok(cn):
                return
        cand = [m for m in p["members"] if self.node_ok(m["node_id"]) and (p["forge"] == "local" or m.get("gh_user"))]
        if not cand:
            raise RuntimeError("no laptop online to coordinate merges")
        new = cand[0]
        self.log(p, "net", f"{self.node_name(cn)}'s laptop is offline: {new['name']}'s laptop now merges and verifies.")
        await self.must(p, new["node_id"], "git_prepare", 180, dir=self.cdir(p), repo=p["repo"], base=p["base"], forge=p["forge"])
        p["coordinator"] = new["node_id"]
        self.save(p)

    def save(self, p: dict) -> None:
        self.hub.store.save_project(p["id"], p)
        self.hub.push_projects()

    def public(self, p: dict) -> dict:
        q = {k: v for k, v in p.items() if k not in ("log", "plan", "cards")}
        plan = p.get("plan") or {}
        q["plan"] = {"summary": plan.get("summary", ""), "stack": plan.get("stack", ""), "test_cmds": plan.get("test_cmds", []), "setup_cmds": plan.get("setup_cmds", []),
                     "contract_files": sorted((plan.get("contract_files") or {}).keys()), "contract_preview": {k: clip(v, 1800) for k, v in list((plan.get("contract_files") or {}).items())[:4]}}
        q["log"] = p.get("log", [])[-120:]
        q["cards"] = p.get("cards", [])
        return q

    def public_all(self) -> dict:
        return {"projects": [self.public(p) for p in sorted(self.items.values(), key=lambda x: -x.get("created", 0))]}

    def log(self, p: dict, kind: str, text: str, task: str = "") -> None:
        p.setdefault("log", []).append({"ts": now_ms(), "kind": kind, "text": clip(text, 400), "task": task})
        if len(p["log"]) > 500:
            del p["log"][:100]
        self.hub.event("project", f"[{p['name']}] {clip(text, 160)}", "", "", "orchestrator", audit=kind in ("phase", "merge", "done", "error"))
        self.save(p)

    def card(self, p: dict, kind: str, text: str, **extra: Any) -> str:
        cid = "c_" + secrets.token_hex(3)
        p.setdefault("cards", []).append({"id": cid, "kind": kind, "text": clip(text, 900), "ts": now_ms(), **extra})
        self.save(p)
        return cid

    def drop_card(self, p: dict, cid: str) -> None:
        p["cards"] = [c for c in p.get("cards", []) if c["id"] != cid]
        self.save(p)

    def planner_label(self, p: dict) -> str:
        pl = p.get("planner") or {}
        if pl.get("kind") == "cli":
            return f"{pl['provider']} CLI (your own login)"
        if pl.get("kind") == "profile":
            return f"API profile {pl.get('profile_id')} / {pl.get('model') or 'default model'}"
        return "Brain default model"

    def client(self, p: Optional[dict] = None):
        """The model that writes the plan and diagnoses failures, exactly as the lead chose it for this project:
        a teammate's own CLI login (Claude Code / Codex), one of the saved API profiles, or the Brain's default. Plans are long, so a slow model
        gets far more time than a chat turn."""
        if self.llm_override is not None:
            return self.llm_override
        from hub.orchestrator import ProfileClient
        from llm import RetryingLLM
        pl = (p or {}).get("planner") or {}
        if pl.get("kind") == "cli":
            return CliClient(self, pl["provider"], pl.get("node_id", ""))
        if pl.get("kind") == "profile":
            prof = self.hub.store.get_profile(pl.get("profile_id", ""), with_key=True)
            if not prof:
                raise RuntimeError("the planner's API profile no longer exists; pick another planner and re-plan")
            return RetryingLLM(ProfileClient(prof, pl.get("model") or prof.get("default_model", ""), timeout=600), max_tries=2, base_delay_s=2.0, deadline_s=1300)
        o = self.hub.orch
        inner = getattr(o.default_llm, "inner", None)
        if o.llm_override is None and inner is not None:
            return RetryingLLM(inner, max_tries=2, base_delay_s=2.0, deadline_s=900)
        return o._client()

    def member_client(self, p: dict, m: dict):
        """A teammate's own model for one-shot questions (ideas, votes, judging): their API profile, or their Claude Code / Codex login, else the planner."""
        if self.llm_override is not None:
            return self.llm_override
        if m.get("profile_id"):
            from hub.orchestrator import ProfileClient
            prof = self.hub.store.get_profile(m["profile_id"], with_key=True)
            if prof:
                return ProfileClient(prof, m.get("model") or prof.get("default_model", ""), timeout=600)
        if m["provider"] in ("claude", "codex"):
            return CliClient(self, m["provider"], m["node_id"])
        return self.client(p)

    # ───────────── viewer commands ─────────────
    async def on_viewer(self, v, t: str, m: dict) -> None:
        ref = m.get("ref")
        res = lambda **kw: v.send(t="result", ref=ref, **kw)
        pid = str(m.get("id", ""))
        p = self.items.get(pid)
        try:
            if t == "project_create":
                p = self.create(m)
                await res(ok=True, id=p["id"])
            elif t == "project_approve" and p and p["status"] == "plan_ready":
                self.start(p)
                await res(ok=True)
            elif t == "project_replan" and p and p["status"] in ("plan_ready", "failed", "draft"):
                p["feedback"] = clip(str(m.get("feedback", "")), 1500)
                if m.get("planner"):
                    p["planner"] = self.parse_planner(m["planner"], p["members"])
                self.start_planning(p)
                await res(ok=True)
            elif t == "project_planner" and p:
                p["planner"] = self.parse_planner(m.get("planner"), p["members"])
                self.save(p)
                await res(ok=True)
            elif t == "project_assign" and p and p["status"] in ("plan_ready", "paused", "running"):
                tk = next((x for x in p["tasks"] if x["id"] == m.get("task")), None)
                if tk and tk["status"] in ("pending", "ready") and any(x["node_id"] == m.get("node_id") for x in p["members"]):
                    tk["owner"] = m["node_id"]
                    self.save(p)
                    await res(ok=True)
                else:
                    await res(ok=False, err="cannot reassign that task now")
            elif t == "project_pause" and p and p["status"] == "running":
                p["status"], p["note"] = "paused", "Paused by the lead."
                self.save(p)
                await res(ok=True)
            elif t == "project_resume" and p and (p["status"] == "paused" or (p["status"] == "failed" and p["phase"] != "plan" and p.get("plan"))):
                self.start(p)
                await res(ok=True)
            elif t == "project_cancel" and p and p["status"] in ("running", "paused", "planning", "plan_ready"):
                await self.cancel(p)
                await res(ok=True)
            elif t == "project_delete" and p and p["status"] not in ("running", "planning"):
                self.items.pop(pid, None)
                self.hub.store.delete_project(pid)
                self.hub.push_projects()
                await res(ok=True)
            elif t == "project_judge" and p and p["status"] in ("done", "failed", "paused") and p.get("repo") and p.get("coordinator"):
                self._spawn(p, self.rejudge(p))
                await res(ok=True)
            elif t == "project_card" and p:
                c = next((x for x in p.get("cards", []) if x["id"] == m.get("card")), None)
                if c:
                    await self.answer_card(p, c, str(m.get("action", "dismiss")))
                await res(ok=True)
            elif t == "project_nudge" and p:
                tk = next((x for x in p["tasks"] if x["id"] == m.get("task")), None)
                s = self.hub.sessions.get(tk.get("sid", "")) if tk else None
                if tk and s and s.state == "live":
                    await self.type_line(s, clip(re.sub(r"[\x00-\x1f]", " ", str(m.get("text", ""))), 600))
                    self.log(p, "nudge", f"lead sent an instruction to the agent on {tk['id']}", tk["id"])
                    await res(ok=True)
                else:
                    await res(ok=False, err="that task has no running session")
            else:
                await res(ok=False, err="not possible in the project's current state")
        except Exception as e:                                           # a bad request must never kill the viewer socket
            await res(ok=False, err=f"{type(e).__name__}: {e}")

    # ───────────── lifecycle ─────────────
    def create(self, m: dict) -> dict:
        name = clip(str(m.get("name", "")).strip(), 60)
        brief = clip(str(m.get("brief", "")).strip(), 4000)
        if not name or len(brief) < 10:
            raise ValueError("a name and a brief (what to build) are required")
        members = []
        want = m.get("members") or []
        online = sorted((n for n in self.hub.nodes.values() if n.online), key=lambda n: n.hostname.lower() != self.hub.hostname.lower())      # the lead's own PC first
        sel = {str(x.get("node_id")): x for x in want if isinstance(x, dict)}
        for n in online:
            if sel and n.id not in sel:
                continue
            cfg = sel.get(n.id, {})
            prov = str(cfg.get("provider") or self.default_provider(n))
            if not any(pr.get("id") == prov and pr.get("available") for pr in n.providers):
                raise ValueError(f"{n.name}: provider '{prov}' is not available there")
            members.append({"node_id": n.id, "name": n.member_name or n.name, "provider": prov, "profile_id": str(cfg.get("profile_id", "")), "model": str(cfg.get("model", ""))})
        if not members:
            raise ValueError("no online teammate selected")
        pid = "p_" + secrets.token_hex(3)
        forge = "local" if m.get("forge") == "local" else "github"
        rname = str(m.get("repo_name", "")).strip()
        if rname and not re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/)?[A-Za-z0-9._-]{1,100}", rname):
            raise ValueError("repository name may only contain letters, numbers, '.', '-' and '_' (optionally owner/name)")
        p = {"id": pid, "name": name, "brief": brief, "repo": str(m.get("repo", "")).strip(), "forge": forge, "base": "main", "created": time.time(),
             "status": "draft", "phase": "plan", "members": members, "tasks": [], "plan": None, "log": [], "cards": [], "round": 0,
             "auto_approve_plan": bool(m.get("auto_approve_plan")), "permissions": m.get("permissions") if m.get("permissions") in ("ask", "auto", "turbo") else "turbo",
             "user_test_cmds": [str(c) for c in (m.get("test_cmds") or []) if str(c).strip()][:5],
             "planner": self.parse_planner(m.get("planner"), members), "slug": f"{slug(name, 18)}-{pid[2:]}", "feedback": "",
             "repo_name": rname or slug(name, 40), "visibility": "public" if m.get("visibility") == "public" else "private", "polish_round": 0,
             "hackathon": bool(m.get("hackathon")), "judge_round": 0}
        self.items[pid] = p
        self.save(p)
        self.start_planning(p)
        return p

    def parse_planner(self, spec: Any, members: List[dict]) -> dict:
        """{"kind": "cli", "provider": "claude"|"codex"} | {"kind": "profile", "profile_id", "model"} | {"kind": "default"}"""
        if not isinstance(spec, dict) or spec.get("kind") not in ("cli", "profile", "default"):
            raise ValueError("choose which model plans the project (the Planner setting)")
        if spec["kind"] == "profile":
            if not self.hub.store.get_profile(str(spec.get("profile_id", ""))):
                raise ValueError("that API profile does not exist")
            return {"kind": "profile", "profile_id": str(spec["profile_id"]), "model": str(spec.get("model", ""))}
        if spec["kind"] == "cli":
            prov = str(spec.get("provider", ""))
            hosts = [n for n in self.hub.nodes.values() if n.online and any(x.get("id") == prov and x.get("available") for x in n.providers)]
            pref = [n for n in hosts if any(m["node_id"] == n.id for m in members)] or hosts
            if prov not in ("claude", "codex") or not pref:
                raise ValueError(f"'{prov}' is not available as a planner on any online laptop")
            return {"kind": "cli", "provider": prov, "node_id": pref[0].id}
        return {"kind": "default"}

    def default_provider(self, n) -> str:
        for pref in ("claude", "agy", "opencode", "codex", "hermes", "demo-agent"):
            if any(pr.get("id") == pref and pr.get("available") for pr in n.providers):
                return pref
        return next((pr["id"] for pr in n.providers if pr.get("available") and pr.get("id") != "shell"), "shell")

    def _spawn(self, p: dict, coro) -> None:
        old = self.runners.get(p["id"])
        if old and not old.done():
            old.cancel()
        t = asyncio.create_task(self._guard(p, coro))
        self.runners[p["id"]] = t

    async def _guard(self, p: dict, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            p["status"], p["error"] = "failed", f"{type(e).__name__}: {e}"
            self.log(p, "error", p["error"])

    def start_planning(self, p: dict) -> None:
        p["status"], p["phase"], p["error"], p["note"] = "planning", "plan", "", "Drafting the plan..."
        self.save(p)
        self._spawn(p, self.plan_flow(p))

    async def plan_flow(self, p: dict) -> None:
        if p.get("hackathon") and not (p.get("ideation") or {}).get("winner"):
            await ideate.run(self, p)
            p["phase"], p["note"] = "plan", "Drafting the plan..."
            self.save(p)
        await self.plan(p)

    def start(self, p: dict) -> None:
        p["status"], p["error"], p["note"] = "running", "", ""
        self.save(p)
        self._spawn(p, self.run(p))

    async def cancel(self, p: dict) -> None:
        t = self.runners.get(p["id"])
        if t and not t.done():
            t.cancel()
        for tk in p["tasks"]:
            if tk.get("sid"):
                await self.hub.stop_session(tk["sid"], "orchestrator")
            if tk["status"] not in ("merged",):
                tk["status"] = "cancelled"
        p["status"], p["note"] = "cancelled", "Cancelled by the lead."
        self.log(p, "phase", "project cancelled")

    # ───────────── planning ─────────────
    async def plan(self, p: dict) -> None:
        n = len(p["members"])
        client = self.client(p)
        roster = "\n".join(f"- {m['name']} (agent: {m['provider']})" for m in p["members"])
        system = PLAN_SYSTEM.replace("{min_tasks}", str(max(n * 2, 3))).replace("{max_tasks}", str(min(MAX_TASKS, n * 6 + 2))).replace("{n}", str(n))
        user = f"PROJECT: {p['name']}\nBRIEF:\n{p['brief']}\n\nTEAM:\n{roster}\n"
        if p.get("user_test_cmds"):
            user += "\nThe lead requires these verification commands: " + "; ".join(p["user_test_cmds"]) + "\n"
        if p.get("feedback"):
            user += f"\nLEAD FEEDBACK ON THE PREVIOUS PLAN (apply it):\n{p['feedback']}\n"
        feedback, plan = "", None
        for attempt in range(3):
            self.log(p, "plan", f"asking the planner: {self.planner_label(p)} (attempt {attempt + 1})")
            res = await client.complete(system, user + feedback, max_tokens=7000)
            try:
                plan = normalize_plan(extract_json(res.text), n)
                break
            except (LLMOutputError, PlanError, ValueError, TypeError) as e:
                feedback = f"\n\nYour previous reply was rejected: {e}. Fix exactly that and reply with ONLY the JSON object."
                self.log(p, "plan", f"plan rejected: {clip(str(e), 200)}")
        if plan is None:
            raise PlanError("the planner could not produce a valid plan after 3 attempts; adjust the brief and re-plan")
        if p.get("user_test_cmds"):
            plan["test_cmds"] = p["user_test_cmds"]
        if not plan["test_cmds"]:
            raise PlanError("the plan has no test_cmds, so nothing could verify the project; re-plan")
        assign_tasks(plan["tasks"], [m["node_id"] for m in p["members"]])
        for t in plan["tasks"]:
            t.update({"status": "pending", "sid": "", "branch": "", "pr_url": "", "commits": 0, "round": 0, "attempts": 0, "violations": 0, "nudges": 0, "log": []})
        p["plan"] = plan
        p["tasks"] = plan["tasks"]
        p["contract_sha"] = hashlib.sha256(json.dumps(plan["contract_files"], sort_keys=True).encode()).hexdigest()
        p["status"], p["phase"], p["note"] = "plan_ready", "plan", "Review the plan, then approve it."
        self.log(p, "phase", f"plan ready: {len(plan['tasks'])} tasks for {n} teammates")
        if p.get("auto_approve_plan"):
            self.start(p)

    # ───────────── helpers ─────────────
    def member(self, p: dict, nid: str) -> dict:
        return next(m for m in p["members"] if m["node_id"] == nid)

    def mdir(self, p: dict, m: dict) -> str:
        return f"kode/{p['slug']}/{slug(m['name'], 14)}-{m['node_id'][2:6]}"

    def cdir(self, p: dict) -> str:
        return f"kode/{p['slug']}/_coord"

    async def op(self, node_id: str, name: str, timeout: float = 120.0, **a: Any) -> dict:
        return await self.hub.node_op(node_id, name, timeout, **a)

    async def must(self, p: dict, node_id: str, name: str, timeout: float = 120.0, **a: Any) -> dict:
        r = await self.op(node_id, name, timeout, **a)
        if not r.get("ok"):
            raise RuntimeError(f"{name} on {self.hub.nodes[node_id].name if node_id in self.hub.nodes else node_id}: {r.get('err', 'failed')}")
        return r

    def fresh(self, s, mark: int, n: int = 1800) -> str:
        """Cleaned terminal text printed AFTER byte offset `mark` (so a dialog that was already answered is not seen again)."""
        new = max(0, s.bytes_out - mark)
        return self.screen(s, n, "".join(s.ring)[-new:] if new else "")

    async def ensure_submitted(self, s, phrase: str) -> None:
        """A TUI that is still loading (MCP servers, update checks) can swallow the Enter that follows a typed prompt, leaving the text sitting
        in its input box. If the agent shows no activity and our text is still on screen, press Enter again (an empty submit is harmless)."""
        for _ in range(4):
            await asyncio.sleep(7.0)
            if s.state != "live":
                return
            if time.time() - s.last_out < 3.0:
                return                                            # output is flowing: the agent took the prompt
            if re.sub(r"\s+", "", phrase).lower() in re.sub(r"\s+", "", self.screen(s, 900)).lower():
                await self.hub.ensure_copilot(s)
                await self.hub.lead_input(self.hub.orch.actor, s, chr(13), label="project")
            else:
                return

    async def supervise(self, p: dict, t: dict, s, tail: str, why: str = "") -> str:
        """Ask the planner-chosen model what (if anything) to press. Returns "acted" | "wait" | "human" | "error".
        Code, not the model, keeps the hard lines: terms/logins and destructive text never reach the model's keys."""
        now = time.time()
        if CONSENT.search(tail[-900:]):
            await self.need_setup(p, t, s, tail)
            return "human"
        if DANGEROUS.search(tail[-700:]):
            self.card(p, "prompt", f"{s.label} is asking something that mentions a destructive or sensitive action, so K.O.D.E. will not answer it:\n{clip(tail[-600:], 600)}", task=t["id"], sid=s.sid)
            return "human"
        sig = re.sub(r"[\u25cf\u25cb\u25d0-\u25d3\u00b7\u2022\u280b-\u28ff\s]+", "", tail)[-240:]
        if sig == t.get("sup_sig") and now - t.get("sup_at", 0) < 90:
            return "wait"                                              # same screen, already decided: do not ask again every tick
        t["sup_sig"], t["sup_at"] = sig, now
        # The same routine question ("run a git command? 1. Yes") comes back many times: remember what was decided for its normalised shape
        # (digits, paths and spacing removed) so only NEW kinds of screens cost a model call.
        norm = re.sub(r"(?:[A-Za-z]:)?[\\/][^\s]*|\d+|[^a-z?]+", "", tail[-600:].lower())[-140:]
        cache = self.sup_cache.setdefault(p["id"], {})
        hit = cache.get(norm) if len(norm) > 25 else None
        if hit:
            await self.hub.ensure_copilot(s)
            for k in hit["keys"]:
                await self.hub.lead_input(self.hub.orch.actor, s, KEYMAP[k], label="project")
                await asyncio.sleep(0.5)
            t["prompt_mark"] = s.bytes_out
            self.log(p, "supervisor", f"{t['id']}: pressed {' '.join(hit['keys'])} on {s.label} (same kind of question as before: {hit['reason']})", t["id"])
            return "acted"
        if now < self.sup_down.get(p["id"], 0):
            return "error"                                         # the supervisor model failed recently: use the fixed rules for a while
        self.sup_calls[p["id"]] = self.sup_calls.get(p["id"], 0) + 1
        try:
            res = await asyncio.wait_for(self.client(p).complete(SUP_SYSTEM, f"AGENT: {s.label}\nTASK: {t['id']} {t['title']}\nCONTEXT: {why or 'screen looks static'}\n\nTERMINAL (latest text, spaces may be missing):\n{tail[-1800:]}", max_tokens=300), 75)
            d = extract_json(res.text)
        except Exception as e:                                         # a failed consult must never freeze the build: fall back to the fixed rules
            self.sup_down[p["id"]] = time.time() + 300
            self.log(p, "supervisor", f"{t['id']}: supervisor model unavailable ({type(e).__name__}: {clip(str(e), 120)}); using fixed rules for 5 minutes", t["id"])
            return "error"
        action, keys = str(d.get("action", "wait")), [str(k).lower() for k in (d.get("keys") or [])][:4]
        reason = clip(str(d.get("reason", "")), 160)
        if action == "ask_human" or (action == "keys" and (not keys or any(k not in KEYMAP for k in keys))):
            if not any(c.get("sid") == s.sid and c["kind"] == "prompt" for c in p.get("cards", [])):
                self.card(p, "prompt", f"The supervisor could not safely answer for {s.label}: {reason}\n\n{clip(tail[-500:], 500)}", task=t["id"], sid=s.sid)
            self.log(p, "supervisor", f"{t['id']}: needs you ({reason})", t["id"])
            return "human"
        if action != "keys":
            return "wait"
        await self.hub.ensure_copilot(s)
        for k in keys:
            await self.hub.lead_input(self.hub.orch.actor, s, KEYMAP[k], label="project")
            await asyncio.sleep(0.5)
        t["prompt_mark"] = s.bytes_out
        t["auto_answers"] = t.get("auto_answers", 0) + 1
        if len(norm) > 25:
            cache[norm] = {"keys": keys, "reason": reason}
        self.log(p, "supervisor", f"{t['id']}: supervisor pressed {' '.join(keys)} on {s.label} ({reason})", t["id"])
        return "acted"

    def retire(self, sid: str, delay: float = 600.0) -> None:
        """A finished task's terminal stays visible for a while (to read what the agent did), then leaves the grid so tiles do not pile up."""
        async def later():
            await asyncio.sleep(delay)
            s = self.hub.sessions.get(sid)
            if s and s.state in ("exited", "error", "offline"):
                self.hub.forget_session(sid)
        asyncio.create_task(later())

    def keys_for(self, tail: str) -> str:
        """Keystrokes that answer a routine dialog: Enter (the affirmative default), except update offers, which are declined."""
        if UPDATE.search(tail):
            m = re.search(r"(\d)\s*[.)]\s*Skip(?!\s*until)", tail, re.I)
            return (m.group(1) + chr(13)) if m else ""          # a bare "update available" banner is not a dialog: send nothing (Esc would clear the input box)
        return chr(13)

    def screen(self, s, n: int = 3000, raw: Optional[str] = None) -> str:
        raw = ANSI.sub("", "".join(s.ring) if raw is None else raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()[-n:]

    async def type_line(self, s, text: str) -> None:
        await self.hub.ensure_copilot(s)
        await self.hub.lead_input(self.hub.orch.actor, s, text, label="project")
        await asyncio.sleep(0.6)
        await self.hub.lead_input(self.hub.orch.actor, s, "\r", label="project")

    # ───────────── run ─────────────
    async def run(self, p: dict) -> None:
        for t in p["tasks"]:
            if t["status"] == "starting":
                s0 = self.hub.sessions.get(t.get("sid", ""))
                t["status"] = "running" if (s0 and s0.state == "live") else "pending"
            if t["status"] == "review":
                t["status"] = "running"                                        # an interrupted merge is simply attempted again
            if t["status"] == "running":
                t["last_progress"] = time.time()
                t["nudge_at"] = time.time()
        p["cards"] = [c for c in p.get("cards", []) if c["kind"] != "stalled"]
        if p["phase"] in ("plan", "bootstrap"):
            await self.net_retry(p, "setup", lambda: self.bootstrap(p))
        while True:
            if p["phase"] == "build":
                await self.build(p)
                if p["status"] != "running":
                    return
                p["phase"] = "verify"
                self.log(p, "phase", "all tasks merged: verifying the integrated project")
            if p["phase"] == "verify":
                ok = await self.net_retry(p, "verification", lambda: self.verify(p))
                if p["status"] != "running":
                    return
                if ok:
                    p["phase"] = "polish"
                    self.log(p, "phase", "verification passed: final quality review")
                elif p["round"] >= MAX_FIX_ROUNDS:
                    p["status"], p["phase"] = "failed", "done"
                    p["error"] = f"verification still fails after {MAX_FIX_ROUNDS} fix rounds"
                    self.log(p, "error", p["error"])
                    await self.finish(p, failed=True)
                    return
                else:
                    await self.net_retry(p, "triage", lambda: self.triage(p))
                    p["phase"] = "build"
            if p["phase"] == "polish":
                clean = await self.net_retry(p, "quality review", lambda: self.quality_review(p))
                if p["status"] != "running":
                    return
                if not clean:
                    p["phase"] = "build"
                    continue
                p["phase"] = "judge"
            if p["phase"] == "judge":
                done = await self.net_retry(p, "code judge", lambda: self.judge_review(p))
                if p["status"] != "running":
                    return
                if not done:
                    p["phase"] = "build"
                    continue
                await self.net_retry(p, "submission write-up", lambda: self.submission_pack(p))
                await self.net_retry(p, "final report", lambda: self.finish(p))
                return

    async def check_agent(self, p: dict, m: dict) -> None:
        """Fail early, with the fix spelled out, if a teammate's chosen agent is missing or not signed in (instead of a task that silently never starts)."""
        n = self.hub.nodes.get(m["node_id"])
        pr = next((x for x in (n.providers if n else []) if x.get("id") == m["provider"]), None)
        label = (pr or {}).get("label", m["provider"])
        if not pr or not pr.get("available"):
            raise RuntimeError(f"{label} is not installed (or not on the PATH) on {m['name']}'s laptop. Install it there, then press Rescan on the Team page and resume.")
        if pr.get("signed_in") is False:
            raise RuntimeError(f"{label} is not signed in on {m['name']}'s laptop. Run it once there and sign in, then press Rescan on the Team page and resume.")

    async def grant_access(self, p: dict) -> None:
        """A private repo created by one laptop is invisible to the others, so they could not push. Make everyone a collaborator and accept for them,
        each step run on the laptop whose own GitHub login must do it (the owner invites; every teammate accepts with their own account)."""
        cn = p["coordinator"]
        coord = self.member(p, cn)
        owner = p["repo"].split("/")[0].lower()
        host = (coord.get("gh_user") or "").lower()
        for m in p["members"]:
            u = (m.get("gh_user") or "").lower()
            if m["node_id"] == cn or not u or u in (host, owner):
                continue                                                           # the owner, or the same GitHub account: nothing to invite
            if (await self.op(m["node_id"], "repo_access", 60, repo=p["repo"])).get("push"):
                continue                                                           # already has write access (earlier run, org member, ...)
            await self.must(p, cn, "collab_add", 90, repo=p["repo"], user=m["gh_user"])
            self.log(p, "git", f"invited @{m['gh_user']} ({m['name']}) to {p['repo']} with write access")
            for attempt in range(6):                                              # the invitation can take a moment to appear
                r = await self.op(m["node_id"], "collab_accept", 60, repo=p["repo"])
                if r.get("ok") and r.get("accepted"):
                    self.log(p, "git", f"{m['name']}'s laptop accepted the invitation")
                    break
                if (await self.op(m["node_id"], "repo_access", 60, repo=p["repo"])).get("push"):
                    break
                await asyncio.sleep(3.0)
        bad = []
        for m in p["members"]:
            r = await self.op(m["node_id"], "repo_access", 60, repo=p["repo"])
            if not r.get("push"):
                bad.append(f"{m['name']} (@{m.get('gh_user') or '?'})")
        if bad:
            raise RuntimeError(f"cannot push to {p['repo']}: {', '.join(bad)}. Ask the repository owner to add them as collaborators with write access, or choose another repository, then resume.")
        self.log(p, "git", f"everyone can push to {p['repo']}")

    async def net_retry(self, p: dict, what: str, fn):
        """Run a project-level step; a network problem (laptop offline, GitHub unreachable) is retried with backoff instead of failing the project."""
        start, n = time.time(), 0
        while True:
            try:
                return await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if not TRANSIENT.search(msg) or time.time() - start > NET_GRACE_S or p["status"] not in ("running", "paused"):
                    raise
                delay = NET_BACKOFF[min(n, len(NET_BACKOFF) - 1)]
                n += 1
                if n in (1, 5, 20):
                    self.log(p, "net", f"{what}: {clip(msg, 160)}. Retrying automatically (next in {delay}s).")
                await asyncio.sleep(delay)
                while p["status"] == "paused":
                    await asyncio.sleep(1.0)

    async def bootstrap(self, p: dict) -> None:
        p["phase"] = "bootstrap"
        self.log(p, "phase", "preflight: checking git and GitHub CLI on every laptop")
        pre = {}
        for m in p["members"]:
            r = await self.must(p, m["node_id"], "preflight", 60)
            pre[m["node_id"]] = r
            m["git_email"], m["git_name"], m["gh_user"] = r.get("git_email", ""), r.get("git_name", ""), r.get("gh_user", "")
            m["skills"] = [x for x in (r.get("skills") or []) if isinstance(x, dict) and x.get("name")][:120]
            if not r.get("git"):
                raise RuntimeError(f"git is not installed on {m['name']}'s laptop")
            if p["forge"] == "github" and not r.get("gh"):
                raise RuntimeError(f"the GitHub CLI is not logged in on {m['name']}'s laptop (run `gh auth login` there)")
            if not r.get("git_email"):
                self.log(p, "warn", f"{m['name']}: no git user.email configured; commits may not be attributed")
            await self.check_agent(p, m)
        ok = [m for m in p["members"] if pre[m["node_id"]].get("gh") or p["forge"] == "local"]
        host_first = sorted(ok, key=lambda m: not getattr(self.hub.nodes.get(m["node_id"]), "hostname", "").lower() == getattr(self.hub, "hostname", "").lower())
        coord = (host_first or p["members"])[0]                            # the lead's own PC coordinates when it is on the team: contract, merges and the final commits are the lead's
        p["coordinator"] = coord["node_id"]
        cn = coord["node_id"]
        if not p["repo"]:
            r = await self.must(p, cn, "repo_create", 120, forge=p["forge"], repo_name=p["repo_name"], visibility=p["visibility"], path=f"kode/{p['slug']}/remote.git", base=p["base"], description=clip(p["brief"], 120))
            p["repo"] = r["repo"]
            self.log(p, "git", f"created {p['visibility']} repository {p['repo']}" if p["forge"] == "github" else f"created local repository {p['repo']}")
        if p["forge"] == "github":
            await self.net_retry(p, "repository access", lambda: self.grant_access(p))
        r = await self.must(p, cn, "git_prepare", 180, dir=self.cdir(p), repo=p["repo"], base=p["base"], forge=p["forge"])
        p["base_sha"] = r["head"]
        files = dict(p["plan"]["contract_files"])
        files["PLAN.md"] = self.render_plan_md(p)
        rules = render_rules(p)
        files["CLAUDE.md"] = rules
        files["AGENTS.md"] = rules
        files[".gitignore"] = ".kode/\nnode_modules/\n__pycache__/\n*.pyc\n.venv/\n"
        await self.must(p, cn, "write_files", 120, dir=self.cdir(p), base=p["base"], files=files, message="chore: add frozen interface contract, plan and team rules (K.O.D.E.)")
        p["contract_paths"] = sorted(p["plan"]["contract_files"].keys())
        self.log(p, "git", f"contract frozen ({len(p['contract_paths'])} files, sha {p['contract_sha'][:10]}) and pushed to {p['base']}")
        for t in p["tasks"]:
            t["status"] = "pending"
        p["phase"] = "build"
        self.save(p)

    def render_plan_md(self, p: dict) -> str:
        pl = p["plan"]
        rows = "\n".join(f"| {t['id']} | {t['title']} | {self.member(p, t['owner'])['name']} | {', '.join(t['files_owned'])} | {', '.join(t['depends_on']) or '-'} |" for t in p["tasks"])
        return f"# {p['name']}\n\n{pl['summary']}\n\nStack: {pl['stack']}\n\nVerification: `{'` and `'.join(pl['test_cmds'])}`\n\n| Task | Title | Owner | Files | Depends on |\n|---|---|---|---|---|\n{rows}\n"

    # ───────────── build ─────────────
    async def build(self, p: dict) -> None:
        while True:
            while p["status"] == "paused":
                await asyncio.sleep(1.0)
            if p["status"] != "running":
                return
            pend = [t for t in p["tasks"] if t["status"] not in ("merged", "cancelled")]
            if not pend:
                return
            merged = {t["id"] for t in p["tasks"] if t["status"] == "merged"}
            busy = {t["owner"] for t in p["tasks"] if t["status"] in ("running", "review", "starting")}
            for t in sorted(pend, key=lambda x: (-x["weight"], x["id"])):
                if t["status"] == "pending" and t["owner"] not in busy and all(d in merged for d in t["depends_on"]) and time.time() >= t.get("retry_at", 0) and self.node_ok(t["owner"]):
                    busy.add(t["owner"])
                    t["status"] = "starting"
                    asyncio.create_task(self._safe(p, t, self.start_task(p, t)))
            live = [t for t in p["tasks"] if t["status"] in ("running", "review")]
            await asyncio.gather(*(self._safe(p, t, self.tick_task(p, t)) for t in live))
            if any(t["status"] == "blocked" for t in p["tasks"]) and not live and not any(t["status"] in ("starting",) for t in p["tasks"]):
                p["note"] = "Waiting for the lead: a task is blocked (see the cards)."
            await asyncio.sleep(TICK_S)

    async def _safe(self, p: dict, t: dict, coro) -> None:
        try:
            await coro
            self.net_ok(t)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if t["status"] == "review":
                t["status"] = "running"                                        # never leave a task stuck in "merging" after a failure
            if TRANSIENT.search(msg) or not self.node_ok(t["owner"]):
                if t["status"] == "starting":
                    t["status"] = "pending" if not t.get("sid") else "running"
                self.net_wait(p, t, msg if TRANSIENT.search(msg) else f"{self.node_name(t['owner'])}'s laptop is offline")
                return
            t["attempts"] = t.get("attempts", 0) + 1
            self.log(p, "error", f"{t['id']}: {clip(msg, 300)}", t["id"])
            if t["attempts"] >= 3:
                t["status"] = "blocked"
                self.card(p, "blocked", f"Task {t['id']} ({t['title']}) is blocked: {clip(msg, 400)}", task=t["id"])
            elif t["status"] in ("starting", "running"):
                t["status"] = "pending" if not t.get("sid") else "running"

    async def start_task(self, p: dict, t: dict) -> None:
        m = self.member(p, t["owner"])
        nid, d = m["node_id"], self.mdir(p, m)
        branch = f"kode/{t['id'].lower()}-{slug(t['title'], 24)}" + (f"-r{t['round']}" if t.get("round") else "")
        t["branch"] = branch
        self.log(p, "task", f"{t['id']} '{t['title']}' -> {m['name']} ({m['provider']}) on branch {branch}", t["id"])
        await self.must(p, nid, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], branch=branch, forge=p["forge"], resume=bool(t.get("attempts") or t.get("restarts")))
        await self.must(p, nid, "write_task", 30, dir=d, fname="TASK.md", content=render_task(p, t, branch, p.get("contract_paths", []), m.get("skills")))
        from hub.apiprofiles import project_args
        ms = p.setdefault("msess", {}).get(nid) or {}
        cur = self.hub.sessions.get(ms.get("sid", ""))
        again = bool(t.get("attempts") or t.get("restarts"))             # a retried or restarted task gets a fresh agent; a stuck one is not reused
        reuse = cur if (cur and cur.state == "live" and not again and ms.get("tasks", 0) < MAX_REUSE and ms.get("dir") == d and ms.get("provider") == m["provider"]) else None
        if reuse is None:
            stopped = False
            for sid0 in {t.get("sid", ""), ms.get("sid", "")} - {""}:
                o = self.hub.sessions.get(sid0)
                if o and o.state in ("live", "starting"):
                    await self.hub.stop_session(o.sid, "orchestrator")                 # never two agents in one working folder
                    stopped = True
            if stopped:
                await asyncio.sleep(1.0)
            sid = await self.hub.start_session(nid, m["provider"], d, "orchestrator", profile_id=m.get("profile_id", ""), model=m.get("model", ""),
                                               extra_args=project_args(m["provider"], p["permissions"] in ("auto", "turbo"), p["permissions"] == "turbo", resume=bool(t.get("restarts"))))
            if not sid:
                raise RuntimeError(f"could not start {m['provider']} on {m['name']}'s laptop")
            t["sid"] = sid
            s = self.hub.sessions[sid]
            for _ in range(120):                                              # wait for the session to come up and the TUI banner to settle
                await asyncio.sleep(0.5)
                if s.state in ("error", "exited", "offline"):
                    raise RuntimeError(f"{m['provider']} failed to start: {s.error or s.state}")
                if s.state == "live" and time.time() - s.last_out > 2.5 and s.bytes_out > 0:
                    break
            await self.settle_prompts(p, t, s)
            p["msess"][nid] = {"sid": sid, "tasks": 0, "dir": d, "provider": m["provider"]}
            first = f"You are a teammate on project {p['name']}. Read .kode/TASK.md and CLAUDE.md, do exactly what the task says, use your relevant skills, commit sensibly, push your branch, then print KODE_DONE and the number of commits."
        else:
            s, sid = reuse, reuse.sid
            t["sid"] = sid
            first = f"Next task for you: {t['id']} ({t['title']}). .kode/TASK.md was just replaced and the branch switched. Read it again, do exactly what it says (use your relevant skills), commit sensibly, push your branch, then print KODE_DONE and the number of commits."
            self.log(p, "task", f"{t['id']}: reusing {m['name']}'s running agent session (task {ms.get('tasks', 0) + 1} of at most {MAX_REUSE} for it)", t["id"])
        p["msess"][nid]["tasks"] += 1
        t["mark0"] = t["prompt_mark"] = s.bytes_out                         # only text printed after this point counts as this task's progress
        t["dlg_sig"] = ""
        await self.type_line(s, first)
        asyncio.create_task(self.ensure_submitted(s, "Read .kode/TASK.md" if reuse is None else "Read it again"))
        t["status"], t["started"], t["last_progress"] = "running", time.time(), time.time()
        t["last_head"], t["settled"] = "", 0
        self.save(p)

    async def settle_prompts(self, p: dict, t: dict, s) -> None:
        """First-run dialogs (trust this folder? pick a theme?) are answered before the task prompt is typed, so it is not typed into a menu.
        Only a screen that has stopped changing counts as a dialog (a spinner means the agent is already working), terms/logins are never answered."""
        mark = max(0, s.bytes_out - 1800)
        for _ in range(45):
            idle = time.time() - s.last_out
            if idle < 1.5 and not s.needs_input():
                await asyncio.sleep(1.0)
                continue
            tail = self.fresh(s, mark)
            if CONSENT.search(tail[-900:]):
                await self.need_setup(p, t, s, tail)
                continue
            if s.needs_input() or STARTUP.search(tail[-900:]):
                r = "error" if STARTUP.search(tail[-900:]) and not DANGEROUS.search(tail[-600:]) else                     await self.supervise(p, t, s, tail, "the agent was just launched and a start-up dialog may be open")
                if r == "acted":
                    mark = s.bytes_out
                    await asyncio.sleep(2.0)
                    continue
                if r in ("human", "wait"):
                    return
                if DANGEROUS.search(tail[-600:]):
                    await self.handle_prompt(p, t, s)
                    return
                await self.hub.ensure_copilot(s)
                keys = self.keys_for(tail[-700:])
                if not keys:
                    return
                await self.hub.lead_input(self.hub.orch.actor, s, keys, label="project")
                self.log(p, "prompt", f"{t['id']}: start-up dialog on {s.label} confirmed with Enter [{clip(tail[-140:], 140)}]", t["id"])
                mark = s.bytes_out
                await asyncio.sleep(3.0)
                continue
            return

    async def need_setup(self, p: dict, t: dict, s, tail: str) -> None:
        """One-time setup (terms, login, telemetry choice) on a teammate's laptop: only a human decides. Ask once, then wait for the screen to move on."""
        if not any(c.get("sid") == s.sid and c["kind"] == "setup" for c in p.get("cards", [])):
            self.card(p, "setup", f"{s.label} on {self.member(p, t['owner'])['name']}'s laptop is showing a one-time setup screen (terms, login or data-sharing choice). K.O.D.E. will not answer that for you. Open its console, complete it, and the task continues by itself." + chr(10) * 2 + clip(tail[-500:], 500), task=t["id"], sid=s.sid)
            self.log(p, "setup", f"{t['id']}: waiting for you to complete {s.label}'s one-time setup", t["id"])
        t["setup_wait"] = t.get("setup_wait", 0) + 1
        if t["setup_wait"] > 600:
            raise RuntimeError(f"{s.label} still needs its one-time setup after 20 minutes")
        await asyncio.sleep(2.0)

    async def handle_prompt(self, p: dict, t: dict, s) -> None:
        now = time.time()
        if now - t.get("prompt_at", 0) < PROMPT_COOLDOWN_S:
            return
        text = self.fresh(s, t.get("prompt_mark", 0), 900) or self.screen(s, 900)
        if any(c.get("sid") == s.sid and c["kind"] == "prompt" for c in p.get("cards", [])):
            return
        if p["permissions"] in ("auto", "turbo") and not DANGEROUS.search(text) and not CONSENT.search(text) and t.get("auto_answers", 0) < 80:
            t["prompt_at"], t["auto_answers"] = now, t.get("auto_answers", 0) + 1
            await self.hub.ensure_copilot(s)
            keys = self.keys_for(text[-700:])
            t["prompt_mark"] = s.bytes_out
            if not keys:
                return
            await self.hub.lead_input(self.hub.orch.actor, s, keys, label="project")
            self.log(p, "prompt", f"{t['id']}: agent asked a question, confirmed with Enter (auto permissions) [{clip(text[-160:], 160)}]", t["id"])
        else:
            t["prompt_at"] = now
            self.card(p, "prompt", f"The agent on {t['id']} is waiting for you:\n{clip(text[-700:], 700)}", task=t["id"], sid=s.sid)

    async def answer_card(self, p: dict, c: dict, action: str) -> None:
        keys = {"enter": "\r", "yes": "y\r", "no": "n\r", "esc": "\x1b"}
        s = self.hub.sessions.get(c.get("sid", "")) if c.get("sid") else None
        if c["kind"] == "prompt" and action in keys and s and s.state == "live":
            await self.hub.ensure_copilot(s)
            await self.hub.lead_input(self.hub.orch.actor, s, keys[action], label="project")
        elif c["kind"] == "blocked" and action == "retry":
            tk = next((x for x in p["tasks"] if x["id"] == c.get("task")), None)
            if tk:
                tk["status"], tk["attempts"] = "pending", 0
                if tk.get("sid"):
                    await self.hub.stop_session(tk["sid"], "orchestrator")
                    tk["sid"] = ""
        elif c["kind"] == "blocked" and action == "skip":
            tk = next((x for x in p["tasks"] if x["id"] == c.get("task")), None)
            if tk:
                tk["status"] = "merged"                                   # lead accepts the gap; dependants may proceed
                tk["skipped"] = True
        self.drop_card(p, c["id"])

    async def tick_task(self, p: dict, t: dict) -> None:
        if t["status"] == "review":
            return
        if time.time() < t.get("retry_at", 0):
            return
        m = self.member(p, t["owner"])
        nid, d = m["node_id"], self.mdir(p, m)
        s = self.hub.sessions.get(t["sid"])
        node = self.hub.nodes.get(nid)
        if not node or not node.online:                                       # laptop offline: its session keeps running locally; wait, do not restart
            self.net_wait(p, t, f"{m['name']}'s laptop is offline")
            return
        if s and s.state == "offline" and time.time() - node.connected_at < REATTACH_S:
            return                                                            # just reconnected: the session list is still being re-announced
        self.net_ok(t)
        dead = (not s) or s.state in ("exited", "error", "offline")
        if s and not dead:
            fr = self.fresh(s, t.get("prompt_mark", 0), 1200)
            hit = bool(STARTUP.search(fr) or PERMISSION.search(fr) or CONSENT.search(fr)) or (s.needs_input() and bool(fr))
            # Some TUIs keep blinking a dot while a dialog waits, so "no output" is the wrong test: the dialog is real once the same
            # question text has been on screen (ignoring the blinking glyphs) for a few seconds.
            sig = re.sub(r"[●○◐-◓·•⠋-⣿\s]+", "", fr)[-200:] if hit else ""
            if hit and sig == t.get("dlg_sig"):
                if time.time() - t.get("dlg_since", time.time()) >= 3.0:
                    t["dlg_sig"] = ""
                    r = await self.supervise(p, t, s, self.screen(s, 1800), "a dialog or question is on screen")
                    if r == "error":
                        await self.handle_prompt(p, t, s)
            else:
                t["dlg_sig"], t["dlg_since"] = sig, time.time()
            quiet0 = time.time() - t.get("last_progress", time.time())
            if not hit and time.time() - s.last_out > 30 and quiet0 > 60 and not DONE_MARK.search(self.fresh(s, t.get("mark0", 0), 1500)):
                await self.supervise(p, t, s, self.screen(s, 1800), "the agent has printed nothing for 30s and made no commit for a minute")
        gs = await self.op(nid, "git_status", 60, dir=d, base=p["base"], branch=t["branch"])
        if not gs.get("ok"):
            raise RuntimeError("laptop unreachable: " + gs.get("err", ""))                     # transient: _safe backs off and retries
        t["commits"], t["added"], t["removed"] = gs["ahead"], gs["added"], gs["removed"]
        t["changed"] = gs["changed"][:40]
        if gs["head"] != t.get("last_head"):
            t["last_head"], t["last_progress"] = gs["head"], time.time()
        idle = time.time() - s.last_out if s and not dead else 999
        marker = bool(s and DONE_MARK.search(self.fresh(s, t.get("mark0", 0), 2500)))
        enough = gs["ahead"] >= t["min_commits"] or (marker and gs["ahead"] >= 1)
        finished = enough and (idle >= IDLE_DONE_S or (marker and idle >= MARKER_IDLE_S)) and (gs["pushed"] or idle >= IDLE_DONE_S * 2)
        if dead and not marker:
            finished = False                                              # one commit no longer proves a task is complete: an agent that died without saying it is done gets restarted first
        if dead and gs["ahead"] >= 1 and t.get("restarts", 0) >= 2:
            finished = True                                                   # restarted twice and still short of the target: merge what exists, verification decides
        if finished or (dead and gs["ahead"] == 0 and t.get("restarts", 0) >= 2):
            if gs["ahead"] == 0:
                raise RuntimeError("the agent exited without making any commit")
            await self.complete_task(p, t, m, d, gs)
            return
        if dead:                                                          # agent ended early with nothing usable: restart on the same branch
            t["restarts"] = t.get("restarts", 0) + 1
            self.log(p, "task", f"{t['id']}: agent session ended before finishing; restarting (attempt {t['restarts']})", t["id"])
            self.retire(t["sid"], 120.0)
            t["status"] = "starting"
            t["attempts"] = max(t.get("attempts", 0), 0)
            await self.start_task(p, t)
            return
        # still working: nudge if the agent finished its turn without finishing the job, or has stalled
        quiet = time.time() - t.get("last_progress", time.time())
        if s and not dead and s.mode not in ("copilot", "takeover"):
            await self.hub.ensure_copilot(s)                      # e.g. after a Brain restart every session is back to watch-only
        if quiet < 120 and any(c.get("task") == t["id"] and c["kind"] == "stalled" for c in p.get("cards", [])):
            p["cards"] = [c for c in p["cards"] if not (c.get("task") == t["id"] and c["kind"] == "stalled")]
            self.save(p)
        if s and not dead and DIFFVIEW.search(self.screen(s, 1500)):
            if await self.supervise(p, t, s, self.screen(s, 1800), "a file-review screen is open") != "acted":
                if not any(c.get("sid") == s.sid and c["kind"] == "blocked" for c in p.get("cards", [])):
                    self.card(p, "blocked", f"{s.label} on {self.member(p, t['owner'])['name']}'s laptop is on a file-review screen. Press Retry to restart this task in accept-edits mode, or open the console to answer it yourself.", task=t["id"], sid=s.sid)
            return
        if (idle >= NUDGE_IDLE_S or quiet >= NUDGE_QUIET_S * 2) and quiet >= NUDGE_QUIET_S and (gs["dirty"] or not gs["pushed"] or gs["ahead"] < t["min_commits"]) and t["nudges"] < 3 and time.time() - t.get("nudge_at", 0) > 90:
            t["nudges"] += 1
            t["nudge_at"] = time.time()
            need = []
            if gs["dirty"]:
                need.append("commit your uncommitted changes")
            if gs["ahead"] < 1:
                need.append("commit your work (nothing is committed yet)")
            if not gs["pushed"]:
                need.append(f"push with: git push -u origin {t['branch']}")
            await self.type_line(s, f"Status check from the team lead. Continue .kode/TASK.md. Still to do: {'; '.join(need) or 'finish the task'}. Then print KODE_DONE <number of commits>.")
            asyncio.create_task(self.ensure_submitted(s, "Status check from the team lead"))
            self.log(p, "nudge", f"{t['id']}: reminded the agent ({'; '.join(need)})", t["id"])
        elif quiet > STALL_S and t["nudges"] >= 3 and not any(c.get("task") == t["id"] and c["kind"] == "stalled" for c in p["cards"]):
            self.card(p, "stalled", f"Task {t['id']} ({t['title']}) has made no progress for {int(quiet / 60)} minutes. Open its console to help, or use Retry.", task=t["id"], sid=t["sid"])

    def out_of_scope(self, p: dict, t: dict, changed: List[str]) -> List[str]:
        return [f for f in changed if not path_in_scope(f, t["files_owned"]) or any(path_in_scope(f, [c]) for c in p.get("contract_paths", []))]

    async def complete_task(self, p: dict, t: dict, m: dict, d: str, gs: dict) -> None:
        nid = m["node_id"]
        t["status"] = "review"
        self.save(p)
        if gs["dirty"] or not gs["pushed"]:                               # safety net: the engine saves and pushes whatever the agent left behind
            await self.must(p, nid, "git_push", 90, dir=d, branch=t["branch"], commit_dirty=True, message=f"chore: save unfinished work for {t['id']}")
            gs = await self.must(p, nid, "git_status", 60, dir=d, base=p["base"], branch=t["branch"])
        bad = self.out_of_scope(p, t, gs["changed"])
        if bad:                                                           # code, not prompts, enforces ownership and the contract
            t["violations"] += len(bad)
            await self.must(p, nid, "scope_revert", 90, dir=d, base=p["base"], branch=t["branch"], files=bad, message=f"chore: revert out-of-scope changes to {len(bad)} file(s) (K.O.D.E. scope guard)")
            self.log(p, "guard", f"{t['id']}: reverted {len(bad)} out-of-scope/contract file(s): {', '.join(bad[:5])}", t["id"])
        pr = await self.must(p, nid, "pr_create", 120, dir=d, base=p["base"], branch=t["branch"], forge=p["forge"], title=f"[{t['id']}] {t['title']}",
                             body=f"{t['description']}\n\nOwner: {m['name']}\nCommits: {gs['ahead']}\nManaged by K.O.D.E.")
        t["pr_url"], t["pr_number"] = pr["url"], pr.get("number", 0)
        self.log(p, "pr", f"{t['id']}: pull request opened {pr['url']}", t["id"])
        await self.ensure_coordinator(p)
        async with self.locks.setdefault(p["id"], asyncio.Lock()):
            # authoritative check on the shared remote just before merging: late pushes from the agent cannot slip a violation in
            bf = await self.must(p, p["coordinator"], "branch_files", 90, dir=self.cdir(p), base=p["base"], branch=t["branch"])
            late = self.out_of_scope(p, t, bf["changed"])
            if late:
                t["violations"] += len(late)
                await self.must(p, p["coordinator"], "scope_revert", 120, dir=self.cdir(p), base=p["base"], branch=t["branch"], files=late, checkout=True,
                                message=f"chore: revert out-of-scope changes to {len(late)} file(s) (K.O.D.E. scope guard)")
                self.log(p, "guard", f"{t['id']}: reverted {len(late)} more out-of-scope/contract file(s) before merging: {', '.join(late[:5])}", t["id"])
            for attempt in range(3):
                r = await self.op(p["coordinator"], "pr_merge", 150, dir=self.cdir(p), base=p["base"], branch=t["branch"], url=t["pr_url"], forge=p["forge"])
                if r.get("ok"):
                    break
                self.log(p, "merge", f"{t['id']}: merge failed ({clip(r.get('err', ''), 160)}); asking the agent to rebase", t["id"])
                s = self.hub.sessions.get(t["sid"])
                fixed = False
                if s and s.state == "live":
                    await self.type_line(s, f"The merge of your branch into {p['base']} failed: run `git fetch origin && git rebase origin/{p['base']}`, resolve conflicts keeping BOTH sides' intent only inside your own files, then `git push --force-with-lease origin {t['branch']}`. Print KODE_DONE when finished.")
                    for _ in range(60):
                        await asyncio.sleep(3)
                        st = await self.op(nid, "git_status", 60, dir=d, base=p["base"], branch=t["branch"])
                        if st.get("ok") and st["behind"] == 0 and st["pushed"] and not st["dirty"]:
                            fixed = True
                            break
                if not fixed:
                    rb = await self.op(nid, "rebase_push", 180, dir=d, base=p["base"], branch=t["branch"])
                    fixed = rb.get("ok", False)
                if not fixed and attempt == 2:
                    t["status"] = "blocked"
                    self.card(p, "blocked", f"Task {t['id']} cannot be merged automatically: {clip(r.get('err', ''), 300)}", task=t["id"])
                    return
            else:
                return
        if t["status"] == "blocked":
            return
        t["status"], t["done_at"] = "merged", time.time()
        if t.get("sid"):
            ms = p.get("msess", {}).get(nid, {})
            more = any(x["owner"] == nid and x is not t and x["status"] not in ("merged", "cancelled") for x in p["tasks"])
            if not (more and ms.get("sid") == t["sid"] and ms.get("tasks", 0) < MAX_REUSE and p["status"] == "running"):
                await self.hub.stop_session(t["sid"], "orchestrator")          # nothing more for this teammate: end the session
                self.retire(t["sid"])
        self.log(p, "merge", f"{t['id']} merged into {p['base']} ({gs['ahead']} commits by {m['name']})", t["id"])

    # ───────────── verify / fix / finish ─────────────
    async def verify(self, p: dict) -> bool:
        await self.ensure_coordinator(p)
        cn, d = p["coordinator"], self.cdir(p)
        await self.must(p, cn, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], forge=p["forge"])
        results = []
        for cmd in p["plan"].get("setup_cmds", []) + p["plan"]["test_cmds"]:
            r = await self.op(cn, "run", 420, dir=d, cmd=cmd, cmd_timeout=360)
            results.append({"cmd": cmd, "code": r.get("code", 1) if r.get("ok") else 1, "out": r.get("out", r.get("err", ""))})
            self.log(p, "verify", f"$ {cmd} -> exit {results[-1]['code']}")
            if results[-1]["code"] != 0:
                break
        p["verify"] = {"round": p["round"], "ts": time.time(), "results": results, "passed": all(x["code"] == 0 for x in results)}
        self.save(p)
        return p["verify"]["passed"]

    async def triage(self, p: dict) -> None:
        p["round"] += 1
        self.log(p, "phase", f"verification failed: triage round {p['round']} of {MAX_FIX_ROUNDS}")
        failing = [x for x in p["verify"]["results"] if x["code"] != 0][-1]
        files = await self.op(p["coordinator"], "files", 30, dir=self.cdir(p))
        owners = "\n".join(f"- {t['id']} ({t['title']}): {', '.join(t['files_owned'])}" for t in p["tasks"] if t.get("round", 0) == 0)
        user = (f"PROJECT: {p['name']}\nFAILED COMMAND: {failing['cmd']} (exit {failing['code']})\nOUTPUT (tail):\n{failing['out'][-3500:]}\n\n"
                f"TASK AREAS:\n{owners}\n\nFILES IN REPO:\n" + "\n".join((files.get('files') or [])[:150]))
        client = self.client(p)
        fix, feedback = None, ""
        for _ in range(3):
            res = await client.complete(TRIAGE_SYSTEM, user + feedback, max_tokens=2500)
            try:
                obj = extract_json(res.text)
                tasks = obj.get("fix_tasks")
                if not isinstance(tasks, list) or not (1 <= len(tasks) <= 4):
                    raise PlanError("'fix_tasks' must be 1..4 items")
                for ft in tasks:
                    if not ft.get("files_owned") or any(not _safe_rel(str(g).replace("*", "x")) for g in ft["files_owned"]):
                        raise PlanError("each fix task needs safe relative files_owned")
                    for g in ft["files_owned"]:
                        if any(globs_overlap(g, c) for c in p["contract_paths"]):
                            raise PlanError("fix tasks must not edit contract files")
                for i, a in enumerate(tasks):
                    for b in tasks[i + 1:]:
                        if any(globs_overlap(x, y) for x in a["files_owned"] for y in b["files_owned"]):
                            raise PlanError("fix tasks must have disjoint files_owned")
                fix = obj
                break
            except (LLMOutputError, PlanError, ValueError, TypeError, AttributeError) as e:
                feedback = f"\n\nYour previous reply was rejected: {e}. Reply with ONLY the JSON object."
        if fix is None:
            raise PlanError("triage could not produce valid fix tasks")
        self.log(p, "triage", clip(str(fix.get("diagnosis", "")), 350))
        load = {m["node_id"]: sum(t["weight"] for t in p["tasks"] if t["owner"] == m["node_id"]) for m in p["members"]}
        for i, ft in enumerate(fix["fix_tasks"]):
            orig = next((t for t in p["tasks"] if t["id"] == ft.get("owner_task")), None)
            owner = orig["owner"] if orig else min(load, key=load.get)
            load[owner] += 1
            tid = f"F{p['round']}-{i + 1}"
            p["tasks"].append({"id": tid, "title": clip(str(ft.get("title", "fix")), 120), "description": clip(str(ft.get("description", "")), 3000),
                               "acceptance": f"`{failing['cmd']}` passes.", "files_owned": [str(g) for g in ft["files_owned"]], "depends_on": [],
                               "weight": 1, "owner": owner, "min_commits": 1, "status": "pending", "sid": "", "branch": "", "pr_url": "", "commits": 0,
                               "round": p["round"], "attempts": 0, "violations": 0, "nudges": 0, "log": []})
        self.save(p)

    # ───────────── final quality review ─────────────
    def hygiene_issues(self, a: dict) -> List[dict]:
        out = []
        if a.get("readme_len", 0) < 400 or not a.get("readme_has_run") or not a.get("readme_has_test"):
            out.append({"title": "Write a complete README", "files": ["README.md"], "severity": "major",
                        "description": "README.md is missing or too thin. Write it for a first-time visitor: what the project is and who it is for, features, prerequisites, setup, how to run it, how to run the tests, usage examples and the project structure. Use only commands that really work in this repository."})
        if a.get("stray"):
            out.append({"title": "Remove files that do not belong in the repository", "files": list(a["stray"])[:20], "severity": "major",
                        "description": "These tracked files are build output, caches, logs or tool scratch and must be deleted from git: " + ", ".join(a["stray"][:20]) + ". Add matching patterns to .gitignore if it is yours to edit."})
        if a.get("secrets"):
            files = sorted({x.split(":")[0] for x in a["secrets"]})
            out.append({"title": "Remove secrets from the code", "files": files[:10], "severity": "blocker",
                        "description": "Possible secrets or keys were found at: " + ", ".join(a["secrets"]) + ". Replace them with environment variables and document them in the README."})
        if a.get("todos"):
            files = sorted({x.split(":")[0] for x in a["todos"]})
            out.append({"title": "Resolve TODO / FIXME / placeholder text", "files": files[:10], "severity": "major",
                        "description": "Finish or remove these leftovers: " + "; ".join(a["todos"][:8])})
        return out

    async def quality_review(self, p: dict) -> bool:
        """Last gate before 'done': a hygiene audit (code) plus a strict AI review against the brief. True = clean (or rounds exhausted, notes kept)."""
        await self.ensure_coordinator(p)
        cn, d = p["coordinator"], self.cdir(p)
        await self.must(p, cn, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], forge=p["forge"])
        a = await self.must(p, cn, "repo_audit", 120, dir=d)
        issues = self.hygiene_issues(a)
        review_note = ""
        try:
            tasks = "\n".join(f"- {t['title']}: {t.get('acceptance', '')}" for t in p["tasks"] if not t.get("round"))
            vt = "\n".join(f"$ {x['cmd']} -> exit {x['code']}\n{x['out'][-700:]}" for x in (p.get("verify") or {}).get("results", []))
            samples = "\n\n".join(f"--- {k} ---\n{v}" for k, v in (a.get("samples") or {}).items())
            user = (f"BRIEF:\n{p['brief']}\n\nTASKS AND ACCEPTANCE:\n{tasks}\n\nAUDIT: files={a.get('count')} tests={a.get('has_tests')} gitignore={a.get('has_gitignore')} "
                    f"license={a.get('has_license')} stray={a.get('stray')} secrets={a.get('secrets')} todos={a.get('todos')} big={a.get('big')}\n\nREADME.md:\n{a.get('readme') or '(missing)'}\n\n"
                    f"TEST RUN:\n{vt}\n\nFILES:\n" + "\n".join((a.get('files') or [])[:120]) + f"\n\nSAMPLE SOURCE:\n{samples}")
            res = await asyncio.wait_for(self.client(p).complete(REVIEW_SYSTEM, user, max_tokens=2500), 420)
            obj = extract_json(res.text)
            review_note = clip(str(obj.get("verdict", "")), 20)
            for it in (obj.get("issues") or [])[:5]:
                if not isinstance(it, dict) or str(it.get("severity", "minor")) not in ("blocker", "major"):
                    continue
                files = [str(f) for f in (it.get("files") or []) if _safe_rel(str(f).replace("*", "x")) and not any(globs_overlap(str(f), c) for c in p["contract_paths"])]
                if files:
                    issues.append({"title": clip(str(it.get("title", "Improve quality")), 100), "description": clip(str(it.get("description", "")), 1500), "files": files[:10], "severity": str(it["severity"])})
        except Exception as e:                                                  # the model is a bonus: hygiene checks above always apply
            self.log(p, "warn", f"AI review unavailable ({type(e).__name__}); using the automatic hygiene checks only")
        keep: List[dict] = []
        for it in issues:                                                       # fix tasks must own disjoint files
            if not any(globs_overlap(x, y) for k in keep for x in k["files"] for y in it["files"]):
                keep.append(it)
        p["quality"] = {"round": p.get("polish_round", 0), "verdict": review_note, "issues": [{"title": k["title"], "severity": k["severity"]} for k in keep],
                        "readme": a.get("readme_len", 0), "files": a.get("count", 0), "tests": a.get("has_tests"), "stray": len(a.get("stray") or []), "secrets": len(a.get("secrets") or [])}
        if not keep:
            self.log(p, "quality", "quality review: clean")
            self.save(p)
            return True
        if p.get("polish_round", 0) >= MAX_POLISH_ROUNDS:
            self.log(p, "quality", f"quality review: {len(keep)} remaining note(s) after {MAX_POLISH_ROUNDS} polish rounds: " + "; ".join(k["title"] for k in keep[:4]))
            self.save(p)
            return True
        p["polish_round"] = p.get("polish_round", 0) + 1
        self.log(p, "quality", f"quality review round {p['polish_round']}: {len(keep)} improvement task(s): " + "; ".join(k["title"] for k in keep[:4]))
        self.add_fix_tasks(p, keep[:5], f"Q{p['polish_round']}", 100 + p["polish_round"])
        return False

    def add_fix_tasks(self, p: dict, items: List[dict], prefix: str, rnd: int) -> None:
        """Improvement tasks go to whoever has the least work so far (the lead's PC included), each on its own disjoint files."""
        load = {m["node_id"]: sum(t["weight"] for t in p["tasks"] if t["owner"] == m["node_id"]) for m in p["members"]}
        for i, it in enumerate(items):
            owner = min(load, key=load.get)
            load[owner] += 1
            p["tasks"].append({"id": f"{prefix}-{i + 1}", "title": it["title"], "description": it["description"], "acceptance": "A reviewer would no longer flag this.",
                               "files_owned": it["files"], "depends_on": [], "weight": 1, "owner": owner, "min_commits": 1, "status": "pending", "sid": "", "branch": "", "pr_url": "",
                               "commits": 0, "round": rnd, "attempts": 0, "violations": 0, "nudges": 0, "log": []})
        self.save(p)

    # ───────────── the code judge ─────────────
    def judge_clients(self, p: dict) -> List[tuple]:
        """Independent judges: one per distinct model source on the team (own CLI login or API profile), the planner as a fallback; at most three."""
        out, seen = [], set()
        for m in p["members"]:
            key = ("profile", m["profile_id"]) if m.get("profile_id") else ("cli", m["provider"]) if m["provider"] in ("claude", "codex") else None
            if key and key not in seen:
                seen.add(key)
                out.append((f"{m['provider']}" + (f" / {m['profile_id']}" if m.get("profile_id") else " (own login)"), self.member_client(p, m)))
        if not out:
            out.append((self.planner_label(p), self.client(p)))
        return out[:3]

    async def judge_review(self, p: dict, rejudge: bool = False) -> bool:
        """Score the finished repository. True = done (scored, or judging unavailable); False = improvement tasks were added and the build must run again."""
        await self.ensure_coordinator(p)
        cn, d = p["coordinator"], self.cdir(p)
        await self.must(p, cn, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], forge=p["forge"])
        a = await self.must(p, cn, "repo_audit", 120, dir=d)
        caps = jd.caps_from_audit(a, bool((p.get("verify") or {}).get("passed")))
        clients = self.judge_clients(p)
        self.log(p, "judge", f"judging with {len(clients)} independent judge(s): " + ", ".join(c[0] for c in clients))
        try:
            card = await jd.run_judges(clients, jd.evidence(p, a), caps)
        except Exception as e:
            self.log(p, "warn", f"the code judge could not score the project: {clip(str(e), 220)}")
            p["judge"] = {"error": clip(str(e), 300), "ts": time.time()}
            self.save(p)
            return True
        card["ts"], card["round"], card["loc"] = time.time(), p.get("judge_round", 0), a.get("loc", 0)
        hist = (p.get("judge") or {}).get("history", []) if isinstance(p.get("judge"), dict) else []
        card["history"] = (hist + [card["overall"]])[-6:]
        p["judge"] = card
        self.log(p, "judge", f"score {card['overall']}/100 (grade {card['grade']})" + (f"; judges disagree on {len(card['disagreement'])} criterion/criteria" if card["disagreement"] else ""))
        self.save(p)
        if rejudge or card["overall"] >= jd.JUDGE_PASS or p.get("judge_round", 0) >= 1:
            return True
        items = []
        for it in card["improvements"]:
            if it["severity"] == "minor":
                continue
            files = [f for f in it["files"] if _safe_rel(f.replace("*", "x")) and not any(globs_overlap(f, c) for c in p["contract_paths"])]
            if files and not any(globs_overlap(x, y) for k in items for x in k["files"] for y in files):
                items.append({"title": it["title"], "description": it["detail"], "files": files[:10]})
        if not items:
            return True
        p["judge_round"] = p.get("judge_round", 0) + 1
        self.log(p, "judge", f"below {jd.JUDGE_PASS}: one improvement round with {len(items[:4])} task(s) from the judges' findings")
        self.add_fix_tasks(p, items[:4], f"J{p['judge_round']}", 200 + p["judge_round"])
        return False

    async def rejudge(self, p: dict) -> None:
        try:
            await self.judge_review(p, rejudge=True)
        except Exception as e:
            self.log(p, "warn", f"re-judging failed: {clip(str(e), 200)}")

    SUBMISSION_SYSTEM = """You write the submission text for a hackathon entry (Devpost style) from the facts given. Be concrete and honest: describe only what the repository really does.
Markdown with these sections: a one-line tagline; Inspiration / the problem; What it does; How we built it (stack and architecture); Challenges; What is next; A 3-minute demo script (numbered steps).
No hype words, no invented features or statistics. Reply with the markdown only."""

    async def submission_pack(self, p: dict) -> None:
        """docs/SUBMISSION.md: the write-up a hackathon form asks for, from the brief, the plan, the README and the judge's scorecard. Failure is not fatal."""
        try:
            await self.ensure_coordinator(p)
            cn, d = p["coordinator"], self.cdir(p)
            a = await self.must(p, cn, "repo_audit", 120, dir=d)
            j = p.get("judge") or {}
            facts = (f"PROJECT: {p['name']}\nPROBLEM / BRIEF:\n{p.get('problem') or p['brief']}\n\nPLAN SUMMARY: {(p.get('plan') or {}).get('summary', '')}\nSTACK: {(p.get('plan') or {}).get('stack', '')}\n"
                     f"CHOSEN IDEA: {(p.get('ideation') or {}).get('winner_title', '-')}\n\nREADME:\n{a.get('readme', '')[:3500]}\n\nFILES:\n" + "\n".join((a.get("files") or [])[:80])
                     + (f"\n\nSELF-ASSESSMENT (automatic judge): {j.get('overall')}/100; strengths: {'; '.join(j.get('strengths', []))}" if j.get("overall") else ""))
            res = await asyncio.wait_for(self.client(p).complete(self.SUBMISSION_SYSTEM, facts, max_tokens=2500), 420)
            text = res.text.strip()
            if len(text) < 300:
                return
            await self.must(p, cn, "write_files", 120, dir=d, base=p["base"], files={"docs/SUBMISSION.md": text + "\n"}, message="docs: add hackathon submission write-up (K.O.D.E.)")
            self.log(p, "git", "wrote docs/SUBMISSION.md (tagline, story, architecture and a demo script)")
        except Exception as e:
            self.log(p, "warn", f"submission write-up skipped: {clip(str(e), 160)}")

    async def close_sessions(self, p: dict) -> None:
        for ms in list((p.get("msess") or {}).values()):
            o = self.hub.sessions.get(ms.get("sid", ""))
            if o and o.state in ("live", "starting"):
                await self.hub.stop_session(o.sid, "orchestrator")
                self.retire(o.sid)

    async def finish(self, p: dict, failed: bool = False) -> None:
        await self.close_sessions(p)
        cn = p["coordinator"]
        stats = await self.op(cn, "log_stats", 60, dir=self.cdir(p), base=p["base"], since=p.get("base_sha", ""))
        by_email = {a["email"].lower(): a for a in stats.get("authors", [])}
        bal = []
        for m in p["members"]:
            a = by_email.get((m.get("git_email") or "").lower(), {})
            tasks = [t for t in p["tasks"] if t["owner"] == m["node_id"]]
            bal.append({"member": m["name"], "email": m.get("git_email", ""), "commits": a.get("commits", 0), "added": a.get("added", 0), "removed": a.get("removed", 0),
                        "tasks": len(tasks), "merged": sum(1 for t in tasks if t["status"] == "merged"), "violations": sum(t.get("violations", 0) for t in tasks)})
        p["final"] = {"balance": bal, "passed": not failed, "rounds": p["round"], "finished": time.time(), "quality": p.get("quality"),
                      "judge": ({k: v for k, v in (p.get("judge") or {}).items() if k in ("overall", "grade")} or None)}
        if not failed:
            p["status"], p["phase"], p["note"] = "done", "done", "Project complete: all tasks merged and the verification commands pass."
            self.log(p, "done", "project complete; verification passed")
        self.save(p)
