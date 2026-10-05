"""Provider manifests: adding a provider = adding a YAML file in bridge/manifests/ (no code).

A manifest only ever names a command the SERVER operator chose. Browsers send a provider id, never a command.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

MANIFEST_DIR = Path(os.environ.get("KODE_MANIFEST_DIR") or Path(__file__).parent / "manifests")        # override: tests / custom provider sets
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)
BASE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "TEMP", "TMP", "USERNAME",
            "COMPUTERNAME", "HOME", "SHELL", "LANG", "TERM")


@dataclass
class Manifest:
    id: str
    label: str
    command: List[str]
    tier: int = 0
    env_passthrough: List[str] = field(default_factory=list)

    @property
    def resolved(self) -> str | None:
        """Absolute path of the executable if installed, else None."""
        return shutil.which(self.command[0])

    @property
    def available(self) -> bool:
        return self.resolved is not None

    def argv(self) -> List[str]:
        return [self.resolved or self.command[0], *self.command[1:]]

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "tier": self.tier, "available": self.available}


NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def refresh_path() -> bool:
    """Pick up programs installed AFTER this process started.

    A running program keeps the PATH it was launched with, so a tool the teammate installs later (Antigravity, Claude Code...) is invisible
    until a restart. On Windows we re-read the machine and user PATH from the registry; everywhere we add the usual install folders and
    any folders listed in the file named by KODE_EXTRA_PATH_FILE (one per line; used by tests and unusual setups). Returns True if PATH grew.
    """
    have = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    low = {os.path.normcase(os.path.normpath(p)) for p in have}
    extra: List[str] = []
    if os.name == "nt":
        try:
            import winreg
            for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"), (winreg.HKEY_CURRENT_USER, "Environment")):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        extra += [os.path.expandvars(x) for x in str(winreg.QueryValueEx(k, "Path")[0]).split(";") if x]
                except OSError:
                    continue
        except ImportError:
            pass
        extra += [os.path.expandvars(r"%APPDATA%\npm"), os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"), os.path.expandvars(r"%USERPROFILE%\.local\bin")]
    else:
        home = str(Path.home())
        extra += [home + "/.local/bin", home + "/.npm-global/bin", home + "/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    f = os.environ.get("KODE_EXTRA_PATH_FILE")
    if f:
        try:
            extra += [l.strip() for l in Path(f).read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            pass
    added = []
    for p in extra:
        k = os.path.normcase(os.path.normpath(p))
        if k not in low and os.path.isdir(p):
            low.add(k)
            added.append(p)
    if added:
        os.environ["PATH"] = os.pathsep.join(have + added)
    return bool(added)


_AUTH_CACHE: Dict[str, tuple] = {}


def _run_quiet(argv: List[str], timeout: float = 8.0):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def signed_in(m: "Manifest") -> Optional[bool]:
    """Best-effort 'is this tool logged in?'. True / False when the tool can tell us cheaply, None when we cannot know."""
    if not m.available:
        return None
    hit = _AUTH_CACHE.get(m.id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    res: Optional[bool] = None
    exe = m.resolved or m.command[0]
    if m.id == "claude":
        r = _run_quiet([exe, "auth", "status"])
        if r is not None and r.stdout.strip().startswith("{"):
            try:
                res = bool(json.loads(r.stdout).get("loggedIn"))
            except ValueError:
                res = None
    elif m.id == "codex":
        r = _run_quiet([exe, "login", "status"])
        if r is not None:
            res = r.returncode == 0 and "logged in" in (r.stdout + r.stderr).lower()
    _AUTH_CACHE[m.id] = (time.time(), res)
    return res


def scan(manifests: Dict[str, "Manifest"], probe_auth: bool = True) -> List[dict]:
    """The provider list a node reports: installed or not, and signed in or not (when the tool can tell)."""
    out = []
    for m in manifests.values():
        d = m.public()
        d["signed_in"] = signed_in(m) if (probe_auth and d["available"]) else None
        out.append(d)
    return out


def load_manifests(directory: Path = MANIFEST_DIR) -> Dict[str, Manifest]:
    out: Dict[str, Manifest] = {}
    for f in sorted(directory.glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("command"), list) or not d["command"]:
            raise ValueError(f"bad manifest {f.name}: needs id, label and a non-empty command list")
        sub = {"{python}": sys.executable, "{backend}": str(Path(__file__).resolve().parents[1])}
        cmd = []
        for c in (d.get("command_win") if sys.platform == "win32" and d.get("command_win") else d["command"]):
            c = str(c)
            for k, v in sub.items():
                c = c.replace(k, v)
            cmd.append(c)
        m = Manifest(id=str(d["id"]), label=str(d.get("label", d["id"])), command=cmd,
                     tier=int(d.get("tier", 0)), env_passthrough=[str(e) for e in d.get("env_passthrough", [])])
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", m.id):
            raise ValueError(f"bad manifest id {m.id!r}")
        out[m.id] = m
    return out


def scrubbed_env(manifest: Manifest, environ: dict | None = None) -> Dict[str, str]:
    """Environment for a shared terminal: a small allowlist, with secret-looking variables removed unless the
    manifest explicitly passes them through. Prevents the host's exported API keys leaking into a viewed/controlled session."""
    src = dict(os.environ if environ is None else environ)
    keep = {k.upper() for k in BASE_ENV} | {k.upper() for k in manifest.env_passthrough}
    env = {k: v for k, v in src.items() if k.upper() in keep and (not SECRET_NAME.search(k) or k.upper() in {p.upper() for p in manifest.env_passthrough})}
    env.setdefault("TERM", "xterm-256color")
    return env
