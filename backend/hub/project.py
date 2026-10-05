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

from hub import context as ctx, ideate, judge as jd
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
BUSY_RX = re.compile(r"esc to interrupt|ctrl\+c to interrupt|Working \(|\(esc to cancel\)|Thinking|[\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f]")
LIMIT_RX = re.compile(r"usage limit|rate[- ]limit(?:ed)?|limit reached|quota (?:exceeded|exhausted)|out of (?:credits|tokens)|insufficient (?:credits|quota|balance)|credit balance is too low|you(?:'|\u2019)ve hit your|overloaded_error|resource_exhausted|too many requests", re.I)
API_ERR = re.compile(r"API error|Retrying in \d+s|attempt \d+/\d+|ECONNRESET|connection (?:error|reset)", re.I)
BACKUP_ORDER = ("commandcode", "claude", "codex", "agy", "opencode", "hermes")
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
        must = [clip(str(x).strip(), 200) for x in (t.get("must") or []) if str(x).strip()][:6] if isinstance(t.get("must"), list) else []
        must_not = [clip(str(x).strip(), 200) for x in (t.get("must_not") or []) if str(x).strip()][:6] if isinstance(t.get("must_not"), list) else []
        if not files or any(not _safe_rel(f.replace("*", "x")) for f in files):
            raise PlanError(f"task {tid}: 'files_owned' needs relative paths/globs (no '..', no absolute paths, nothing under .git or .kode)")
        title, desc = clip(str(t.get("title", "")).strip(), 120), clip(str(t.get("description", "")).strip(), 3000)
        if not title or not desc:
            raise PlanError(f"task {tid}: needs a title and a description")
        weight = max(1, min(3, int(t.get("weight") or 1)))
        out.append({"id": tid, "title": title, "description": desc, "acceptance": clip(str(t.get("acceptance", "")).strip(), 1200),
                    "files_owned": files, "depends_on": [str(d) for d in (t.get("depends_on") or [])], "weight": weight, "must": must, "must_not": must_not})
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
            "files_owned": ["api/**"], "depends_on": [], "weight": 1,
            "must": ["2-5 short things this task has to deliver"], "must_not": ["2-5 short things it must not do, e.g. edit files it does not own"]}]
}

RULES
- At least {min_tasks} tasks and at most {max_tasks}. Each task is 10-25 minutes of work for one AI agent. weight is 1 (small), 2 or 3 (large).
- files_owned: relative paths/globs. They must be DISJOINT across all tasks (two tasks never edit the same file) and must not overlap contract_files.
  Include tests, docs and the README as their own owned files so work stays balanced.
- depends_on: only when a task truly needs another task's CODE merged first. Prefer contract-first design so most tasks start immediately in parallel.
- Keep contract files compact (about 60 lines in total) and task descriptions to 2-4 sentences each: the whole reply must stay short.
- The project must be small enough to finish in the available time, run locally without paid services, and be verifiable by test_cmds (use only tools
  that are commonly installed: python, node/npm). The tests must really exercise the code, not just print OK.
- INTEGRATION (most failures come from here): the contract states the exact file and module layout (package names, which module exports which public functions and classes,
  the entry point and how it is started). Every module that another task imports is owned by exactly ONE task and appears in the contract. One task owns the wiring
  (app entry point, package __init__, static hosting) and imports ONLY what the contract lists. Never reference a module that no task owns. Dependencies live in one
  requirements/package file owned by one task and listed in setup_cmds.
- Every task has "must" and "must_not" lists so each agent knows exactly what is in and out of scope. They follow ONE plan and ONE contract; no agent invents interfaces.
- Where the lead attached competition context (event rules, judging criteria, links, documents), the plan must serve it: judging criteria shape the scope and the README.
- Where a teammate has skills installed, name the ones that fit in the task description (e.g. "use the test-driven-development skill"). Only name skills listed for that team.
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
7. The lead may have attached competition context (rules, judging criteria, documents, images): it is in `.kode/context/` in your working folder, start with INDEX.md.
8. If your agent has skills or plugins installed (testing, code review, security review, frontend design, debugging), use the ones that fit your task: TASK.md lists the relevant ones. Prefer them to improvising.
9. You are one long-lived session that may be given several tasks one after another: finish each task completely, then wait for the next one; never start other sessions or sub-agents unless the task needs them.
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
    do_block = ""
    if t.get("must"):
        do_block += "## You must" + chr(10) + chr(10).join("- " + x for x in t["must"]) + chr(10) + chr(10)
    if t.get("must_not"):
        do_block += "## You must not" + chr(10) + chr(10).join("- " + x for x in t["must_not"]) + chr(10) + chr(10)
    if p.get("context"):
        do_block += "## Competition context (in .kode/context/, read INDEX.md first)" + chr(10) + ctx.summary(p, 2500) + chr(10) + chr(10)
    skill_block = ("## Skills installed on this laptop (use the ones that fit; invoke them instead of improvising)" + chr(10) + chr(10).join(f"- {s['name']}: {s['description']}" for s in use) + chr(10) * 2) if use else ""
    return f"""# Task {t['id']}: {t['title']}

Project: {p['name']}
Brief: {p['brief']}

## What to build
{t['description']}

## Acceptance
{t.get('acceptance') or 'It does what the description says and works with the contract.'}

{do_block}## Your scope (the ONLY files you may create or edit)
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

    def __init__(self, engine: "ProjectEngine", provider: str, node_id: str, session: str = ""):
        self.engine, self.provider, self.node_id, self.session = engine, provider, node_id, session

    async def complete(self, system: str, user: str, *, max_tokens: int = 0):
        from llm import LLMResult
        prompt = system + chr(10) * 2 + user + chr(10) * 2 + "Reply with the JSON object only, no commentary and no code fences."
        sid = str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, "kode-planner-" + self.session)) if self.session else ""
        again = bool(sid) and self.engine.psess.get(self.session, False)
        r = await self.engine.hub.node_op(self.node_id, "cli_complete", 900.0, tool=self.provider, cmd_timeout=840, prompt=prompt,
                                          **({"session_id": sid, "resume": again} if sid else {}))
        if not r.get("ok") and sid and again and ("session" in str(r.get("err", "")).lower() or "conversation" in str(r.get("err", "")).lower()):
            r = await self.engine.hub.node_op(self.node_id, "cli_complete", 900.0, tool=self.provider, cmd_timeout=840, prompt=prompt,
                                              session_id=str(__import__("uuid").uuid4()), resume=False)             # the saved conversation is gone: start a new one
        if not r.get("ok"):
            raise RuntimeError(f"planner ({self.provider} CLI): {r.get('err', 'failed')}")
        if sid:
            self.engine.psess[self.session] = True
        return LLMResult(r["text"], 0, 0, "stop")


class Metered:
    """Wraps a model client and records what every call costs (estimated from text length when the provider reports no tokens)."""

    def __init__(self, inner, ledger, purpose: str):
        self.inner, self.ledger, self.purpose = inner, ledger, purpose

    async def complete(self, system: str, user: str, *, max_tokens: int = 0):
        t0 = time.time()
        res = await self.inner.complete(system, user, max_tokens=max_tokens)
        self.ledger(self.purpose, res, system, user, time.time() - t0)
        return res


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
        self.pending_ctx: Dict[str, list] = {}                # context items sent with project_create, ingested when planning starts (never stored raw)
        self.psess: Dict[str, bool] = {}                      # project id -> the planner's Claude session already exists

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
        key = f"{p.get('status')}/{p.get('phase')}"
        tl = p.setdefault("timeline", [])
        if not tl or tl[-1]["k"] != key:
            now = time.time()
            if tl and not tl[-1]["end"]:
                tl[-1]["end"] = now
            tl.append({"k": key, "status": p.get("status"), "phase": p.get("phase"), "start": now, "end": 0})
            del tl[:-120]
        self.hub.store.save_project(p["id"], p)
        self.hub.push_projects()

    def public(self, p: dict) -> dict:
        q = {k: v for k, v in p.items() if k not in ("log", "plan", "cards", "context", "ctx_sent", "msess")}
        q["context"] = ctx.public(p)
        plan = p.get("plan") or {}
        q["plan"] = {"summary": plan.get("summary", ""), "stack": plan.get("stack", ""), "test_cmds": plan.get("test_cmds", []), "setup_cmds": plan.get("setup_cmds", []),
                     "contract_files": sorted((plan.get("contract_files") or {}).keys()), "contract_preview": {k: clip(v, 1800) for k, v in list((plan.get("contract_files") or {}).items())[:4]}}
        q["log"] = p.get("log", [])[-120:]
        q["cards"] = p.get("cards", [])
        return q

    def public_all(self) -> dict:
        mem = self.memory()
        return {"projects": [self.public(p) for p in sorted(self.items.values(), key=lambda x: -x.get("created", 0))], "presets": self.jget("presets", []),
                "memory": {"hackathons": mem.get("hackathons", [])[-8:], "agents": mem.get("agents", {})}}

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

    def cfg(self, key: str, default):
        try:
            raw = self.hub.store.get_setting("proj." + key)
        except Exception:
            raw = ""
        if raw in ("", None):
            return default
        try:
            return type(default)(float(raw)) if isinstance(default, (int, float)) and not isinstance(default, bool) else (raw.lower() in ("1", "true", "yes") if isinstance(default, bool) else raw)
        except (TypeError, ValueError):
            return default

    def jget(self, key: str, default):
        try:
            raw = self.hub.store.get_setting(key)
            return json.loads(raw) if raw else default
        except Exception:
            return default

    def jset(self, key: str, value) -> None:
        self.hub.store.set_setting(key, json.dumps(value))

    def memory(self) -> dict:
        return self.jget("memory", {"hackathons": [], "agents": {}})

    def ledger(self, p: dict, purpose: str, res, system: str, user: str, secs: float) -> None:
        u = p.setdefault("usage", {}).setdefault(purpose, {"calls": 0, "in": 0, "out": 0, "secs": 0.0})
        u["calls"] += 1
        u["in"] += int(getattr(res, "prompt_tokens", 0) or 0) or (len(system) + len(user)) // 4
        u["out"] += int(getattr(res, "completion_tokens", 0) or 0) or len(res.text) // 4
        u["secs"] = round(u["secs"] + secs, 1)

    def metered(self, p: dict, client, purpose: str):
        return Metered(client, lambda *a: self.ledger(p, *a), purpose)

    def now(self, p: dict, text: str, sub: str = "") -> None:
        """One plain sentence for the console: what the Brain is doing right now (the console adds a ticking timer)."""
        p["now"] = {"text": text, "sub": sub, "since": time.time()}

    def steps(self, p: dict, items: list) -> None:
        """The checklist the console shows while the Brain works: what is happening now and what comes next."""
        p["steps"] = [{"id": i, "title": t, "detail": d, "state": "pending", "started": 0, "ended": 0} for i, t, d in items]
        self.save(p)

    def step(self, p: dict, sid: str, state: str, detail: Optional[str] = None) -> None:
        st = next((x for x in p.get("steps", []) if x["id"] == sid), None)
        if not st:
            return
        if state == "working" and not st["started"]:
            st["started"] = time.time()
        if state in ("done", "failed", "skipped"):
            st["ended"] = time.time()
        st["state"] = state
        if detail is not None:
            st["detail"] = detail
        self.save(p)

    async def ingest_context(self, p: dict, items: list) -> None:
        for it in items[:ctx.MAX_ITEMS]:
            try:
                if it.get("url"):
                    self.log(p, "context", f"reading {clip(it['url'], 80)}")
                    await ctx.add_link(p, it["url"])
                elif it.get("b64"):
                    ctx.add_file(p, it.get("name", "file"), it.get("mime", ""), it["b64"])
                elif it.get("text"):
                    ctx.add_note(p, it["text"], it.get("name", "Note"))
            except ctx.ContextError as e:
                self.log(p, "warn", f"context item skipped: {e}")
            except Exception as e:
                self.log(p, "warn", f"context item skipped: {type(e).__name__}: {clip(str(e), 120)}")
        self.save(p)

    CLARIFY_SYSTEM = """You are the planning lead of a hackathon team. Before writing the plan, decide whether you need answers from the team lead to build what they actually want.
