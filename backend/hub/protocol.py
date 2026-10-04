"""Shared protocol constants and helpers for Hub, Node agent and Console.

Wire format: JSON text frames {"t": <type>, ...}. Node<->Hub on /hub/node, Console<->Hub on /hub/viewer.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict

PROTOCOL_VERSION = 1

# Control ladder (what the lead may do to a session). The MEMBER owns the grant; node and hub both enforce it.
MODES = ("watch", "suggest", "copilot", "takeover")
MODE_RANK = {m: i for i, m in enumerate(MODES)}
TAKEOVER_MAX_MIN = 10          # takeover auto-expires; the lead may renew while the member still allows it
RING_CHARS = 240_000           # per-session replay buffer on the hub

ROLE_LEAD, ROLE_MEMBER = "lead", "member"

# Best-effort secret redaction applied on the NODE before output leaves the member's machine.
_SECRET_PATTERNS = [
    (re.compile(r"thk_live_[A-Za-z0-9_\-]{12,}"), "thk_live_[REDACTED]"),
    (re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}"), "gh*_[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_[REDACTED]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "sk-[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "[REDACTED PRIVATE KEY]"),
]


# Terminal *replies* (cursor position, device attributes, colour queries ...). A viewer's xterm generates these automatically;
# exactly one elected "responder" viewer may forward them, and ONLY text that fully matches this pattern - so the reply
# channel can never be used to type into a session.
_REPLY = (r"(?:\x1b\[(?:\d+;\d+R|\?[\d;]*c|>[\d;]*c|[\d;]*n|\?[\d;]*\$y|[\d;]*t|\?[\d;]*u)"
          r"|\x1b\](?:4|10|11|12|104);[^\x07\x1b]*(?:\x07|\x1b\\)"
          r"|\x1bP[^\x1b]*\x1b\\)")
REPLY_RE = re.compile(rf"{_REPLY}+")


def is_terminal_reply(d: str) -> bool:
    return bool(d) and len(d) < 400 and REPLY_RE.fullmatch(d) is not None


def redact(text: str) -> str:
    for rx, rep in _SECRET_PATTERNS:
        text = rx.sub(rep, text)
    return text


def now_ms() -> int:
    return int(time.time() * 1000)


def clip(s: Any, n: int = 200) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def valid_mode(m: Any) -> bool:
    return isinstance(m, str) and m in MODE_RANK


def jdump(**kw: Any) -> str:
    import json
    return json.dumps(kw, separators=(",", ":"), default=str)


Json = Dict[str, Any]
