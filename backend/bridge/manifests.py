"""Provider manifests: adding a provider = adding a YAML file in bridge/manifests/ (no code).

A manifest only ever names a command the SERVER operator chose. Browsers send a provider id, never a command.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

MANIFEST_DIR = Path(__file__).parent / "manifests"
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