Ask ONLY questions whose answers would change what gets built (target users, must-have vs nice-to-have, required tech or APIs, demo format, deadline, judging priorities).
Skip anything the brief or the attached context already answers. At most 5 questions; if the brief is clear, ask none. Offer 2-4 likely options when it helps, and a sensible default.
Reply with ONLY one JSON object: {"questions": [{"id": "q1", "q": "the question", "why": "what it changes", "options": ["a", "b"], "default": "a"}]}"""

    async def clarify(self, p: dict) -> list:
        self.step(p, "clarify", "working", "The planner is reading your brief and attached material to decide what it needs to ask you")
        user = f"PROJECT: {p['name']}{chr(10)}BRIEF:{chr(10)}{p['brief']}{chr(10)}"
        if ctx.summary(p):
            user += f"{chr(10)}ATTACHED CONTEXT:{chr(10)}{ctx.summary(p)}{chr(10)}"
        try:
            res = await asyncio.wait_for(self.client(p, "questions").complete(self.CLARIFY_SYSTEM, user, max_tokens=1500), 300)
            qs = extract_json(res.text).get("questions") or []
        except Exception as e:
            self.log(p, "warn", f"the planner could not prepare questions ({type(e).__name__}); planning without them")
            self.step(p, "clarify", "skipped", "No questions; planning directly")
            return []
        out = []
        for i, q in enumerate(qs[:5]):
            if isinstance(q, dict) and str(q.get("q", "")).strip():
                out.append({"id": str(q.get("id") or f"q{i + 1}")[:8], "q": clip(str(q["q"]), 300), "why": clip(str(q.get("why", "")), 200),
                            "options": [clip(str(o), 80) for o in (q.get("options") or [])][:4] if isinstance(q.get("options"), list) else [], "default": clip(str(q.get("default", "")), 120)})
        self.step(p, "clarify", "done", f"{len(out)} question(s) for you" if out else "Nothing to ask: the brief is clear")
        return out

    def client(self, p: Optional[dict] = None, purpose: str = "plan"):
        """The model that writes the plan and diagnoses failures, exactly as the lead chose it for this project:
        a teammate's own CLI login (Claude Code / Codex), one of the saved API profiles, or the Brain's default. Plans are long, so a slow model
        gets far more time than a chat turn."""
        if p is None:
            return self._client(p)
        return self.metered(p, self._client(p), purpose)

    def _client(self, p: Optional[dict] = None):
        if self.llm_override is not None:
            return self.llm_override
        from hub.orchestrator import ProfileClient
        from llm import RetryingLLM
        pl = (p or {}).get("planner") or {}
        if pl.get("kind") == "cli":
            return CliClient(self, pl["provider"], pl.get("node_id", ""), session=(p or {}).get("id", "") if pl["provider"] == "claude" else "")
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

    def member_client(self, p: dict, m: dict, purpose: str = "ideas"):
        return self.metered(p, self._member_client(p, m), purpose)

    def _member_client(self, p: dict, m: dict):
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
            elif t == "project_cancel" and p and p["status"] in ("running", "paused", "planning", "plan_ready", "questions", "review"):
                await self.cancel(p)
                await res(ok=True)
            elif t == "project_delete" and p and p["status"] not in ("running", "planning"):
                self.items.pop(pid, None)
                self.hub.store.delete_project(pid)
                self.hub.push_projects()
                await res(ok=True)
            elif t == "project_context_add" and p and p["status"] not in ("cancelled",):
                try:
                    if m.get("url"):
                        await ctx.add_link(p, str(m["url"]))
                    elif m.get("b64"):
                        ctx.add_file(p, str(m.get("name", "file")), str(m.get("mime", "")), m["b64"])
                    elif str(m.get("text", "")).strip():
                        ctx.add_note(p, str(m["text"]), str(m.get("name", "Note")))
                    else:
                        raise ctx.ContextError("nothing to add")
                    p.get("ctx_sent", {}).clear()                              # laptops get the new material with their next task
                    self.log(p, "context", "context added")
                    await res(ok=True)
                except ctx.ContextError as e:
                    await res(ok=False, err=str(e))
            elif t == "project_context_remove" and p:
                ctx.remove(p, str(m.get("item", "")))
                p.get("ctx_sent", {}).clear()
                self.save(p)
                await res(ok=True)
            elif t == "project_answers" and p and p["status"] == "questions":
                ans = m.get("answers") if isinstance(m.get("answers"), dict) else {}
                p["answers"] = [] if m.get("skip") else [{"q": q["q"], "a": clip(str(ans.get(q["id"]) or q.get("default") or "your call"), 400)} for q in p.get("questions", [])]
                p["questions"] = []
                self.log(p, "plan", "the lead answered the planner's questions" if p["answers"] else "the lead let the planner decide")
                self.start_planning(p)
                await res(ok=True)
            elif t == "project_feedback" and p and p["status"] == "review":
                if m.get("approve"):
                    p["note"] = "Finishing up..."
                    self._spawn(p, self.net_retry(p, "final report", lambda: self.finalize(p)))
                    await res(ok=True)
                elif str(m.get("text", "")).strip():
                    p["status"], p["phase"], p["note"] = "running", "build", "Turning your feedback into tasks..."
                    self._spawn(p, self.rework(p, clip(str(m["text"]).strip(), 2000), [str(x) for x in (m.get("tasks") or [])][:12]))
                    await res(ok=True)
                else:
                    await res(ok=False, err="write what should change, or approve")
            elif t == "project_complete" and p and p["status"] == "done" and not p.get("completed"):
                self.complete_hackathon(p)
                await res(ok=True)
            elif t == "project_task" and p and str(m.get("action")) in ("retry", "skip", "switch", "ask"):
                tk = next((x for x in p["tasks"] if x["id"] == m.get("task")), None)
                if tk and tk["status"] in ("running", "starting", "pending", "blocked"):
                    await self.answer_card(p, {"id": "", "kind": "stalled", "task": tk["id"], "sid": tk.get("sid", "")}, str(m["action"]))
                    await res(ok=True)
                else:
                    await res(ok=False, err="that task is not in progress")
            elif t == "project_stats" and p and p.get("coordinator") and p.get("repo"):
                st = await self.op(p["coordinator"], "log_stats", 60, dir=self.cdir(p), base=p["base"], since=p.get("base_sha", ""))
                by = {a["email"].lower(): a for a in st.get("authors", [])}
                await res(ok=True, members=[{"member": mm["name"], "times": by.get((mm.get("git_email") or "").lower(), {}).get("times", []), "commits": by.get((mm.get("git_email") or "").lower(), {}).get("commits", 0),
                                              "added": by.get((mm.get("git_email") or "").lower(), {}).get("added", 0)} for mm in p["members"]])
            elif t == "project_handoff" and p:
                self.write_handoff(p)
                await res(ok=True, name=f"{slug(p['name'], 30)}-context.md", text=self.build_handoff(p))
            elif t == "preset_save":
                name = clip(str(m.get("name", "")).strip(), 50)
                if not name:
                    await res(ok=False, err="give the preset a name")
                else:
                    pres = [x for x in self.jget("presets", []) if x.get("name") != name]
                    pres.append({"name": name, "members": [{"member": clip(str(x.get("member", "")), 60), "provider": str(x.get("provider", ""))[:20], "pool": [str(a)[:20] for a in (x.get("pool") or [])][:8], "profile_id": str(x.get("profile_id", ""))[:40]} for x in (m.get("members") or [])][:12],
                                 "planner": m.get("planner") if isinstance(m.get("planner"), dict) else {}, "permissions": m.get("permissions", "turbo"), "backup": [str(b)[:20] for b in (m.get("backup") or [])][:5],
                                 "review": m.get("review", "human"), "ask_questions": bool(m.get("ask_questions")), "hackathon": bool(m.get("hackathon"))})
                    self.jset("presets", pres[-20:])
                    self.hub.push_projects()
                    await res(ok=True)
            elif t == "preset_delete":
                self.jset("presets", [x for x in self.jget("presets", []) if x.get("name") != m.get("name")])
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
            pool = [str(a) for a in (cfg.get("pool") or []) if any(pr.get("id") == a and pr.get("available") for pr in n.providers)][:8]
            members.append({"node_id": n.id, "name": n.member_name or n.name, "provider": prov, "profile_id": str(cfg.get("profile_id", "")), "model": str(cfg.get("model", "")),
                            "pool": list(dict.fromkeys([prov] + pool))})
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
             "hackathon": bool(m.get("hackathon")), "judge_round": 0,
             "ask_questions": bool(m.get("ask_questions")), "review": "auto" if m.get("review") == "auto" else "human",
             "backup": [str(b) for b in (m.get("backup") or []) if re.fullmatch(r"[a-z0-9-]{1,20}", str(b))][:5], "context": [], "usage": {}, "sessions": []}
        if isinstance(m.get("context"), list) and m["context"]:
            self.pending_ctx[pid] = m["context"][:ctx.MAX_ITEMS]
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
        """Everything that happens before the build, with a live checklist: context, ideas (hackathon mode), questions, then the plan itself."""
        pend = self.pending_ctx.pop(p["id"], None)
        hack = bool(p.get("hackathon")) and not (p.get("ideation") or {}).get("winner")
        ask = bool(p.get("ask_questions")) and not p.get("questions_done")
        items = []
        if pend or p.get("context"):
            items.append(("context", "Reading the attached context", "Links are fetched, PDFs and documents are turned into text so the planner and every agent can use them"))
        if hack:
            items.append(("ideate", "Ideas, critique and vote", "Every agent researches the problem and proposes ideas, they critique and answer each other anonymously, then each votes (never for its own idea)"))
        if ask:
            items.append(("clarify", "Questions for you", "The planner asks only what would change what gets built. You can answer or let it decide"))
        items += [("plan", "Writing the plan and the contract", "Tasks with exact files, a frozen interface contract and what each task must and must not do"),
                  ("check", "Checking the plan", "File ownership must not overlap, dependencies must not loop, there must be tests to verify the result")]
        self.steps(p, items)
        if pend or p.get("context"):
            self.step(p, "context", "working")
            if pend:
                await self.ingest_context(p, pend)
            self.step(p, "context", "done", f"{len(p.get('context') or [])} item(s) ready")
        if hack:
            self.step(p, "ideate", "working")
            await ideate.run(self, p)
            self.step(p, "ideate", "done", "Winner: " + (p.get("ideation") or {}).get("winner_title", ""))
            p["phase"], p["note"] = "plan", "Drafting the plan..."
            self.save(p)
        if ask:
            qs = await self.clarify(p)
            p["questions_done"] = True
            if qs:
                p["questions"], p["status"], p["phase"] = qs, "questions", "plan"
                p["note"] = "The planner has a few questions. Answer them (or let it decide) and planning continues."
                self.notify(p, "info", "The planner has questions for you before it writes the plan.")
                self.save(p)
                return
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
        client = self.client(p, "plan")
        pre = await asyncio.gather(*(self.op(m["node_id"], "preflight", 60) for m in p["members"]))
        for m, r in zip(p["members"], pre):
            if r.get("ok"):
                m["skills"] = [x for x in (r.get("skills") or []) if isinstance(x, dict) and x.get("name")][:120]
        roster = chr(10).join(f"- {m['name']} (agent: {m['provider']}; installed skills: " + (", ".join(x["name"] for x in sorted(m.get("skills") or [], key=lambda s: (s["name"] not in ALWAYS_SKILLS, s["name"]))[:30]) or "none") + ")" for m in p["members"])
        self.step(p, "plan", "working", f"{self.planner_label(p)} is writing the plan")
        system = PLAN_SYSTEM.replace("{min_tasks}", str(max(n * 2, 3))).replace("{max_tasks}", str(min(MAX_TASKS, n * 6 + 2))).replace("{n}", str(n))
        user = f"PROJECT: {p['name']}\nBRIEF:\n{p['brief']}\n\nTEAM:\n{roster}\n"
        if ctx.summary(p):
            user += f"\nATTACHED COMPETITION CONTEXT (from the lead):\n{ctx.summary(p)}\n"
        if p.get("answers"):
            user += "\nTHE LEAD ANSWERED YOUR QUESTIONS:\n" + chr(10).join(f"- {a['q']} -> {a['a']}" for a in p["answers"]) + "\n"
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
            self.step(p, "plan", "failed")
            raise PlanError("the planner could not produce a valid plan after 3 attempts; adjust the brief and re-plan")
        self.step(p, "plan", "done", f"{len(plan['tasks'])} tasks")
        self.step(p, "check", "working")
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
        self.step(p, "check", "done", "Plan is consistent")
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
        for i in range(5):
            await asyncio.sleep(2.5 if i == 0 else 5.0)
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
            res = await asyncio.wait_for(self.client(p, "supervisor").complete(SUP_SYSTEM, f"AGENT: {s.label}\nTASK: {t['id']} {t['title']}\nCONTEXT: {why or 'screen looks static'}\n\nTERMINAL (latest text, spaces may be missing):\n{tail[-1800:]}", max_tokens=300), 75)
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
        await asyncio.sleep(0.25)
        await self.hub.lead_input(self.hub.orch.actor, s, "\r", label="project")

    def agent_busy(self, s) -> bool:
        """True while the agent is visibly working (spinner, 'esc to interrupt') or printed something a moment ago."""
        return bool(s and s.state == "live" and (time.time() - s.last_out < 2.5 or BUSY_RX.search(self.screen(s, 500))))

    def last_line(self, s) -> str:
        for ln in reversed(self.screen(s, 900).splitlines()):
            ln = ln.strip(" \u2502|>\u203a\u276f")
            if len(ln) >= 6 and not re.fullmatch(r"[\W_]+", ln):
                return clip(ln, 140)
        return ""

    async def send_prompt(self, s, text: str, key: str, wait: float = 0.0) -> bool:
        """Type a message into an agent only when it is idle, and never while a copy of it still sits unsent or queued in its input box.
        (Typing into a busy agent queues the message; typing the same reminder again then sends it twice and costs tokens.)"""
        end = time.time() + wait
        while self.agent_busy(s):
            if time.time() >= end:
                return False
            await asyncio.sleep(1.0)
        if re.sub(r"\s+", "", key).lower() in re.sub(r"\s+", "", self.screen(s, 700)).lower():
            return False                                                     # already on screen: waiting to be read, do not repeat it
        await self.type_line(s, text)
        return True

    # ───────────── choosing who does what ─────────────
    def member_stats(self, p: dict) -> dict:
        out = {}
        for m in p["members"]:
            mine = [t for t in p["tasks"] if t["owner"] == m["node_id"]]
            done = [t for t in mine if t["status"] == "merged" and t.get("started") and t.get("done_at") and not t.get("skipped")]
            secs = [t["done_at"] - t["started"] for t in done]
            closed = [t for t in mine if t["status"] in ("merged", "blocked")]
            bad = [t for t in closed if t.get("restarts") or t.get("attempts") or t.get("violations") or t.get("skipped") or t["status"] == "blocked"]
            prior = (self.memory().get("agents") or {}).get(m["provider"]) or {}
            prec = (1 - len(bad) / len(closed)) if closed else ((prior["clean"] / prior["tasks"]) if prior.get("tasks", 0) >= 3 else None)      # past hackathons are the prior until this one has history
            out[m["node_id"]] = {"avg": sum(secs) / len(secs) if secs else (prior.get("secs", 0) / prior["tasks"] if prior.get("tasks", 0) >= 3 and prior.get("secs") else None), "precision": prec,
                                 "load": sum(t["weight"] for t in mine if t["status"] not in ("merged", "cancelled")), "done": len(done)}
        return out

    def pick_owner(self, p: dict, prefer: str = "", exclude=()) -> tuple:
        """The Brain's choice of who gets a task, from facts: how precise (little rework) and how fast each laptop has been, and how busy it is now."""
        st = self.member_stats(p)
        cands = [m for m in p["members"] if m["node_id"] not in exclude and self.node_ok(m["node_id"])] or [m for m in p["members"] if m["node_id"] not in exclude] or p["members"]
        avgs = [v["avg"] for v in st.values() if v["avg"]]
        fastest = min(avgs) if avgs else None

        def score(m):
            v = st[m["node_id"]]
            prec = v["precision"] if v["precision"] is not None else 0.8
            speed = (fastest / v["avg"]) if (fastest and v["avg"]) else 0.7
            return 0.55 * prec + 0.30 * speed - 0.08 * v["load"] + (0.08 if m["node_id"] == prefer else 0.0)
        best = max(cands, key=score)
        v = st[best["node_id"]]
        why = (f"{best['name']}: " + (f"{int(v['precision'] * 100)}% first-time-right" if v["precision"] is not None else "no history yet") +
               (f", {max(1, int(v['avg'] // 60))} min per task" if v["avg"] else "") + f", {v['load']} open task weight")
        return best["node_id"], why

    def backups_for(self, p: dict, m: dict, tried: list) -> list:
        """Other agents on the same laptop that can take over, in the order the lead chose (project backup list), then the built-in order."""
        n = self.hub.nodes.get(m["node_id"])
        have = {x.get("id"): x for x in (n.providers if n else []) if x.get("available") and x.get("signed_in") is not False}
        order = list(p.get("backup") or []) + list(BACKUP_ORDER)
        allowed = set(m.get("pool") or []) or set(have)
        return [x for x in dict.fromkeys(order) if x in have and x not in tried and x not in ("shell", "demo-agent") and x in (allowed | set(p.get("backup") or []))]

    async def reset_task(self, p: dict, t: dict, why: str) -> None:
        """Retry: stop the agent, keep the branch and its commits, and start a fresh agent on it."""
        o = self.hub.sessions.get(t.get("sid", ""))
        if o and o.state in ("live", "starting"):
            await self.hub.stop_session(o.sid, "orchestrator")
        ms = (p.get("msess") or {}).get(t["owner"])
        if ms and ms.get("sid") == t.get("sid"):
            ms["tasks"] = self.cfg("reuse", MAX_REUSE)                                         # never reuse a session that was just stopped for trouble
        t.update({"status": "pending", "sid": "", "restarts": t.get("restarts", 0) + 1, "nudges": 0, "attempts": 0, "last_progress": time.time(), "nudge_at": time.time(), "retry_at": 0})
        p["cards"] = [c for c in p.get("cards", []) if c.get("task") != t["id"]]
        self.log(p, "task", f"{t['id']}: restarting with a fresh agent ({why})", t["id"])
        self.save(p)

    async def skip_task(self, p: dict, t: dict) -> None:
        o = self.hub.sessions.get(t.get("sid", ""))
        if o and o.state in ("live", "starting"):
            await self.hub.stop_session(o.sid, "orchestrator")
        t["status"], t["skipped"], t["done_at"] = "merged", True, time.time()
        p["cards"] = [c for c in p.get("cards", []) if c.get("task") != t["id"]]
        self.log(p, "task", f"{t['id']}: skipped by the lead; dependants may proceed", t["id"])
        self.save(p)

    async def failover(self, p: dict, t: dict, m: dict, why: str) -> bool:
        """The agent is out of quota or broken: switch this task to another agent on the same laptop, else to another teammate. True if switched."""
        cur = t.get("provider") or m["provider"]
        tried = t.setdefault("tried", [cur])
        if cur not in tried:
            tried.append(cur)
        nxt = next(iter(self.backups_for(p, m, tried)), "")
        if nxt:
            t["provider"], t["switched"] = nxt, True
            tried.append(nxt)
            self.log(p, "failover", f"{t['id']}: {cur} {why}; continuing with {nxt} on {m['name']}'s laptop (same branch, work kept)", t["id"])
            self.notify(p, "warn", f"{cur} on {m['name']}'s laptop {why}. Switched {t['id']} to {nxt}.")
            await self.reset_task(p, t, f"{cur} {why}")
            return True
        others = [x for x in p["members"] if x["node_id"] != t["owner"] and self.node_ok(x["node_id"])]
        if others and t.get("handoffs", 0) < 2:
            nid, reason = self.pick_owner(p, exclude={t["owner"]})
            t["handoffs"] = t.get("handoffs", 0) + 1
            t["owner"], t["provider"], t["tried"], t["switched"] = nid, None, [], True
            self.log(p, "failover", f"{t['id']}: no other agent available on {m['name']}'s laptop; handing the task to {self.node_name(nid)} ({reason})", t["id"])
            self.notify(p, "warn", f"{cur} {why} and has no backup. {t['id']} moved to {self.node_name(nid)}.")
            await self.reset_task(p, t, f"{cur} {why}")
            return True
        if not any(c.get("task") == t["id"] and c["kind"] == "blocked" for c in p.get("cards", [])):
            self.card(p, "blocked", f"{cur} {why} on {m['name']}'s laptop and no other agent could take {t['id']} ({t['title']}). Add another signed-in agent, or press Retry later.", task=t["id"], sid=t.get("sid", ""))
        return False

    def notify(self, p: dict, level: str, text: str) -> None:
        """A message the lead should see even if the tab is in the background (the console shows it as a toast, a desktop notification and a webhook ping)."""
        fn = getattr(self.hub, "notice", None)
        if fn:
            fn(level, f"[{p['name']}] {text}", p["id"])

    STUCK_SYSTEM = """You are the tech lead supervising AI coding agents that build one project. A task has made no progress for a while.
Choose the cheapest action that can unblock it. Reply with ONLY one JSON object:
{"action": "nudge" | "restart" | "switch_agent" | "reassign" | "accept" | "wait" | "ask_human", "message": "...", "to": "teammate name", "reason": "one sentence"}
nudge = type a short precise instruction into the running agent (message required, under 300 characters, say exactly what is missing).
restart = stop it and start a fresh agent of the same kind on the same branch. switch_agent = another agent on the same laptop takes over (broken, looping, rate limited).
reassign = another teammate takes the task ("to" required). accept = merge what is committed (only if the work is essentially complete).
wait = it is probably just slow. ask_human = only if nothing above can help."""

    async def decide_stuck(self, p: dict, t: dict, force: bool = False) -> None:
        """The planner decides what to do about a stuck task, with the task, its git state, the agent's screen and each teammate's track record in front of it."""
        m = self.member(p, t["owner"])
        s = self.hub.sessions.get(t.get("sid", ""))
        t["decisions"], t["decided_at"] = t.get("decisions", 0) + 1, time.time()
        gs = await self.op(m["node_id"], "git_status", 60, dir=self.mdir(p, m), base=p["base"], branch=t["branch"])
        st = self.member_stats(p)
        nl = chr(10)
        team = nl.join(f"- {x['name']} ({x['provider']}): " + (f"{int((st[x['node_id']]['precision'] or 0) * 100)}% first-time-right, " if st[x['node_id']]['precision'] is not None else "") +
                       f"{st[x['node_id']]['load']} open, {'online' if self.node_ok(x['node_id']) else 'OFFLINE'}" for x in p["members"])
        recent = nl.join(l["text"] for l in p.get("log", []) if l.get("task") == t["id"])[-900:]
        quiet_min = int((time.time() - t.get("last_progress", time.time())) / 60)
        user = (f"TASK {t['id']}: {t['title']}" + nl + t["description"][:600] + nl + f"Owner: {m['name']} using {t.get('provider') or m['provider']}; restarts so far {t.get('restarts', 0)}; reminders sent {t['nudges']}" + nl +
                f"Quiet for {quiet_min} minutes. Commits {gs.get('ahead', '?')}, uncommitted changes {gs.get('dirty', '?')}, pushed {gs.get('pushed', '?')}." + nl * 2 +
                "AGENT SCREEN (tail):" + nl + (self.screen(s, 1100) if s else "(no session)") + nl * 2 + "WHAT HAPPENED ON THIS TASK:" + nl + recent + nl * 2 + "TEAM:" + nl + team + nl)
        try:
            res = await asyncio.wait_for(self.client(p, "decisions").complete(self.STUCK_SYSTEM, user, max_tokens=500), 240)
            d = extract_json(res.text)
        except Exception as e:
            d = {"action": "restart" if t.get("restarts", 0) < 2 else "ask_human", "reason": f"the planner could not be asked ({type(e).__name__})"}
        act = str(d.get("action", "wait"))
        why = clip(str(d.get("reason", "")), 200)
        self.log(p, "decision", f"{t['id']}: planner decided '{act}'. {why}", t["id"])
        if act == "nudge" and s and s.state == "live" and d.get("message"):
            if await self.send_prompt(s, clip(str(d["message"]), 300), clip(str(d["message"]), 24), wait=30):
                asyncio.create_task(self.ensure_submitted(s, clip(str(d["message"]), 24)))
        elif act == "restart":
            await self.reset_task(p, t, "planner decision")
        elif act == "switch_agent":
            await self.failover(p, t, m, "was judged stuck by the planner")
        elif act == "reassign":
            target = next((x for x in p["members"] if x["name"].lower() == str(d.get("to", "")).lower() and x["node_id"] != t["owner"]), None)
            nid = target["node_id"] if target else self.pick_owner(p, exclude={t["owner"]})[0]
            t["owner"], t["provider"], t["tried"] = nid, None, []
            await self.reset_task(p, t, f"planner moved it to {self.node_name(nid)}")
        elif act == "accept" and gs.get("ok") and gs.get("ahead", 0) >= 1:
            await self.complete_task(p, t, m, self.mdir(p, m), gs)
        elif act == "ask_human" or force:
            if not any(c.get("task") == t["id"] and c["kind"] == "stalled" for c in p.get("cards", [])):
                self.card(p, "stalled", f"Task {t['id']} ({t['title']}) is stuck. Planner's view: {why or 'it could not find a safe fix'}", task=t["id"], sid=t.get("sid", ""))
        self.save(p)
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
                p["now"] = None
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
                elif p["round"] >= self.cfg("max_fix_rounds", MAX_FIX_ROUNDS):
                    p["status"], p["phase"] = "failed", "done"
                    p["error"] = f"verification still fails after {self.cfg("max_fix_rounds", MAX_FIX_ROUNDS)} fix rounds"
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
                if p.get("review") == "auto":
                    await self.net_retry(p, "final report", lambda: self.finalize(p))
                else:
                    await self.net_retry(p, "review report", lambda: self.prepare_review(p))
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
        self.now(p, "Checking git, the GitHub CLI and the agents on every laptop", "Nothing is built yet: this makes sure every laptop can clone, commit and push")
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
        self.now(p, "Creating the repository and giving every teammate push access", "Teammates get an invitation that their laptop accepts automatically")
        if not p["repo"]:
            r = await self.must(p, cn, "repo_create", 120, forge=p["forge"], repo_name=p["repo_name"], visibility=p["visibility"], path=f"kode/{p['slug']}/remote.git", base=p["base"], description=clip(p["brief"], 120))
            p["repo"] = r["repo"]
            self.log(p, "git", f"created {p['visibility']} repository {p['repo']}" if p["forge"] == "github" else f"created local repository {p['repo']}")
        if p["forge"] == "github":
            await self.net_retry(p, "repository access", lambda: self.grant_access(p))
        r = await self.must(p, cn, "git_prepare", 180, dir=self.cdir(p), repo=p["repo"], base=p["base"], forge=p["forge"])
        p["base_sha"] = r["head"]
        self.now(p, "Freezing the contract and pushing the plan", "From now on nobody may change the shared interface")
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
        prov = t.get("provider") or m["provider"]                         # a task can run on a backup agent after a failover
        t["agent"] = prov
        branch = f"kode/{t['id'].lower()}-{slug(t['title'], 24)}" + (f"-r{t['round']}" if t.get("round") else "")
        t["branch"] = branch
        self.log(p, "task", f"{t['id']} '{t['title']}' -> {m['name']} ({prov}) on branch {branch}", t["id"])
        await self.must(p, nid, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], branch=branch, forge=p["forge"], resume=bool(t.get("attempts") or t.get("restarts")))
        if p.get("context"):
            sig = ",".join(x["id"] for x in p["context"])
            if p.setdefault("ctx_sent", {}).get(nid) != sig:
                await self.must(p, nid, "write_context", 120, dir=d, files=ctx.files_payload(p))
                p["ctx_sent"][nid] = sig
        await self.must(p, nid, "write_task", 30, dir=d, fname="TASK.md", content=render_task(p, t, branch, p.get("contract_paths", []), m.get("skills")))
        from hub.apiprofiles import project_args
        ms = p.setdefault("msess", {}).get(nid) or {}
        cur = self.hub.sessions.get(ms.get("sid", ""))
        again = bool(t.get("attempts") or t.get("restarts"))             # a retried or restarted task gets a fresh agent; a stuck one is not reused
        reuse = cur if (cur and cur.state == "live" and not again and ms.get("tasks", 0) < self.cfg("reuse", MAX_REUSE) and ms.get("dir") == d and ms.get("provider") == prov) else None
        if reuse is None:
            stopped = False
            for sid0 in {t.get("sid", ""), ms.get("sid", "")} - {""}:
                o = self.hub.sessions.get(sid0)
                if o and o.state in ("live", "starting"):
                    await self.hub.stop_session(o.sid, "orchestrator")                 # never two agents in one working folder
                    stopped = True
            if stopped:
                await asyncio.sleep(1.0)
            sid = await self.hub.start_session(nid, prov, d, "orchestrator", profile_id=m.get("profile_id", "") if prov == m["provider"] else "", model=m.get("model", "") if prov == m["provider"] else "",
                                               extra_args=project_args(prov, p["permissions"] in ("auto", "turbo"), p["permissions"] == "turbo", resume=bool(t.get("restarts")) and not t.pop("switched", False)))
            if not sid:
                raise RuntimeError(f"could not start {prov} on {m['name']}'s laptop")
            t["sid"] = sid
            s = self.hub.sessions[sid]
            for _ in range(120):                                              # wait for the session to come up and the TUI banner to settle
                await asyncio.sleep(0.5)
                if s.state in ("error", "exited", "offline"):
                    raise RuntimeError(f"{prov} failed to start: {s.error or s.state}")
                if s.state == "live" and time.time() - s.last_out > 1.5 and s.bytes_out > 0:
                    break
            await self.settle_prompts(p, t, s)
            p["msess"][nid] = {"sid": sid, "tasks": 0, "dir": d, "provider": prov}
            p.setdefault("sessions", []).append({"sid": sid, "node": nid, "member": m["name"], "agent": prov, "dir": d, "first_task": t["id"], "started": time.time()})
            first = f"You are a teammate on project {p['name']}. Read .kode/TASK.md and CLAUDE.md, do exactly what the task says, use your relevant skills, commit sensibly, push your branch, then print KODE_DONE and the number of commits."
        else:
            s, sid = reuse, reuse.sid
            t["sid"] = sid
            first = f"Next task for you: {t['id']} ({t['title']}). .kode/TASK.md was just replaced and the branch switched. Read it again, do exactly what it says (use your relevant skills), commit sensibly, push your branch, then print KODE_DONE and the number of commits."
            self.log(p, "task", f"{t['id']}: reusing {m['name']}'s running agent session (task {ms.get('tasks', 0) + 1} of at most {self.cfg("reuse", MAX_REUSE)} for it)", t["id"])
        p["msess"][nid]["tasks"] += 1
        t["mark0"] = t["prompt_mark"] = s.bytes_out                         # only text printed after this point counts as this task's progress
        t["dlg_sig"] = ""
        if reuse is not None:
            if not await self.send_prompt(s, first, f"Next task for you: {t['id']}", wait=45):   # still busy: never queue a second message in its input box
                p["msess"][nid]["tasks"] = self.cfg("reuse", MAX_REUSE)
                await self.hub.stop_session(s.sid, "orchestrator")
                await asyncio.sleep(1.0)
                return await self.start_task(p, t)
        else:
            await self.type_line(s, first)
        asyncio.create_task(self.ensure_submitted(s, "Read .kode/TASK.md" if reuse is None else f"Next task for you: {t['id']}"))
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
        elif c["kind"] in ("blocked", "stalled", "setup", "prompt") and action in ("retry", "skip", "switch", "ask"):
            tk = next((x for x in p["tasks"] if x["id"] == c.get("task")), None)
            if tk and action == "retry":
                tk["tried"], tk["provider"] = [], None
                await self.reset_task(p, tk, "retried by the lead")
            elif tk and action == "skip":
                await self.skip_task(p, tk)                                  # the lead accepts the gap; dependants may proceed
            elif tk and action == "switch":
                await self.failover(p, tk, self.member(p, tk["owner"]), "was switched by the lead")
            elif tk and action == "ask":
                self.drop_card(p, c["id"])
                await self.decide_stuck(p, tk, force=True)
                return
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
        busy = bool(s and not dead and self.agent_busy(s))
        limit = bool(s and not dead and not busy and not marker and idle >= 20 and LIMIT_RX.search(self.fresh(s, t.get("mark0", 0), 700)))
        apierr = bool(s and not dead and not busy and not marker and idle >= 90 and API_ERR.search(self.screen(s, 500)))
        state = "ended" if dead else "limit" if limit else "api_error" if apierr else "working" if (busy or idle < 20) else "idle"
        t["activity"] = {"state": state, "idle": int(idle) if idle < 999 else 0, "line": self.last_line(s) if (s and not dead) else "", "ts": int(time.time()), "agent": t.get("agent") or m["provider"]}
        if limit or apierr:
            if await self.failover(p, t, m, "hit its usage or rate limit" if limit else "keeps failing with API errors"):
                return
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
        if (idle >= NUDGE_IDLE_S or quiet >= NUDGE_QUIET_S * 2) and quiet >= NUDGE_QUIET_S and (gs["dirty"] or not gs["pushed"] or gs["ahead"] < t["min_commits"]) and t["nudges"] < 3 and time.time() - t.get("nudge_at", 0) > 90 and s and not dead:
            need = []
            if gs["dirty"]:
                need.append("commit your uncommitted changes")
            if gs["ahead"] < 1:
                need.append("commit your work (nothing is committed yet)")
            if not gs["pushed"]:
                need.append(f"push with: git push -u origin {t['branch']}")
            key = f"Status check for {t['id']}"
            if await self.send_prompt(s, f"{key} on branch {t['branch']}: {'; '.join(need) or 'finish the task'}. Then print KODE_DONE <number of commits>.", key):
                t["nudges"] += 1
                t["nudge_at"] = time.time()
                asyncio.create_task(self.ensure_submitted(s, key))
                self.log(p, "nudge", f"{t['id']}: reminded the agent ({'; '.join(need)})", t["id"])
            else:
                t["nudge_at"] = time.time() - 60                              # busy or the reminder is already waiting in its input box: look again in 30 s, never stack them
        elif quiet > self.cfg("stall_min", 8) * 30 and t["nudges"] >= 2 and t.get("decisions", 0) < 3 and time.time() - t.get("decided_at", 0) > 300 and not dead:
            await self.decide_stuck(p, t)
        elif quiet > self.cfg("stall_min", 8) * 60 and (t["nudges"] >= 3 or t.get("decisions", 0) >= 3) and not any(c.get("task") == t["id"] and c["kind"] == "stalled" for c in p["cards"]):
            self.card(p, "stalled", f"Task {t['id']} ({t['title']}) has made no progress for {int(quiet / 60)} minutes. Retry starts a fresh agent on the same branch, Skip accepts the gap, or ask the planner to decide.", task=t["id"], sid=t["sid"])

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
            if not (more and ms.get("sid") == t["sid"] and ms.get("tasks", 0) < self.cfg("reuse", MAX_REUSE) and p["status"] == "running"):
                await self.hub.stop_session(t["sid"], "orchestrator")          # nothing more for this teammate: end the session
                self.retire(t["sid"])
        self.log(p, "merge", f"{t['id']} merged into {p['base']} ({gs['ahead']} commits by {m['name']})", t["id"])

    # ───────────── verify / fix / finish ─────────────
    async def verify(self, p: dict) -> bool:
        await self.ensure_coordinator(p)
        cn, d = p["coordinator"], self.cdir(p)
        await self.must(p, cn, "git_prepare", 180, dir=d, repo=p["repo"], base=p["base"], forge=p["forge"])
        results = []
        cmds = p["plan"].get("setup_cmds", []) + p["plan"]["test_cmds"]
        for i, cmd in enumerate(cmds):
            self.now(p, f"Running `{clip(cmd, 90)}` on the merged code", f"Check {i + 1} of {len(cmds)}. If it fails, the planner diagnoses it and fix tasks are given out")
            self.save(p)
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
        self.now(p, "The planner is diagnosing why verification failed", "It reads the failing output and creates the smallest fix tasks")
        self.log(p, "phase", f"verification failed: triage round {p['round']} of {self.cfg("max_fix_rounds", MAX_FIX_ROUNDS)}")
        failing = [x for x in p["verify"]["results"] if x["code"] != 0][-1]
        files = await self.op(p["coordinator"], "files", 30, dir=self.cdir(p))
        owners = "\n".join(f"- {t['id']} ({t['title']}): {', '.join(t['files_owned'])}" for t in p["tasks"] if t.get("round", 0) == 0)
        user = (f"PROJECT: {p['name']}\nFAILED COMMAND: {failing['cmd']} (exit {failing['code']})\nOUTPUT (tail):\n{failing['out'][-3500:]}\n\n"
                f"TASK AREAS:\n{owners}\n\nFILES IN REPO:\n" + "\n".join((files.get('files') or [])[:150]))
        client = self.client(p, "triage")
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
            owner, why_owner = self.pick_owner(p, prefer=orig["owner"] if orig else "")
            self.log(p, "decision", f"fix task {i + 1}: given to {why_owner}")
            load[owner] = load.get(owner, 0) + 1
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
        self.now(p, "Reviewing code quality, the README and repository hygiene", "A reviewer model checks the result against your brief; findings become polish tasks")
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
            res = await asyncio.wait_for(self.client(p, "review").complete(REVIEW_SYSTEM, user, max_tokens=2500), 420)
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
        if p.get("polish_round", 0) >= self.cfg("polish_rounds", MAX_POLISH_ROUNDS):
            self.log(p, "quality", f"quality review: {len(keep)} remaining note(s) after {self.cfg("polish_rounds", MAX_POLISH_ROUNDS)} polish rounds: " + "; ".join(k["title"] for k in keep[:4]))
            self.save(p)
            return True
        p["polish_round"] = p.get("polish_round", 0) + 1
        self.log(p, "quality", f"quality review round {p['polish_round']}: {len(keep)} improvement task(s): " + "; ".join(k["title"] for k in keep[:4]))
        self.add_fix_tasks(p, keep[:5], f"Q{p['polish_round']}", 100 + p["polish_round"])
        return False

    def add_fix_tasks(self, p: dict, items: List[dict], prefix: str, rnd: int) -> None:
        """Improvement tasks go to whoever has the least work so far (the lead's PC included), each on its own disjoint files."""
        for i, it in enumerate(items):
            owner, why_owner = self.pick_owner(p)
            self.log(p, "decision", f"{prefix}-{i + 1}: given to {why_owner}")
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
                out.append((f"{m['provider']}" + (f" / {m['profile_id']}" if m.get("profile_id") else " (own login)"), self.member_client(p, m, "judge")))
        if not out:
            out.append((self.planner_label(p), self.client(p, "judge")))
        return out[:3]

    async def judge_review(self, p: dict, rejudge: bool = False) -> bool:
        """Score the finished repository. True = done (scored, or judging unavailable); False = improvement tasks were added and the build must run again."""
        self.now(p, "Independent judges are scoring the finished project", "Each judge scores alone and blind; the median per criterion counts")
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
        if rejudge or card["overall"] >= self.cfg("judge_pass", jd.JUDGE_PASS) or p.get("judge_round", 0) >= 1:
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
        self.log(p, "judge", f"below {self.cfg("judge_pass", jd.JUDGE_PASS)}: one improvement round with {len(items[:4])} task(s) from the judges' findings")
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
        self.now(p, "Writing the submission text and demo script", "docs/SUBMISSION.md for the hackathon form")
        try:
            await self.ensure_coordinator(p)
            cn, d = p["coordinator"], self.cdir(p)
            a = await self.must(p, cn, "repo_audit", 120, dir=d)
            j = p.get("judge") or {}
            facts = (f"PROJECT: {p['name']}\nPROBLEM / BRIEF:\n{p.get('problem') or p['brief']}\n\nPLAN SUMMARY: {(p.get('plan') or {}).get('summary', '')}\nSTACK: {(p.get('plan') or {}).get('stack', '')}\n"
                     f"CHOSEN IDEA: {(p.get('ideation') or {}).get('winner_title', '-')}\n\nREADME:\n{a.get('readme', '')[:3500]}\n\nFILES:\n" + "\n".join((a.get("files") or [])[:80])
                     + (f"\n\nSELF-ASSESSMENT (automatic judge): {j.get('overall')}/100; strengths: {'; '.join(j.get('strengths', []))}" if j.get("overall") else ""))
            res = await asyncio.wait_for(self.client(p, "submission").complete(self.SUBMISSION_SYSTEM, facts, max_tokens=2500), 420)
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

    # ───────────── human review, rework, finalisation ─────────────
    async def prepare_review(self, p: dict) -> None:
        """Verify, polish and judge are done. Show the lead what was built, what was tested and how it scored, and wait for a yes or for changes."""
        await self.close_sessions(p)
        self.write_handoff(p)
        j = p.get("judge") or {}
        q = p.get("quality") or {}
        tasks = [{"id": t["id"], "title": t["title"], "owner": self.node_name(t["owner"]), "agent": t.get("agent", ""), "pr": t.get("pr_url", ""), "commits": t.get("commits", 0),
                  "minutes": round((t.get("done_at", 0) - t.get("started", 0)) / 60, 1) if t.get("done_at") and t.get("started") else 0, "skipped": bool(t.get("skipped")), "files": t.get("changed", [])[:6]} for t in p["tasks"]]
        v = p.get("verify") or {}
        p["report"] = {"ts": time.time(), "tasks": tasks, "tests": [{"cmd": r["cmd"], "code": r["code"], "tail": str(r.get("out", ""))[-300:]} for r in v.get("results", [])], "tests_passed": bool(v.get("passed")),
                       "judge": {"overall": j.get("overall"), "grade": j.get("grade")} if j.get("overall") is not None else None, "quality_open": q.get("issues", []),
                       "fix_rounds": p["round"], "rework_rounds": p.get("rework_round", 0), "repo": p["repo"], "visibility": p.get("visibility"),
                       "how_to_run": clip(((p.get("plan") or {}).get("stack") or ""), 300),
                       "limits": ["Tests and the judge ran against the repository on the coordinator's laptop. The app was not opened in a browser and no screenshots were taken."]}
        p["status"], p["phase"], p["note"] = "review", "review", "Your turn: read the report, then approve it or ask for changes."
        self.log(p, "phase", "ready for your review")
        self.notify(p, "ok", "Verify, polish and judge are done. The report is ready for your review.")
        self.save(p)

    REWORK_SYSTEM = """You are the tech lead. The team lead reviewed the finished project and asked for changes. Turn the request into the smallest set of implementation tasks.
Reply with ONLY one JSON object:
{"summary": "1-2 sentences", "tasks": [{"title": "...", "description": "exactly what to change and why, quoting the request", "files_owned": ["paths or globs the task may edit"],
  "must": ["short"], "must_not": ["short"], "owner_task": "<id of the original task whose area this is, or empty>"}]}
Rules: 1-6 tasks, files_owned disjoint between tasks, never contract files, include tests for changed behaviour, no unrelated refactors."""

    async def rework(self, p: dict, text: str, ids: list) -> None:
        p["rework_round"] = p.get("rework_round", 0) + 1
        n = p["rework_round"]
        await self.ensure_coordinator(p)
        files = await self.op(p["coordinator"], "files", 30, dir=self.cdir(p))
        areas = chr(10).join(f"- {t['id']} ({t['title']}): {', '.join(t['files_owned'])}" for t in p["tasks"] if not t.get("round"))
        sel = chr(10).join(f"- {t['id']} {t['title']}" for t in p["tasks"] if t["id"] in ids)
        user = (f"PROJECT: {p['name']}{chr(10)}BRIEF:{chr(10)}{p.get('problem') or p['brief']}{chr(10)}{chr(10)}THE LEAD'S REQUEST:{chr(10)}{text}{chr(10)}" +
                (f"{chr(10)}THE LEAD POINTED AT THESE TASKS:{chr(10)}{sel}{chr(10)}" if sel else "") + f"{chr(10)}TASK AREAS:{chr(10)}{areas}{chr(10)}{chr(10)}FILES IN REPO:{chr(10)}" + chr(10).join((files.get("files") or [])[:150]))
        client, feedback, plan = self.client(p, "rework"), "", None
        for _ in range(3):
            res = await client.complete(self.REWORK_SYSTEM, user + feedback, max_tokens=3000)
            try:
                obj = extract_json(res.text)
                items = obj.get("tasks")
                if not isinstance(items, list) or not (1 <= len(items) <= 6):
                    raise PlanError("'tasks' must be 1..6 items")
                for it in items:
                    if not it.get("files_owned") or any(not _safe_rel(str(g).replace("*", "x")) for g in it["files_owned"]):
                        raise PlanError("each task needs safe relative files_owned")
                    if any(globs_overlap(g, c) for g in it["files_owned"] for c in p["contract_paths"]):
                        raise PlanError("tasks must not edit contract files")
                for i, a in enumerate(items):
                    for b in items[i + 1:]:
                        if any(globs_overlap(x, y) for x in a["files_owned"] for y in b["files_owned"]):
                            raise PlanError("tasks must have disjoint files_owned")
                plan = obj
                break
            except (LLMOutputError, PlanError, ValueError, TypeError, AttributeError) as e:
                feedback = f"{chr(10)}{chr(10)}Your previous reply was rejected: {e}. Reply with ONLY the JSON object."
        if plan is None:
            raise PlanError("the planner could not turn the request into valid tasks; rephrase it and try again")
        self.log(p, "decision", "rework plan: " + clip(str(plan.get("summary", "")), 300))
        for i, it in enumerate(plan["tasks"]):
            orig = next((t for t in p["tasks"] if t["id"] == it.get("owner_task")), None)
            owner, why = self.pick_owner(p, prefer=orig["owner"] if orig else "")
            self.log(p, "decision", f"R{n}-{i + 1}: given to {why}")
            p["tasks"].append({"id": f"R{n}-{i + 1}", "title": clip(str(it.get("title", "change")), 120), "description": clip(str(it.get("description", "")), 3000) + chr(10) * 2 + "The lead's request: " + clip(text, 800),
                               "acceptance": "The lead's request is satisfied and the verification commands still pass.", "files_owned": [str(g) for g in it["files_owned"]], "depends_on": [], "weight": 1,
                               "must": [clip(str(x), 200) for x in (it.get("must") or [])][:5], "must_not": [clip(str(x), 200) for x in (it.get("must_not") or [])][:5],
                               "owner": owner, "min_commits": 1, "status": "pending", "sid": "", "branch": "", "pr_url": "", "commits": 0, "round": 300 + n, "attempts": 0, "violations": 0, "nudges": 0, "log": []})
        p["round"], p["polish_round"], p["judge_round"] = 0, 0, 0                                # a fresh budget for the verify, polish and judge that follow
        p["report"] = None
        self.start(p)
        p["phase"] = "build"
        self.save(p)

    async def finalize(self, p: dict) -> None:
        """The lead approved. The build scaffolding (CLAUDE.md, AGENTS.md, PLAN.md) leaves the repository; the full context goes to a handoff file instead."""
        self.now(p, "Cleaning up the repository and closing the project", "The build scaffolding is removed; the full context goes to the handoff file")
        self.write_handoff(p)
        await self.ensure_coordinator(p)
        if p["forge"] == "github" or p["forge"] == "local":
            r = await self.op(p["coordinator"], "remove_files", 120, dir=self.cdir(p), base=p["base"], files=["CLAUDE.md", "AGENTS.md", "PLAN.md"], message="chore: remove build scaffolding (K.O.D.E.)")
            self.log(p, "git", "removed CLAUDE.md, AGENTS.md and PLAN.md from the repository (the context is in the handoff file)" if r.get("ok") else f"could not remove the build scaffolding: {clip(r.get('err', ''), 160)}")
        p["report"] = p.get("report")
        await self.finish(p)
        self.notify(p, "ok", "Project approved and finished.")

    def handoff_path(self, p: dict):
        d = ctx.DATA / re.sub(r"[^A-Za-z0-9_-]", "", p["id"])
        d.mkdir(parents=True, exist_ok=True)
        return d / "CONTEXT.md"

    def build_handoff(self, p: dict) -> str:
        """One file that lets any AI (or person) pick the project up: what it is, where the code is, how it was decided, what is done and what is not, and which sessions did it."""
        nl = chr(10)
        pl = p.get("plan") or {}
        j = p.get("judge") or {}
        out = [f"# {p['name']}: project context for hand-off", "",
               "Give this file to any AI coding assistant. It describes the project, where the code is, the agreed contract, what has been done and how to continue.", "",
               "## How to continue", f"1. Clone the repository: `git clone https://github.com/{p['repo']}.git`" if p["forge"] == "github" else f"1. Clone the repository: `git clone {p['repo']}`",
               f"2. Install: `{'` and `'.join(pl.get('setup_cmds') or ['(nothing to install)'])}`   Verify: `{'` and `'.join(pl.get('test_cmds') or [])}`",
               "3. Read README.md, then the contract below. The contract is the agreed interface: change it only deliberately and update every user of it.",
               "4. Work on a branch, commit in sensible steps, run the verify command before you push.", "",
               "## The problem and the decision", p.get("problem") or p["brief"], ""]
        if (p.get("ideation") or {}).get("winner_title"):
            ide = p["ideation"]
            out += [f"Chosen idea (agent vote): **{ide['winner_title']}**", "Ballots: " + "; ".join(f"{v['voter']} voted {v['vote']}" for v in ide.get("votes", [])), ""]
        if p.get("answers"):
            out += ["## Answers the lead gave"] + [f"- {a['q']} -> {a['a']}" for a in p["answers"]] + [""]
        if p.get("context"):
            out += ["## Attached competition context", ctx.summary(p, 6000), ""]
        out += ["## Stack", pl.get("stack", ""), "", "## Summary", pl.get("summary", ""), "", "## Frozen contract (the interface everyone built against)"]
        for path, body in (pl.get("contract_files") or {}).items():
            out += [f"### {path}", "```", str(body), "```"]
        out += ["", "## Tasks", "| Task | Title | Owner | Agent | Status | PR |", "|---|---|---|---|---|---|"]
        for t in p["tasks"]:
            out.append(f"| {t['id']} | {t['title']} | {self.node_name(t['owner'])} | {t.get('agent', '')} | {'skipped' if t.get('skipped') else t['status']} | {t.get('pr_url', '')} |")
        out += ["", "## Decisions and incidents (newest last)"]
        out += [f"- {l['text']}" for l in p.get("log", []) if l.get("kind") in ("decision", "failover", "triage", "judge", "quality", "plan", "ideate", "git")][-60:]
        v = p.get("verify") or {}
        out += ["", "## Verification", f"Passed: {v.get('passed')}"] + [f"- `{r['cmd']}` exit {r['code']}" for r in v.get("results", [])]
        if j.get("overall") is not None:
            out += ["", f"## Code judge: {j['overall']}/100 (grade {j.get('grade')})"] + [f"- {c['label']}: {c['score']}/10" for c in j.get("criteria", [])] + ["", "Improvements suggested:"] + [f"- {i['title']}" for i in j.get("improvements", [])]
        skipped = [t for t in p["tasks"] if t.get("skipped")]
        if skipped:
            out += ["", "## Known gaps (tasks that were skipped)"] + [f"- {t['id']} {t['title']}" for t in skipped]
        out += ["", "## Team and agent sessions (to revisit a session, open that agent in the folder shown)", "| Teammate | Agent | Session | Folder on their laptop | First task |", "|---|---|---|---|---|"]
        for s in p.get("sessions", []):
            out.append(f"| {s['member']} | {s['agent']} | {s['sid']} | {s['dir']} | {s['first_task']} |")
        out += ["", "## Brain usage (planner, supervisor, judge calls; tokens are estimates where the provider reports none)", "| Purpose | Calls | Tokens in | Tokens out |", "|---|---|---|---|"]
        for k, u in (p.get("usage") or {}).items():
            out.append(f"| {k} | {u['calls']} | {u['in']} | {u['out']} |")
        out += ["", "## Conventions used while building", render_rules(p)]
        return nl.join(out) + nl

    def write_handoff(self, p: dict) -> None:
        try:
            self.handoff_path(p).write_text(self.build_handoff(p), encoding="utf-8")
        except Exception as e:
            self.log(p, "warn", f"could not write the handoff file: {clip(str(e), 120)}")

    def complete_hackathon(self, p: dict) -> None:
        """The lead closes the event: what this team and these agents achieved is remembered and used as the starting point for the next one."""
        mem = self.memory()
        done = [t for t in p["tasks"] if t["status"] == "merged" and not t.get("skipped")]
        for t in done:
            a = mem.setdefault("agents", {}).setdefault(t.get("agent") or self.member(p, t["owner"])["provider"], {"tasks": 0, "clean": 0, "secs": 0})
            a["tasks"] += 1
            a["clean"] += 0 if (t.get("restarts") or t.get("attempts") or t.get("violations")) else 1
            a["secs"] += int((t.get("done_at", 0) - t.get("started", 0)) or 0)
        mem.setdefault("hackathons", []).append({"name": p["name"], "id": p["id"], "ts": int(time.time()), "repo": p.get("repo", ""), "team": [{"name": m["name"], "agent": m["provider"]} for m in p["members"]],
                                                  "score": (p.get("judge") or {}).get("overall"), "tasks": len(done), "idea": (p.get("ideation") or {}).get("winner_title", "")})
        mem["hackathons"] = mem["hackathons"][-30:]
        self.jset("memory", mem)
        p["completed"] = int(time.time())
        self.write_handoff(p)
        self.log(p, "done", "hackathon marked complete; the team's agents and results are remembered for the next one")
        self.save(p)

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
                        "tasks": len(tasks), "merged": sum(1 for t in tasks if t["status"] == "merged"), "violations": sum(t.get("violations", 0) for t in tasks), "times": a.get("times", [])})
        p["final"] = {"balance": bal, "passed": not failed, "rounds": p["round"], "finished": time.time(), "quality": p.get("quality"),
                      "judge": ({k: v for k, v in (p.get("judge") or {}).items() if k in ("overall", "grade")} or None)}
        if not failed:
            p["status"], p["phase"], p["note"] = "done", "done", "Project complete: all tasks merged and the verification commands pass."
            self.log(p, "done", "project complete; verification passed")
        self.save(p)
