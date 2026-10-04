"""Hub persistence (SQLite): paired machines (hashed tokens), one-time invites, audit log, usage samples, API profiles.

API-profile keys are encrypted at rest with Fernet (key in a local file next to the DB). Never logged, never returned to the UI.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from cryptography.fernet import Fernet
except Exception:                                            # pragma: no cover
    Fernet = None

ACCESS_LEVELS = ("ask", "copilot", "full")
PROTOCOLS = ("anthropic", "openai-chat", "openai-responses")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, name TEXT, hostname TEXT, os TEXT, token_hash TEXT, member_hash TEXT,
    created REAL, last_seen REAL, member_name TEXT DEFAULT '', access TEXT DEFAULT 'ask');
CREATE TABLE IF NOT EXISTS invites (code_hash TEXT PRIMARY KEY, expires REAL, used INTEGER DEFAULT 0, created REAL,
    member_name TEXT DEFAULT '', access TEXT DEFAULT 'ask');
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, node TEXT, sid TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS usage_samples (ts REAL, node TEXT, sid TEXT, provider TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT, base_url TEXT, protocol TEXT, key_enc TEXT, key_hint TEXT,
    models TEXT, default_model TEXT, price_in REAL, price_out REAL, context_window INTEGER, created REAL);
"""


class HubStore:
    """Thread-safe sqlite3 wrapper (calls are tiny; the hub is single-process)."""

    def __init__(self, path: str | Path, secret_file: str | Path | None = None):
        self.path = Path(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()
        self.fernet = None
        if Fernet is not None:
            sf = Path(secret_file) if secret_file else self.path.with_name(self.path.stem + ".secret")
            if not sf.exists():
                sf.write_bytes(Fernet.generate_key())
                try:
                    sf.chmod(0o600)
                except OSError:
                    pass
            self.fernet = Fernet(sf.read_bytes())

    def _migrate(self) -> None:
        """Older databases lack the team columns: add them in place."""
        for table, col, ddl in (("nodes", "member_name", "TEXT DEFAULT ''"), ("nodes", "access", "TEXT DEFAULT 'ask'"),
                                ("invites", "member_name", "TEXT DEFAULT ''"), ("invites", "access", "TEXT DEFAULT 'ask'")):
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ── invites / pairing ──
    def create_invite(self, ttl_s: int = 600, member_name: str = "", access: str = "ask") -> str:
        if access not in ACCESS_LEVELS:
            raise ValueError("unknown access level")
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))        # e.g. 9F3A-0C11-B7D2
        self._x("INSERT INTO invites(code_hash,expires,used,created,member_name,access) VALUES(?,?,0,?,?,?)",
                (_h(code), time.time() + ttl_s, time.time(), member_name[:60], access))
        return code

    def invite_status(self, code: str) -> Optional[Dict[str, Any]]:
        """For the public join page: valid / used / expired, without consuming the invite. None for an unknown code."""
        rows = self._q("SELECT * FROM invites WHERE code_hash=?", (_h(code.strip().upper()),))
        if not rows:
            return None
        r = rows[0]
        status = "used" if r["used"] else ("expired" if r["expires"] < time.time() else "valid")
        return {"status": status, "member": r["member_name"] or "", "access": r["access"] or "ask", "expires_in": max(0, int(r["expires"] - time.time()))}

    def get_setting(self, k: str) -> str:
        rows = self._q("SELECT v FROM settings WHERE k=?", (k,))
        return rows[0]["v"] if rows else ""

    def set_setting(self, k: str, v: str) -> None:
        self._x("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))

    def consume_invite(self, code: str) -> Optional[Dict[str, str]]:
        """Single use. Returns {member_name, access} or None if unknown / used / expired."""
        rows = self._q("SELECT * FROM invites WHERE code_hash=?", (_h(code.strip().upper()),))
        if not rows or rows[0]["used"] or rows[0]["expires"] < time.time():
            return None
        self._x("UPDATE invites SET used=1 WHERE code_hash=?", (rows[0]["code_hash"],))
        return {"member_name": rows[0]["member_name"] or "", "access": rows[0]["access"] or "ask"}

    def create_node(self, name: str, hostname: str, os_name: str, member_name: str = "", access: str = "ask") -> Dict[str, str]:
        node_id, token, member_key = "n_" + secrets.token_hex(4), secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self._x("INSERT INTO nodes(id,name,hostname,os,token_hash,member_hash,created,last_seen,member_name,access) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (node_id, name, hostname, os_name, _h(token), _h(member_key), time.time(), time.time(), member_name, access))
        return {"node_id": node_id, "token": token, "member_key": member_key}

    def verify_node(self, node_id: str, token: str) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT * FROM nodes WHERE id=?", (node_id,))
        if rows and hmac.compare_digest(rows[0]["token_hash"], _h(token or "")):
            return rows[0]
        return None

    def node_for_member_key(self, key: str) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT * FROM nodes WHERE member_hash=?", (_h(key or ""),))
        return rows[0] if rows else None

    def touch_node(self, node_id: str, name: str, hostname: str, os_name: str) -> None:
        self._x("UPDATE nodes SET last_seen=?, name=?, hostname=?, os=? WHERE id=?", (time.time(), name, hostname, os_name, node_id))

    # ── projects (one JSON document each; the engine owns the shape) ──
    def save_project(self, pid: str, data: Dict[str, Any]) -> None:
        self._x("INSERT OR REPLACE INTO projects(id,data,updated) VALUES(?,?,?)", (pid, json.dumps(data, default=str), time.time()))

    def load_projects(self) -> List[Dict[str, Any]]:
        out = []
        for r in self._q("SELECT data FROM projects ORDER BY updated"):
            try:
                out.append(json.loads(r["data"]))
            except ValueError:
                continue
        return out

    def delete_project(self, pid: str) -> None:
        self._x("DELETE FROM projects WHERE id=?", (pid,))

    def set_node_access(self, node_id: str, access: str) -> None:
        if access in ACCESS_LEVELS:
            self._x("UPDATE nodes SET access=? WHERE id=?", (access, node_id))

    def list_nodes(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT id,name,hostname,os,created,last_seen,member_name,access FROM nodes ORDER BY created")]

    def remove_node(self, node_id: str) -> None:
        self._x("DELETE FROM nodes WHERE id=?", (node_id,))

    # ── audit ──
    def audit(self, actor: str, action: str, node: str = "", sid: str = "", detail: str = "") -> None:
        self._x("INSERT INTO audit(ts,actor,action,node,sid,detail) VALUES(?,?,?,?,?,?)", (time.time(), actor, action, node, sid, detail[:400]))

    def audit_list(self, limit: int = 200, node: str = "") -> List[Dict[str, Any]]:
        if node:
            rows = self._q("SELECT * FROM audit WHERE node=? ORDER BY seq DESC LIMIT ?", (node, limit))
        else:
            rows = self._q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ── usage ──
    def add_usage(self, node: str, sid: str, provider: str, u: Dict[str, Any]) -> None:
        self._x("INSERT INTO usage_samples VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), node, sid, provider, u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("cache_read", 0), u.get("cache_write", 0), u.get("source", "unknown")))

    def usage_by_day(self, days: int = 14) -> List[Dict[str, Any]]:
        since = time.time() - days * 86400
        rows = self._q("""SELECT date(ts,'unixepoch','localtime') AS day, node, provider, MAX(input_tokens) AS inp, MAX(output_tokens) AS out
                          FROM usage_samples WHERE ts>? GROUP BY day, node, sid ORDER BY day""", (since,))
        return [dict(r) for r in rows]

    # ── API profiles (custom base URL / key / model) ──
    def _enc(self, key: str) -> str:
        if not self.fernet:
            raise RuntimeError("the 'cryptography' package is required to store API keys safely")
        return self.fernet.encrypt(key.encode()).decode()

    def save_profile(self, p: Dict[str, Any], pid: str = "") -> str:
        """Create or update. An empty api_key on update keeps the stored key."""
        name, base = str(p.get("name", "")).strip()[:60], str(p.get("base_url", "")).strip().rstrip("/")
        if not name or not base.startswith(("http://", "https://")):
            raise ValueError("a name and an http(s) base URL are required")
        if p.get("protocol") not in PROTOCOLS:
            raise ValueError("protocol must be one of " + ", ".join(PROTOCOLS))
        models = [str(m).strip() for m in (p.get("models") or []) if str(m).strip()][:40]
        dm = str(p.get("default_model") or (models[0] if models else "")).strip()
        pid = pid or "p_" + secrets.token_hex(3)
        old = self._q("SELECT key_enc, key_hint FROM profiles WHERE id=?", (pid,))
        key = str(p.get("api_key") or "")
        if key:
            enc, hint = self._enc(key), key[-4:]
        elif old:                                                  # no new key supplied: keep the stored (encrypted) one
            enc, hint = old[0]["key_enc"], old[0]["key_hint"]
        else:
            enc, hint = "", ""
        vals = (pid, name, base, p["protocol"], enc, hint, json.dumps(models), dm, float(p.get("price_in") or 0), float(p.get("price_out") or 0),
                int(p.get("context_window") or 0), time.time())
        self._x("INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", vals)
        return pid

    def get_profile(self, pid: str, with_key: bool = False) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM profiles WHERE id=?", (pid,))
        if not rows:
            return None
        r = dict(rows[0])
        r["models"] = json.loads(r["models"] or "[]")
        enc = r.pop("key_enc") if not with_key else r["key_enc"]
        if with_key:
            r["api_key"] = self.fernet.decrypt(enc.encode()).decode() if (enc and self.fernet) else ""
            r.pop("key_enc", None)
        r["has_key"] = bool(enc)
        return r

    def list_profiles(self) -> List[Dict[str, Any]]:
        return [self.get_profile(r["id"]) for r in self._q("SELECT id FROM profiles ORDER BY created")]

    def delete_profile(self, pid: str) -> None:
        self._x("DELETE FROM profiles WHERE id=?", (pid,))
