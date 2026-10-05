"""Git / GitHub-CLI / command operations the Brain can ask a node to run on ITS machine, inside the member's workspace roots.

Why here: commits and pushes must come from each teammate's own laptop, under their own authenticated `gh` / git identity,
so authorship (and the hackathon scoreboard) is real. The hub never holds a GitHub token.

forge = "github": clone / PR / merge go through the `gh` CLI.   forge = "local": plain git against a path remote (tests, offline demos).
Every directory is resolved inside the node's workspace roots; every command is an argv list (no shell) except `run`, which the lead
uses to run the project's own test commands.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TAIL = 6000
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}      # CREATE_NO_WINDOW: helper processes must never pop up a console of their own
STRAY = re.compile(r"(^|/)(__pycache__|node_modules|\.DS_Store|Thumbs\.db|\.kode|\.kode-planner|\.token-optimizer|\.idea|\.vscode)(/|$)|\.(log|tmp|bak|orig|swp|pyc)$|(^|/)\.env(\.|$)|(^|/)(scratch|tmp|temp)[^/]*(/|$)", re.I)
SECRET_RX = [re.compile(p) for p in (r"AKIA[0-9A-Z]{16}", r"\bgh[opsur]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}", r"\bsk-[A-Za-z0-9]{32,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                                         r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]")]
TODO_RX = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b|lorem ipsum|placeholder", re.I)
EXCLUDE = ".kode/"
LOCAL_IGNORES = [".kode/", ".kode-planner/", ".token-optimizer/"]


class OpError(Exception):
    pass


def tail(s: str, n: int = TAIL) -> str:
    s = s or ""
    return s if len(s) <= n else "..." + s[-n:]


SKILL_DIRS = (".claude/skills", ".agents/skills", ".codex/skills", ".gemini/antigravity/skills", ".gemini/skills")


def scan_skills(limit: int = 120) -> List[Dict[str, str]]:
    """Skills installed on this laptop (a folder with a SKILL.md): name and one-line description, so the plan can tell each agent which ones to use."""
    out, seen = [], set()
    for rel in SKILL_DIRS:
        base = Path.home() / rel
        try:
            dirs = sorted(x for x in base.iterdir() if x.is_dir()) if base.is_dir() else []
        except OSError:
            continue
        for d in dirs:
            f = d / "SKILL.md"
            if d.name in seen or not f.is_file():
                continue
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            m = re.search(r"^description:\s*(.+)$", head, re.M)
            desc = (m.group(1).strip().strip('"' + chr(39)) if m else "")[:140]
            seen.add(d.name)
            out.append({"name": d.name, "description": desc})
            if len(out) >= limit:
                return out
    return out


def _iter_jsonl(path: Path, tail_bytes: int = 0):
    try:
        with open(path, "rb") as f:
            if tail_bytes:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail_bytes))
                f.readline() if size > tail_bytes else None
            for raw in f:
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue
    except OSError:
        return


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r:
                return r
    return None


def scan_limits() -> Dict[str, Any]:
    import datetime
    out: Dict[str, Any] = {"ts": int(time.time())}
    home = Path.home()
    # Codex: the newest rollout file carries the latest rate_limits block
    root = home / ".codex" / "sessions"
    try:
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if root.is_dir() else []
    except OSError:
        files = []
    for f in files:
        rl = None
        for obj in _iter_jsonl(f, tail_bytes=400_000):
            r = _find(obj, "rate_limits")
            if r:
                rl = r
        if rl:
            def win(w):
                return {"used_percent": w.get("used_percent"), "window_minutes": w.get("window_minutes"), "resets_at": w.get("resets_at")} if isinstance(w, dict) else None
            out["codex"] = {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")), "plan": rl.get("plan_type"), "reached": rl.get("rate_limit_reached_type"), "as_of": int(f.stat().st_mtime)}
            break
    # Claude Code: tokens in rolling windows from its session logs
    root = home / ".claude" / "projects"
    now = time.time()
    t5, t7, msgs5 = {"in": 0, "out": 0, "cache": 0}, {"in": 0, "out": 0, "cache": 0}, 0
    try:
        recent = [p for p in root.rglob("*.jsonl") if p.stat().st_mtime > now - 7 * 86400] if root.is_dir() else []
    except OSError:
        recent = []
    for f in sorted(recent, key=lambda p: p.stat().st_mtime, reverse=True)[:300]:
        for obj in _iter_jsonl(f):
            u = (obj.get("message") or {}).get("usage") if isinstance(obj.get("message"), dict) else None
            if not u:
                continue
            try:
                ts = datetime.datetime.fromisoformat(str(obj.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            age = now - ts
            if age > 7 * 86400:
                continue
            vals = (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0))
            for bucket in ([t7] + ([t5] if age <= 5 * 3600 else [])):
                bucket["in"] += vals[0]
                bucket["out"] += vals[1]
                bucket["cache"] += vals[2]
            if age <= 5 * 3600:
                msgs5 += 1
    if recent:
        out["claude"] = {"last_5h": t5, "last_7d": t7, "messages_5h": msgs5, "note": "Claude Code does not record its plan limits locally; these are tokens used, not a percentage."}
    out["agy"] = {"note": "Antigravity does not expose usage or limits outside its own window."}
    return out


class Ops:
    def __init__(self, roots: List[str], env_extra: Optional[Dict[str, str]] = None):
        self.roots = [Path(r).resolve() for r in roots]
        self.env_extra = dict(env_extra or {})

    # ── plumbing ──
    def env(self) -> Dict[str, str]:
        e = dict(os.environ)
        e.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})
        e.update(self.env_extra)
        return e

    def path(self, want: str, create: bool = False) -> Path:
        base = self.roots[0]
        p = Path(want) if want else base
        p = (base / p) if not p.is_absolute() else p
        p = p.resolve()
        for r in self.roots:
            try:
                p.relative_to(r)
                if create:
                    p.mkdir(parents=True, exist_ok=True)
                return p
            except ValueError:
                continue
        raise OpError("directory is outside this machine's workspace roots")

    def safe_file(self, root: Path, rel: str) -> Path:
        if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or re.match(r"^[A-Za-z]:", rel):
            raise OpError(f"unsafe path: {rel}")
        p = (root / rel).resolve()
        p.relative_to(root.resolve())
        if ".git" in p.relative_to(root.resolve()).parts:
            raise OpError("refusing to write inside .git")
        return p

    async def sh(self, argv: List[str], cwd: Optional[Path] = None, timeout: float = 120, shell: bool = False) -> Dict[str, Any]:
        try:
            if shell:
                proc = await asyncio.create_subprocess_shell(" ".join(argv) if isinstance(argv, list) else argv, cwd=str(cwd) if cwd else None, env=self.env(),
                                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, **NO_WINDOW)
            else:
                exe = shutil.which(argv[0]) or argv[0]
                proc = await asyncio.create_subprocess_exec(exe, *argv[1:], cwd=str(cwd) if cwd else None, env=self.env(),
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, **NO_WINDOW)
        except FileNotFoundError:
            return {"code": 127, "out": f"{argv[0]}: not installed"}
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                if os.name == "nt":                                       # also end children of a shell
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, **NO_WINDOW)
            except Exception:
                pass
            return {"code": 124, "out": f"timed out after {timeout:.0f}s"}
        return {"code": proc.returncode, "out": out.decode("utf-8", "replace")}

    async def git(self, cwd: Path, *args: str, timeout: float = 120) -> Dict[str, Any]:
        return await self.sh(["git", *args], cwd, timeout)

    async def must(self, cwd: Path, *args: str, timeout: float = 120) -> str:
        r = await self.git(cwd, *args, timeout=timeout)
        if r["code"] != 0:
            raise OpError(f"git {' '.join(args[:2])} failed: {tail(r['out'], 800).strip()}")
        return r["out"]

    # ── dispatcher ──
    async def run(self, name: str, a: Dict[str, Any]) -> Dict[str, Any]:
        fn = getattr(self, "op_" + name, None)
        if fn is None:
            return {"ok": False, "err": f"unknown operation '{name}'"}
        try:
            res = await fn(a)
            return {"ok": True, **(res or {})}
        except OpError as e:
            return {"ok": False, "err": str(e)}
        except Exception as e:                                           # never let one op kill the node
            return {"ok": False, "err": f"{type(e).__name__}: {e}"}

    # ── operations ──
    async def op_preflight(self, a):
        git = await self.sh(["git", "--version"])
        gh = await self.sh(["gh", "auth", "status"]) if shutil.which("gh") else {"code": 127, "out": ""}
        user = await self.sh(["git", "config", "--get", "user.name"])
        mail = await self.sh(["git", "config", "--get", "user.email"])
        who = re.search(r"account (\S+)", gh["out"])
        env = self.env_extra
        return {"git": git["code"] == 0, "gh": gh["code"] == 0, "gh_user": who.group(1) if who else "",
                "git_name": env.get("GIT_AUTHOR_NAME") or user["out"].strip(), "git_email": env.get("GIT_AUTHOR_EMAIL") or mail["out"].strip(),
                "skills": scan_skills()}

    async def op_repo_create(self, a):
        """github: `gh repo create <name> --private`; local: a bare repo with one initial commit on `base`."""
        base = a.get("base", "main")
        if a.get("forge") == "local":
            bare = self.path(a["path"], create=True)
            await self.must(bare, "init", "--bare", "-b", base)
            tmp = self.path(a["path"] + "_seed", create=True)
            await self.must(tmp, "init", "-b", base)
            (tmp / "README.md").write_text(f"# {a.get('repo_name', 'project')}\n", encoding="utf-8")
            await self.must(tmp, "add", "-A")
            await self.must(tmp, "-c", "user.name=kode", "-c", "user.email=kode@local", "commit", "-m", "chore: initial commit")
            await self.must(tmp, "remote", "add", "origin", str(bare))
            await self.must(tmp, "push", "-u", "origin", base)
            shutil.rmtree(tmp, ignore_errors=True)
            return {"repo": str(bare)}
        name = a["repo_name"]                                   # "name" (your account) or "org/name"
        if not re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/)?[A-Za-z0-9._-]{1,100}", name):
            raise OpError("repository name may only contain letters, numbers, '.', '-' and '_' (optionally owner/name)")
        vis = "--public" if a.get("visibility") == "public" else "--private"
        r = await self.sh(["gh", "repo", "create", name, vis, "--add-readme", "--description", a.get("description", "K.O.D.E. project")[:300]], self.roots[0], 90)
        if r["code"] != 0:
            raise OpError("gh repo create failed: " + tail(r["out"], 600).strip())
        if "/" in name:
            return {"repo": name}
        who = await self.sh(["gh", "api", "user", "--jq", ".login"])
        return {"repo": f"{who['out'].strip()}/{name}"}

    async def op_collab_add(self, a):
        """Run on the REPOSITORY OWNER's laptop: give a teammate write access (GitHub sends them an invitation)."""
        repo, user = a["repo"], a["user"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", user) or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            raise OpError("bad repository or user name")
        r = await self.sh(["gh", "api", "-X", "PUT", f"repos/{repo}/collaborators/{user}", "-f", "permission=push", "--silent"], self.roots[0], 60)
        if r["code"] != 0:
            raise OpError(f"could not add {user} to {repo} (the account running this needs admin rights on it): " + tail(r["out"], 300).strip())
        return {"invited": user}

    async def op_collab_accept(self, a):
        """Run on the TEAMMATE's laptop with their own gh login: accept the pending invitation to the repository, if there is one."""
        repo = a["repo"]
        r = await self.sh(["gh", "api", "user/repository_invitations", "--jq", ".[] | [.id, .repository.full_name] | @tsv"], self.roots[0], 60)
        if r["code"] != 0:
            raise OpError("could not list repository invitations: " + tail(r["out"], 300).strip())
        accepted = 0
        for line in r["out"].splitlines():
            iid, _, name = line.partition("\t")
            if name.strip().lower() == repo.lower() and iid.strip().isdigit():
                p = await self.sh(["gh", "api", "-X", "PATCH", f"user/repository_invitations/{iid.strip()}", "--silent"], self.roots[0], 60)
                if p["code"] != 0:
                    raise OpError("could not accept the invitation: " + tail(p["out"], 300).strip())
                accepted += 1
        return {"accepted": accepted}

    async def op_repo_access(self, a):
        """Can THIS laptop's gh account push to the repository?"""
        r = await self.sh(["gh", "api", f"repos/{a['repo']}", "--jq", ".permissions.push"], self.roots[0], 60)
        return {"push": r["code"] == 0 and r["out"].strip().lower() == "true", "detail": tail(r["out"], 200).strip()}

    async def op_repo_audit(self, a):
        """Hygiene audit of a checked-out repository: what a reviewer (human or AI) would flag in the first five minutes."""
        d = self.path(a["dir"])
        files = [l for l in (await self.git(d, "ls-files"))["out"].splitlines() if l]
        meta = {"README.md", "CLAUDE.md", "AGENTS.md", "PLAN.md"}
        readme = ""
        for n in ("README.md", "Readme.md", "readme.md", "README"):
            if (d / n).is_file():
                readme = (d / n).read_text(encoding="utf-8", errors="replace")
                break
        stray = [f for f in files if STRAY.search(f.replace("\\", "/"))]
        secrets, todos = [], []
        text_ext = (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh", ".env", ".cfg", ".ini", ".java", ".go", ".rs", ".c", ".cpp", ".h")
        for f in files[:600]:
            if not f.lower().endswith(text_ext) or f in meta or f.startswith("docs/CONTRACT"):
                continue
            try:
                body = (d / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(body.splitlines(), 1):
                if len(secrets) < 8 and any(rx.search(line) for rx in SECRET_RX):
                    secrets.append(f"{f}:{i}")
                if len(todos) < 12 and TODO_RX.search(line) and not f.lower().endswith(".md"):
                    todos.append(f"{f}:{i}: {line.strip()[:80]}")
        big = []
        for f in files[:600]:
            try:
                if (d / f).stat().st_size > 1_000_000:
                    big.append(f)
            except OSError:
                pass
        code_ext = (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".java", ".go", ".rs", ".c", ".cpp")
        sized = []
        for f in files[:600]:
            if f.lower().endswith(code_ext) and f not in meta and not f.startswith(("docs/CONTRACT", "tests/", "test/")):
                try:
                    sized.append(((d / f).stat().st_size, f))
                except OSError:
                    pass
        samples, tests, loc = {}, {}, 0
        for f in files[:600]:
            if f.lower().endswith(code_ext) and f not in meta:
                try:
                    body = (d / f).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                loc += body.count(chr(10)) + 1
                if re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec)\.", f):
                    tests[f] = body
        for _, f in sorted(sized, reverse=True)[:6]:
            samples[f] = (d / f).read_text(encoding="utf-8", errors="replace")[:1800]
        for f in sorted(tests, key=lambda k: -len(tests[k]))[:3]:
            samples[f] = tests[f][:1500]
        low = readme.lower()
        return {"samples": samples, "loc": loc, "test_files": len(tests), "files": files[:400], "readme": readme[:5000], "readme_len": len(readme.strip()),
                "readme_has_run": any(k in low for k in ("run", "usage", "getting started", "install", "quick start")), "readme_has_test": "test" in low,
                "stray": stray[:30], "secrets": secrets, "todos": todos, "big": big[:10],
                "has_tests": any(re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec)\.", f) for f in files),
                "has_gitignore": ".gitignore" in files, "has_license": any(f.upper().startswith("LICENSE") for f in files), "count": len(files)}

    async def op_git_prepare(self, a):
        """Ensure a clone of the repo in `dir`, then check out `branch` created from origin/<base>."""
        d = self.path(a["dir"], create=False)
        base, branch, repo = a.get("base", "main"), a.get("branch", ""), a["repo"]
        if not (d / ".git").exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            if a.get("forge") == "local":
                r = await self.sh(["git", "clone", repo, str(d)], d.parent, 180)
            else:
                r = await self.sh(["gh", "repo", "clone", repo, str(d)], d.parent, 180)
            if r["code"] != 0:
                raise OpError("clone failed: " + tail(r["out"], 600).strip())
        ident = self.env_extra
        if ident.get("GIT_AUTHOR_NAME"):
            await self.must(d, "config", "user.name", ident["GIT_AUTHOR_NAME"])
            await self.must(d, "config", "user.email", ident.get("GIT_AUTHOR_EMAIL", ""))
        ex = d / ".git" / "info" / "exclude"
        have = ex.read_text(encoding="utf-8") if ex.exists() else ""
        missing = [x for x in LOCAL_IGNORES if x not in have]
        if missing:                                               # tool scratch folders (ours, and tools that write into the cwd) never get committed
            ex.parent.mkdir(parents=True, exist_ok=True)
            with open(ex, "a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(missing) + "\n")
        await self.must(d, "fetch", "origin", "--prune", timeout=120)
        if (await self.git(d, "status", "--porcelain"))["out"].strip():          # unfinished work from an earlier session: keep it in git, never lose or block on it
            await self.must(d, "add", "-A")
            await self.git(d, "commit", "-m", "chore: save work in progress before switching task (K.O.D.E.)")
        if branch:
            have = (await self.git(d, "rev-parse", "--verify", f"origin/{branch}"))["code"] == 0
            local = (await self.git(d, "rev-parse", "--verify", branch))["code"] == 0
            if have and a.get("resume"):
                await self.must(d, "checkout", "-B", branch, f"origin/{branch}")
            elif local and a.get("resume"):
                await self.must(d, "checkout", branch)
            else:
                await self.must(d, "checkout", "-B", branch, f"origin/{base}")
        else:
            await self.must(d, "checkout", "-B", base, f"origin/{base}")
        head = (await self.must(d, "rev-parse", "HEAD")).strip()
        return {"path": str(d), "head": head}

    async def op_write_task(self, a):
        d = self.path(a["dir"])
        f = self.safe_file(d / ".kode", a["fname"])
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(a["content"]), encoding="utf-8")
        return {"file": str(f)}

    async def op_write_context(self, a):
        """Copy the competition material (documents, images, notes) into this laptop's working folder: .kode/context/."""
        import base64
        d = self.path(a["dir"])
        out = d / ".kode" / "context"
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for name, b64 in (a.get("files") or {}).items():
            f = self.safe_file(out, re.sub(r"[^A-Za-z0-9._ -]", "_", str(name))[:120])
            f.write_bytes(base64.b64decode(b64))
            n += 1
        return {"files": n}

    async def op_limits(self, a):
        """What each agent's own logs say about usage on this laptop. Codex records its real rate-limit windows (used percent, reset time); Claude Code does not record limits,
        so its token use in the last 5 hours and 7 days is summed from its session logs; Antigravity exposes nothing local. Nothing here calls a model."""
        return await asyncio.to_thread(scan_limits)

    async def op_git_status(self, a):
        d = self.path(a["dir"])
        base, branch = a.get("base", "main"), a.get("branch", "")
        await self.git(d, "fetch", "origin", "--prune", timeout=60)
        head = (await self.git(d, "rev-parse", "HEAD"))["out"].strip()
        cur = (await self.git(d, "rev-parse", "--abbrev-ref", "HEAD"))["out"].strip()
        ahead = int((await self.git(d, "rev-list", "--count", f"origin/{base}..HEAD"))["out"].strip() or 0)
        dirty = [l[3:] for l in (await self.git(d, "status", "--porcelain"))["out"].splitlines() if l.strip()]
        remote = (await self.git(d, "rev-parse", "--verify", f"origin/{branch or cur}"))
        pushed = remote["code"] == 0 and remote["out"].strip() == head
        changed = [l.strip() for l in (await self.git(d, "diff", "--name-only", f"origin/{base}...HEAD"))["out"].splitlines() if l.strip()]
        commits = []
        for l in (await self.git(d, "log", f"origin/{base}..HEAD", "--format=%h\x1f%ae\x1f%s"))["out"].splitlines():
            p = l.split("\x1f")
            if len(p) == 3:
                commits.append({"sha": p[0], "email": p[1], "subject": p[2]})
        added = removed = 0
        for l in (await self.git(d, "diff", "--numstat", f"origin/{base}...HEAD"))["out"].splitlines():
            p = l.split("\t")
            if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
                added, removed = added + int(p[0]), removed + int(p[1])
        behind = int((await self.git(d, "rev-list", "--count", f"HEAD..origin/{base}"))["out"].strip() or 0)
        return {"branch": cur, "head": head, "ahead": ahead, "behind": behind, "dirty": dirty[:30], "pushed": pushed, "changed": changed[:200],
                "commits": commits[:60], "added": added, "removed": removed}

    async def op_git_push(self, a):
        d = self.path(a["dir"])
        branch = a["branch"]
        committed = False
        if a.get("commit_dirty"):
            dirty = (await self.git(d, "status", "--porcelain"))["out"].strip()
            if dirty:
                await self.must(d, "add", "-A")
                r = await self.git(d, "commit", "-m", a.get("message") or "chore: save work in progress")
                committed = r["code"] == 0
        r = await self.git(d, "push", "-u", "origin", branch, *(["--force-with-lease"] if a.get("force") else []), timeout=120)
        if r["code"] != 0:
            raise OpError("push failed: " + tail(r["out"], 700).strip())
        return {"committed": committed}

    async def op_pr_create(self, a):
        d = self.path(a["dir"])
        base, branch = a.get("base", "main"), a["branch"]
        if a.get("forge") == "local":
            return {"url": f"local:{branch}", "number": 0}
        r = await self.sh(["gh", "pr", "create", "--base", base, "--head", branch, "--title", a.get("title", branch)[:200], "--body", a.get("body", "")[:6000]], d, 90)
        if r["code"] != 0:
            v = await self.sh(["gh", "pr", "view", branch, "--json", "url,number"], d, 60)
            if v["code"] == 0:
                j = json.loads(v["out"])
                return {"url": j["url"], "number": j["number"]}
            raise OpError("gh pr create failed: " + tail(r["out"], 600).strip())
        url = next((l.strip() for l in r["out"].splitlines() if l.strip().startswith("http")), "")
        m = re.search(r"/pull/(\d+)", url)
        return {"url": url, "number": int(m.group(1)) if m else 0}

    async def op_pr_merge(self, a):
        """Merge keeps each teammate's own commits (merge commit, no squash), so commit counts stay honest."""
        d = self.path(a["dir"])
        base, branch = a.get("base", "main"), a["branch"]
        if a.get("forge") == "local":
            await self.must(d, "fetch", "origin", "--prune")
            await self.must(d, "checkout", "-B", base, f"origin/{base}")
            r = await self.git(d, "-c", "user.name=kode", "-c", "user.email=kode@local", "merge", "--no-ff", f"origin/{branch}", "-m", f"Merge {branch}")
            if r["code"] != 0:
                await self.git(d, "merge", "--abort")
                raise OpError("merge conflict: " + tail(r["out"], 500).strip())
            await self.must(d, "push", "origin", base)
            return {"merged": True}
        ref = a.get("url") or branch
        r = await self.sh(["gh", "pr", "merge", str(ref), "--merge"], d, 120)
        if r["code"] != 0:
            raise OpError("gh pr merge failed: " + tail(r["out"], 600).strip())
        return {"merged": True}

    async def op_scope_revert(self, a):
        """Undo a branch's changes to files it does not own (or contract files): restore from origin/<base>, or delete files base lacks."""
        d = self.path(a["dir"])
        base = a.get("base", "main")
        if a.get("checkout"):                                                # the coordinator audits the SHARED branch, whatever the owner's clone looks like
            await self.must(d, "fetch", "origin", "--prune")
            await self.must(d, "checkout", "-B", a["branch"], f"origin/{a['branch']}")
        for f in a.get("files") or []:
            self.safe_file(d, f)
            if (await self.git(d, "cat-file", "-e", f"origin/{base}:{f}"))["code"] == 0:
                await self.must(d, "checkout", f"origin/{base}", "--", f)
            else:
                await self.git(d, "rm", "-f", "--ignore-unmatch", "--", f)
        if (await self.git(d, "diff", "--cached", "--quiet"))["code"] != 0:
            await self.must(d, "commit", "-m", a.get("message", "chore: revert out-of-scope changes"))
        await self.must(d, "push", "-u", "origin", a["branch"])
        return {}

    async def op_branch_files(self, a):
        d = self.path(a["dir"])
        base = a.get("base", "main")
        await self.must(d, "fetch", "origin", "--prune")
        out = await self.must(d, "diff", "--name-only", f"origin/{base}...origin/{a['branch']}")
        return {"changed": [l.strip() for l in out.splitlines() if l.strip()]}

    async def op_rebase_push(self, a):
        """Last resort for a conflicted branch: rebase onto origin/<base>; on conflict keep the branch's own side ("theirs" during a rebase)."""
        d = self.path(a["dir"])
        base = a.get("base", "main")
        await self.must(d, "fetch", "origin", "--prune")
        r = await self.git(d, "rebase", f"origin/{base}")
        if r["code"] != 0:
            await self.git(d, "rebase", "--abort")
            r = await self.git(d, "rebase", "-X", "theirs", f"origin/{base}")
            if r["code"] != 0:
                await self.git(d, "rebase", "--abort")
                raise OpError("rebase failed: " + tail(r["out"], 500).strip())
        await self.must(d, "push", "--force-with-lease", "origin", a["branch"])
        return {}

    async def op_write_files(self, a):
        """Bootstrap commit on the base branch (contract, plan, rules). Runs in the coordinator's own clone."""
        d = self.path(a["dir"])
        base = a.get("base", "main")
        await self.must(d, "fetch", "origin", "--prune")
        await self.must(d, "checkout", "-B", base, f"origin/{base}")
        for rel, content in (a.get("files") or {}).items():
            f = self.safe_file(d, rel)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(str(content), encoding="utf-8")
        await self.must(d, "add", "-A")
        r = await self.git(d, "commit", "-m", a.get("message", "chore: add project contract and plan"))
        if r["code"] != 0 and "nothing to commit" not in r["out"]:
            raise OpError("commit failed: " + tail(r["out"], 500).strip())
        await self.must(d, "push", "origin", base)
        return {"head": (await self.must(d, "rev-parse", "HEAD")).strip()}

    async def op_remove_files(self, a):
        """Delete files from the base branch in one commit (the build scaffolding is not part of the finished product)."""
        d = self.path(a["dir"])
        base = a.get("base", "main")
        await self.must(d, "fetch", "origin", "--prune")
        await self.must(d, "checkout", "-B", base, f"origin/{base}")
        names = [self.safe_file(d, rel) and rel for rel in (a.get("files") or [])]
        await self.git(d, "rm", "-q", "-f", "--ignore-unmatch", *names)
        r = await self.git(d, "commit", "-m", a.get("message", "chore: remove build scaffolding"))
        if r["code"] != 0 and "nothing to commit" not in r["out"]:
            raise OpError("commit failed: " + tail(r["out"], 500).strip())
        await self.must(d, "push", "origin", base)
        return {"head": (await self.must(d, "rev-parse", "HEAD")).strip()}

    async def op_run(self, a):
        d = self.path(a["dir"])
        r = await self.sh([a["cmd"]], d, float(a.get("cmd_timeout", 300)), shell=True)
        return {"code": r["code"], "out": tail(r["out"])}

    async def op_files(self, a):
        d = self.path(a["dir"])
        r = await self.git(d, "ls-files")
        return {"files": r["out"].splitlines()[:400]}

    async def op_cli_complete(self, a):
        """Use the teammate's own logged-in CLI (Claude Code / Codex) as a one-shot text model: prompt on stdin, answer on stdout.
        Runs with the same scrubbed environment as that provider's sessions, in a scratch folder inside the workspace root."""
        from bridge.manifests import load_manifests, scrubbed_env
        tool = str(a.get("tool", ""))
        sess = str(a.get("session_id", ""))
        keep = tool == "claude" and re.fullmatch(r"[0-9a-fA-F-]{36}", sess) is not None          # the planner keeps ONE conversation per project: it remembers the plan and every decision
        claude_tail = ["-p", "--resume", sess] if (keep and a.get("resume")) else ["-p", "--session-id", sess] if keep else ["-p", "--no-session-persistence"]
        argv_tail = {"claude": claude_tail, "codex": ["exec", "--ephemeral", "-s", "read-only", "--skip-git-repo-check", "-"]}.get(tool)
        m = load_manifests().get(tool)
        if argv_tail is None or m is None or not m.available:
            raise OpError(f"'{tool}' is not available as a planner on this machine")
        d = self.path(".kode-planner", create=True)
        proc = await asyncio.create_subprocess_exec(*m.argv(), *argv_tail, cwd=str(d), env=scrubbed_env(m), stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(proc.communicate(str(a.get("prompt", "")).encode("utf-8")), float(a.get("cmd_timeout", 600)))
        except asyncio.TimeoutError:
            proc.kill()
            raise OpError(f"{tool} did not answer within {float(a.get('cmd_timeout', 600)):.0f}s")
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not text:
            raise OpError(f"{tool} failed (exit {proc.returncode}): {tail(err.decode('utf-8', 'replace'), 300).strip()}")
        return {"text": text}

    async def op_read_file(self, a):
        d = self.path(a["dir"])
        f = self.safe_file(d, a["fname"])
        return {"content": tail(f.read_text(encoding="utf-8", errors="replace"), int(a.get("max", 6000)))}

    async def op_log_stats(self, a):
        """Commits / lines per author on origin/<base> since `since` (sha) - the commit-balance scoreboard."""
        d = self.path(a["dir"])
        base = a.get("base", "main")
        await self.git(d, "fetch", "origin", "--prune")
        rng = f"{a['since']}..origin/{base}" if a.get("since") else f"origin/{base}"
        out = (await self.git(d, "log", rng, "--no-merges", "--numstat", "--format=@@%ae\x1f%an\x1f%at"))["out"]
        stats: Dict[str, Dict[str, Any]] = {}
        cur = None
        for l in out.splitlines():
            if l.startswith("@@"):
                email, _, rest = l[2:].partition("\x1f")
                name, _, at = rest.partition("\x1f")
                cur = stats.setdefault(email, {"email": email, "name": name, "commits": 0, "added": 0, "removed": 0, "times": []})
                cur["commits"] += 1
                if at.isdigit() and len(cur["times"]) < 600:
                    cur["times"].append(int(at))
            elif cur and "\t" in l:
                p = l.split("\t")
                if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
                    cur["added"] += int(p[0])
                    cur["removed"] += int(p[1])
        return {"authors": list(stats.values())}
