"""T1: bounded retry + exponential backoff, fail loudly, per-agent wall budget."""
import asyncio

import pytest

from agents.base import AgentTimeout
from agents.frontend import FrontendAgent
from llm import LLMResult, LLMTransportError, RetryingLLM, is_transient
from models import EventType
from tests.conftest import run
from tests.fakes import TestDoubleLLM
from tests.test_pipeline import _planned


class Flaky:
    def __init__(self, failures, exc=ConnectionError("Connection error")):
        self.failures, self.exc, self.calls = failures, exc, 0

    async def complete(self, system, user, *, max_tokens=8000):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return LLMResult("ok", 1, 1, "stop")


def _retrying(inner, **kw):
    delays = []

    async def fake_sleep(d):
        delays.append(d)
    return RetryingLLM(inner, sleep=fake_sleep, **kw), delays


def test_recovers_after_two_connection_errors_with_exponential_backoff():
    flaky = Flaky(2)
    llm, delays = _retrying(flaky, max_tries=4, base_delay_s=1.0)
    res = run(llm.complete("s", "u"))
    assert res.text == "ok" and flaky.calls == 3 and llm.retries == 2
    assert len(delays) == 2 and 0.75 <= delays[0] <= 1.25 and 1.5 <= delays[1] <= 2.5     # ~1s then ~2s


def test_fails_loudly_after_max_tries():
    flaky = Flaky(99)
    llm, delays = _retrying(flaky, max_tries=3)
    with pytest.raises(LLMTransportError, match="after 3 attempt"):
        run(llm.complete("s", "u"))
    assert flaky.calls == 3 and len(delays) == 2


def test_non_transient_errors_are_not_retried():
    flaky = Flaky(99, exc=ValueError("invalid api key"))
    llm, delays = _retrying(flaky, max_tries=4)
    with pytest.raises(ValueError):
        run(llm.complete("s", "u"))
    assert flaky.calls == 1 and delays == []


def test_5xx_and_429_are_transient_but_400_is_not():
    class E(Exception):
        def __init__(self, code): self.status_code = code
    assert is_transient(E(502)) and is_transient(E(429)) and not is_transient(E(400)) and not is_transient(E(401))
    assert is_transient(asyncio.TimeoutError()) and is_transient(ConnectionError())


def test_call_deadline_bounds_a_hanging_request():
    class Hang:
        async def complete(self, *a, **k):
            await asyncio.sleep(5)
    llm = RetryingLLM(Hang(), max_tries=3, base_delay_s=0.01, deadline_s=0.3)
    with pytest.raises(LLMTransportError):
        run(llm.complete("s", "u"))


def test_agent_wall_budget_blocks_loudly(services, seeded_contracts):
    async def go():
        s, _, contract, tasks = await _planned(services, llm=TestDoubleLLM(latency=2.0))
        fe = FrontendAgent(TestDoubleLLM(latency=2.0), ("Finn", "f@x.io"), s.bus.emit, wall_budget_s=0.3)
        with pytest.raises(AgentTimeout, match="wall budget"):
            await fe.run(tasks[1], contract)
        evs = await s.state.list_events()
        assert any(e.type == EventType.BLOCKED and e.data.get("reason") == "wall_budget" for e in evs)
    run(go())
