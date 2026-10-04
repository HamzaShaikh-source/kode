"""Isolation enforced in code. The prompt hint is not the guarantee - this module is.

Two checks:
  1. validate_ownership(tasks): no two tasks may own overlapping paths, and no task may own
     paths outside its role's domain / outside the repo. Violation => ScopeViolation.
  2. GuardedWriter / check_write(task, path): every file an agent writes is validated against
     that task's files_owned at write time. Violation => ScopeViolation (+ error log line).
"""
from __future__ import annotations

import logging
import posixpath
from fnmatch import fnmatchcase
from typing import Dict, List

from models import FileWrite, Task

log = logging.getLogger("kode.guard")

# Directory domains each role may ever own. The contracts dir is owned by nobody (frozen).
ROLE_DOMAINS: Dict[str, tuple] = {
    "backend": ("api/", "db/"),
    "frontend": ("web/",),
    "qa": ("tests/",),
}
FROZEN_PREFIXES = ("contracts/",)


class ScopeViolation(Exception):
    """An agent/task touched (or claimed) a file it does not exclusively own."""

    def __init__(self, message: str, *, task_id: str = "", path: str = "", other_task_id: str = ""):
        super().__init__(message)
        self.task_id, self.path, self.other_task_id = task_id, path, other_task_id


def normalize(path: str) -> str:
    """Canonical repo-relative POSIX path; rejects absolute / traversal / backslash tricks.
    A trailing slash (directory ownership) is preserved."""
    if not path or not path.strip():
        raise ScopeViolation("empty path", path=path)
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ScopeViolation(f"absolute path not allowed: {path!r}", path=path)
    is_dir = p.endswith("/")
    norm = posixpath.normpath(p)
    if norm == ".." or norm.startswith("../") or "/../" in norm or norm == ".":
        raise ScopeViolation(f"path escapes repository: {path!r}", path=path)
    return norm + "/" if is_dir else norm


def _is_glob(p: str) -> bool:
    return any(c in p for c in "*?[")


def _covers(pattern: str, path: str) -> bool:
    """Does ownership entry `pattern` cover concrete-or-glob `path`?"""
    if pattern == path:
        return True
    if pattern.endswith("/"):                       # directory ownership
        return path.startswith(pattern)
    return fnmatchcase(path, pattern)


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test between two ownership entries (exact / glob / directory)."""
    a, b = normalize(a), normalize(b)
    return _covers(a, b) or _covers(b, a) or (
        _is_glob(a) and _is_glob(b) and posixpath.dirname(a) == posixpath.dirname(b)
    )


def validate_ownership(tasks: List[Task]) -> None:
    """Reject any task set where ownership is not strictly disjoint or is out of domain."""
    for t in tasks:
        if not t.files_owned:
            raise ScopeViolation(f"task {t.id} ({t.role.value}) declares no files_owned", task_id=t.id)
        domain = ROLE_DOMAINS[t.role.value]
        for raw in t.files_owned:
            f = normalize(raw)
            if f.startswith(FROZEN_PREFIXES):
                raise ScopeViolation(f"{t.role.value} may not own frozen path {f}", task_id=t.id, path=f)
            if not f.startswith(domain):
                raise ScopeViolation(
                    f"{t.role.value} task {t.id} claims {f!r} outside its domain {domain}", task_id=t.id, path=f)
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            for fa in a.files_owned:
                for fb in b.files_owned:
                    if patterns_overlap(fa, fb):
                        msg = (f"overlapping ownership: {a.role.value} task {a.id} claims {fa!r} and "
                               f"{b.role.value} task {b.id} claims {fb!r}")
                        log.error("SCOPE_VIOLATION %s", msg)
                        raise ScopeViolation(msg, task_id=a.id, path=fa, other_task_id=b.id)


def check_write(task: Task, path: str) -> str:
    """Return the normalized path iff `task` owns it; otherwise raise ScopeViolation."""
    p = normalize(path)
    owned = any(_covers(normalize(o), p) for o in task.files_owned)
    if p.startswith(FROZEN_PREFIXES) or not owned:
        msg = f"{task.role.value} agent (task {task.id}) attempted write outside scope: {p}"
        log.error("SCOPE_VIOLATION %s | owned=%s", msg, task.files_owned)
        raise ScopeViolation(msg, task_id=task.id, path=p)
    return p


class GuardedWriter:
    """The only way an agent can produce files. Collects writes in memory after validating each."""

    def __init__(self, task: Task):
        self.task = task
        self.files: Dict[str, str] = {}

    def write(self, path: str, content: str) -> FileWrite:
        p = check_write(self.task, path)
        self.files[p] = content
        return FileWrite(path=p, content=content)
