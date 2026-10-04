"""SQLite persistence (aiosqlite): features, tasks, agents, events, approvals. JSON blobs per row."""
from __future__ import annotations

import json
from typing import List, Optional

import aiosqlite

from models import AgentProfile, ApprovalRequest, ApprovalStatus, Contract, Event, FeatureAnalysis, Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS features (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, name TEXT NOT NULL,
    analysis TEXT NOT NULL, contract TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (role TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, feature_id TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_feature ON events(feature_id);
"""


class State:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def init(self) -> "State":
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ── features / tasks ──
    async def save_feature(self, fid: str, prompt: str, name: str, analysis: FeatureAnalysis,
                           contract: Contract, tasks: List[Task]) -> None:
        await self.db.execute("INSERT OR REPLACE INTO features(id,prompt,name,analysis,contract) VALUES(?,?,?,?,?)",
                              (fid, prompt, name, analysis.model_dump_json(), contract.model_dump_json()))
        for t in tasks:
            await self.save_task(t, commit=False)
        await self.db.commit()

    async def get_feature(self, fid: str) -> Optional[dict]:
        cur = await self.db.execute("SELECT * FROM features WHERE id=?", (fid,))
        r = await cur.fetchone()
        if not r:
            return None
        return {"id": r["id"], "prompt": r["prompt"], "name": r["name"],
                "analysis": FeatureAnalysis.model_validate_json(r["analysis"]),
                "contract": Contract.model_validate_json(r["contract"])}

    async def save_task(self, t: Task, commit: bool = True) -> None:
        await self.db.execute("INSERT OR REPLACE INTO tasks(id,feature_id,body) VALUES(?,?,?)",
                              (t.id, t.feature_id, t.model_dump_json()))
        if commit:
            await self.db.commit()

    async def get_tasks(self, fid: str) -> List[Task]:
        cur = await self.db.execute("SELECT body FROM tasks WHERE feature_id=? ORDER BY rowid", (fid,))
        return [Task.model_validate_json(r["body"]) for r in await cur.fetchall()]

    async def save_profile(self, p: AgentProfile) -> None:
        await self.db.execute("INSERT OR REPLACE INTO agents(role,body) VALUES(?,?)", (p.role.value, p.model_dump_json()))
        await self.db.commit()

    async def list_profiles(self) -> List[AgentProfile]:
        cur = await self.db.execute("SELECT body FROM agents")
        return [AgentProfile.model_validate_json(r["body"]) for r in await cur.fetchall()]

    # ── events ──
    async def add_event(self, ev: Event) -> Event:
        cur = await self.db.execute("INSERT INTO events(feature_id,body) VALUES(?,?)",
                                    (ev.feature_id, ev.model_dump_json()))
        await self.db.commit()
        return ev.model_copy(update={"seq": cur.lastrowid})

    async def list_events(self, after_seq: int = 0, feature_id: Optional[str] = None) -> List[Event]:
        q, args = "SELECT seq, body FROM events WHERE seq>?", [after_seq]
        if feature_id:
            q += " AND feature_id=?"
            args.append(feature_id)
        cur = await self.db.execute(q + " ORDER BY seq", args)
        return [Event.model_validate_json(r["body"]).model_copy(update={"seq": r["seq"]}) for r in await cur.fetchall()]

    # ── approvals ──
    async def save_approval(self, a: ApprovalRequest) -> None:
        await self.db.execute("INSERT OR REPLACE INTO approvals(id,feature_id,body) VALUES(?,?,?)",
                              (a.id, a.feature_id, a.model_dump_json()))
        await self.db.commit()

    async def get_approval(self, aid: str) -> Optional[ApprovalRequest]:
        cur = await self.db.execute("SELECT body FROM approvals WHERE id=?", (aid,))
        r = await cur.fetchone()
        return ApprovalRequest.model_validate_json(r["body"]) if r else None

    async def list_approvals(self, status: Optional[ApprovalStatus] = None) -> List[ApprovalRequest]:
        cur = await self.db.execute("SELECT body FROM approvals ORDER BY rowid")
        out = [ApprovalRequest.model_validate_json(r["body"]) for r in await cur.fetchall()]
        return [a for a in out if status is None or a.status == status]
