"""Event bus + SSE generator. Events are persisted FIRST (assigning a monotonic seq), then fanned
out, so a late subscriber can replay history and an early one sees live transitions in order."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional, Set

from models import AgentRole, Event, EventType
from state import State


class EventBus:
    def __init__(self, state: State):
        self.state = state
        self._subs: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()          # keeps persist+fanout atomic so seq order == delivery order

    async def publish(self, type: EventType, message: str, *, feature_id: Optional[str] = None,
                      agent: Optional[AgentRole] = None, data: Optional[Dict[str, Any]] = None) -> Event:
        async with self._lock:
            ev = await self.state.add_event(Event(feature_id=feature_id, type=type, agent=agent,
                                                  message=message, data=data or {}))
            for q in list(self._subs):
                q.put_nowait(ev)
        return ev

    async def emit(self, type: EventType, message: str, **kw) -> None:
        """Signature used by agents (Emit)."""
        await self.publish(type, message, **kw)

    async def stream(self, *, after_seq: int = 0, feature_id: Optional[str] = None, replay: bool = True,
                     heartbeat: float = 15.0) -> AsyncIterator[dict]:
        """Yield SSE-shaped dicts {id, event, data}. Subscribes BEFORE replaying so nothing is missed."""
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        last = after_seq
        try:
            if replay:
                for ev in await self.state.list_events(after_seq, feature_id):
                    last = ev.seq
                    yield self._frame(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                if ev.seq <= last or (feature_id and ev.feature_id != feature_id):
                    continue
                last = ev.seq
                yield self._frame(ev)
        finally:
            self._subs.discard(q)

    @staticmethod
    def _frame(ev: Event) -> dict:
        return {"id": str(ev.seq), "event": ev.type.value, "data": ev.model_dump_json()}
