"""Node-side collectors: turn a running provider session into STRUCTURED, REAL data.

  * git      - changed files + diff stat for the session's working directory
  * claude   - ~/.claude/projects/<cwd>/<session>.jsonl  -> TodoWrite plan, current tool, token usage
  * codex    - ~/.codex/sessions/**/rollout-*.jsonl      -> update_plan, token_count usage
Anything a provider doesn't expose is reported as absent/"unknown" - never estimated or invented.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(os.environ.get("KODE_PROVIDER_HOME") or Path.home())


# Context-window sizes when the provider does NOT report one. These are ASSUMPTIONS, shown as such in the UI, and bumped
# upward automatically if a session has already used more than the assumed window (real Claude Code sessions exceed 800k tokens).
ASSUMED_WINDOWS = [("[1m]", 1_000_000), ("-1m", 1_000_000), ("sonnet-5", 1_000_000), ("opus-5", 1_000_000), ("claude", 200_000),
                   ("gpt-5", 400_000), ("gpt-4.1", 1_000_000), ("gemini", 1_000_000), ("deepseek", 128_000), ("qwen", 128_000)]


def context_window(model: str, used: int, reported: int = 0):
    """-> (window_tokens, source) where source is 'reported' | 'assumed' | 'inferred'."""
    if reported:
        return int(reported), "reported"
    m = (model or "").lower()
    win = next((w for key, w in ASSUMED_WINDOWS if key in m), 200_000)
    if used > win:
        return (1_000_000 if used <= 1_000_000 else used), "inferred"
    return win, "assumed"


def _enc_claude(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ───────────────────────── git ─────────────────────────
async def _git(cwd: str, *args: str, timeout: float = 4.0) -> Optional[str]:
    try:
        p = await asyncio.create_subprocess_exec("git", "-C", cwd, *args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode("utf-8", "replace") if p.returncode == 0 else None
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None


async def git_info(cwd: str) -> Dict[str, Any]:
    top = await _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return {"repo": False}
    status = await _git(cwd, "status", "--porcelain=v1", "-uall") or ""
    branch = (await _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    stat = (await _git(cwd, "diff", "--shortstat") or "").strip()
    files = []
    for line in status.splitlines()[:200]:
        if len(line) > 3:
            files.append({"s": line[:2].strip() or "?", "path": line[3:].strip().strip('"')})
    return {"repo": True, "branch": branch, "changed": len(status.splitlines()), "files": files, "stat": stat}


# ───────────────────────── incremental jsonl reader ─────────────────────────
class JsonlTail:
    def __init__(self, path: Path):
        self.path, self.offset = path, 0

    def read_new(self) -> List[dict]:
        out: List[dict] = []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
                nl = data.rfind(b"\n")
                if nl < 0:
                    return out
                self.offset += nl + 1
        except OSError:
            return out
        for line in data[: nl + 1].splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out


def _todo_list(items: Any) -> List[dict]:
    res = []
    for t in items or []:
        if isinstance(t, dict):
            text = t.get("content") or t.get("step") or t.get("activeForm") or ""
            st = (t.get("status") or "pending").replace("in_progress", "doing").replace("inProgress", "doing")
            res.append({"text": str(text)[:200], "status": st if st in ("pending", "doing", "completed") else "pending"})
    return res[:40]


def _tool_summary(name: str, inp: dict) -> str:
    for k in ("file_path", "path", "command", "pattern", "url", "description", "query"):
        v = inp.get(k) if isinstance(inp, dict) else None
        if v:
            return f"{name}: {str(v).splitlines()[0][:110]}"
    return name


class ClaudeCollector:
    provider = "claude"

    def __init__(self, cwd: str, started: float):
        self.cwd, self.started = cwd, started
        self.tail: Optional[JsonlTail] = None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "messages": 0}
        self.todos: List[dict] = []
        self.activity = ""
        self.seen_ids: set = set()
        self.model = ""
        self.ctx_used = 0

    def _find(self) -> None:
        d = HOME / ".claude" / "projects" / _enc_claude(self.cwd)
        if not d.is_dir():
            return
        cands = [p for p in d.glob("*.jsonl") if p.stat().st_mtime >= self.started - 5]
        if cands:
            self.tail = JsonlTail(max(cands, key=lambda p: p.stat().st_mtime))

    def poll(self) -> Dict[str, Any]:
        if self.tail is None:
            self._find()
        if self.tail is None:
            return {}
        for rec in self.tail.read_new():
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
            if rec.get("type") != "assistant" or not msg:
                continue
            mid = msg.get("id") or rec.get("uuid")
            u = msg.get("usage")
            if msg.get("model") and not str(msg["model"]).startswith("<"):
                self.model = str(msg["model"])
            if isinstance(u, dict):                                       # the latest prompt size == how full the context is right now
                self.ctx_used = int(u.get("input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
            if isinstance(u, dict) and mid not in self.seen_ids:          # a message can be logged several times; count once
                self.seen_ids.add(mid)
                self.usage["input_tokens"] += int(u.get("input_tokens", 0) or 0)
                self.usage["output_tokens"] += int(u.get("output_tokens", 0) or 0)
                self.usage["cache_read"] += int(u.get("cache_read_input_tokens", 0) or 0)
                self.usage["cache_write"] += int(u.get("cache_creation_input_tokens", 0) or 0)
                self.usage["messages"] += 1
            for blk in msg.get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    if blk.get("name") == "TodoWrite":
                        self.todos = _todo_list((blk.get("input") or {}).get("todos"))
                    self.activity = _tool_summary(str(blk.get("name")), blk.get("input") or {})
        win, src = context_window(self.model, self.ctx_used)
        return {"todos": self.todos, "activity_text": self.activity,
                "usage": {**self.usage, "source": "claude-transcript", "model": self.model, "context_used": self.ctx_used,
                          "context_window": win, "window_source": src}}


class CodexCollector:
    provider = "codex"

    def __init__(self, cwd: str, started: float):
        self.cwd, self.started = cwd, started
        self.tail: Optional[JsonlTail] = None
        self.usage: Dict[str, Any] = {}
        self.todos: List[dict] = []
        self.activity = ""
        self.model = ""

    def _find(self) -> None:
        root = HOME / ".codex" / "sessions"
        if not root.is_dir():
            return
        best, best_m = None, 0.0
        for p in root.rglob("rollout-*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= self.started - 5 and m > best_m:
                try:
                    first = json.loads(open(p, encoding="utf-8", errors="replace").readline())
                    if os.path.normcase((first.get("payload") or {}).get("cwd", "")) != os.path.normcase(self.cwd):
                        continue
                except Exception:
                    continue
                best, best_m = p, m
        if best:
            self.tail = JsonlTail(best)

    def poll(self) -> Dict[str, Any]:
        if self.tail is None:
            self._find()
        if self.tail is None:
            return {}
        for rec in self.tail.read_new():
            p = rec.get("payload") or {}
            if rec.get("type") == "turn_context" and p.get("model"):
                self.model = str(p["model"])
            if p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                t = p["info"].get("total_token_usage") or {}
                last = p["info"].get("last_token_usage") or {}
                win, src = context_window(self.model, int(last.get("input_tokens", 0) or 0), int(p["info"].get("model_context_window", 0) or 0))
                self.usage = {"input_tokens": int(t.get("input_tokens", 0) or 0), "output_tokens": int(t.get("output_tokens", 0) or 0),
                              "cache_read": int(t.get("cached_input_tokens", 0) or 0), "cache_write": int(t.get("cache_write_input_tokens", 0) or 0),
                              "reasoning_tokens": int(t.get("reasoning_output_tokens", 0) or 0), "source": "codex-transcript",
                              "model": self.model, "context_used": int(last.get("input_tokens", 0) or 0), "context_window": win, "window_source": src}
            if p.get("type") == "function_call":
                name, args = p.get("name", ""), p.get("arguments")
                try:
                    a = json.loads(args) if isinstance(args, str) else (args or {})
                except Exception:
                    a = {}
                if name == "update_plan":
                    self.todos = _todo_list(a.get("plan"))
                self.activity = _tool_summary(name, a if isinstance(a, dict) else {})
        out: Dict[str, Any] = {"todos": self.todos, "activity_text": self.activity}
        if self.usage:
            out["usage"] = self.usage
        return out


COLLECTORS = {"claude": ClaudeCollector, "codex": CodexCollector}


class Activity:
    """Provider-agnostic terminal stats: always available, always honest (it measures bytes, not tokens)."""

    def __init__(self) -> None:
        self.bytes_out = 0
        self.active_s = 0.0
        self._last = 0.0

    def feed(self, n: int) -> None:
        now = time.time()
        self.bytes_out += n
        if self._last and now - self._last < 2.0:
            self.active_s += now - self._last
        self._last = now
