"""Acceptance G (events in order, incl. over a real HTTP SSE connection), J (banner), API wiring."""
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app import create_app
from config import banner
from models import EventType
from orchestrator.dispatch import dispatch_feature
from tests.conftest import make_settings, run
from tests.fakes import TestDoubleGitHub, TestDoubleLLM
from tests.test_pipeline import DEMO, _planned

ORDER = [EventType.THINKING, EventType.WRITE_CODE, EventType.COMMIT, EventType.PR_CREATED, EventType.NEEDS_REVIEW]


def test_G_events_stream_in_order_as_they_happen(services, seeded_contracts):
    async def go():
        import asyncio
        s, _, contract, tasks = await _planned(services, llm=TestDoubleLLM(latency=0.05))
        got = []

        async def consume():
            async for frame in s.bus.stream(replay=False, heartbeat=5):
                if frame["event"] == "ping":
                    continue
                got.append(json.loads(frame["data"]))
                if sum(1 for e in got if e["type"] == "NEEDS_REVIEW") == 3:
                    return

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await dispatch_feature(feature_id="feat_test01", contract=contract, tasks=tasks, agents=s.make_agents(),
                               github=s.get_github(), state=s.state, bus=s.bus)
        await asyncio.wait_for(consumer, 5)
        seqs = [e["seq"] for e in got]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)           # strictly ordered, no dupes
        for role in ("backend", "frontend", "qa"):
            types = [EventType(e["type"]) for e in got if e["agent"] == role]
            first = [types.index(t) for t in ORDER]
            assert first == sorted(first), (role, types)                       # THINKING..NEEDS_REVIEW in order
        stored = [e.seq for e in await s.state.list_events()]
        assert seqs == stored                                                  # live == persisted
        print("\nG: live order ->", [(e["agent"], e["type"]) for e in got][:8], "...")
    run(go())


def test_G_late_subscriber_replays_history_then_goes_live(services, seeded_contracts):
    async def go():
        s, *_ = await _planned(services)
        await s.bus.publish(EventType.THINKING, "one")
        await s.bus.publish(EventType.BLOCKED, "two")
        it = s.bus.stream(heartbeat=5)
        a, b = await it.__anext__(), await it.__anext__()
        assert [a["event"], b["event"]] == ["THINKING", "BLOCKED"]
        await s.bus.publish(EventType.COMMIT, "three")
        c = await it.__anext__()
        assert c["event"] == "COMMIT" and int(c["id"]) > int(b["id"])
        await it.aclose()
    run(go())


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def test_G_real_sse_over_http_and_full_api_flow(tmp_path):
    """Boots the real FastAPI app under uvicorn; drives POST /features -> dispatch -> approvals over HTTP
    while a client reads GET /events (SSE). LLM + GitHub are the labelled test doubles."""
    import shutil
    from tests.conftest import REAL_CONTRACTS
    settings = make_settings(tmp_path)
    for n in ("demo_openapi.yaml", "demo_types.ts"):
        shutil.copy(REAL_CONTRACTS / n, settings.contracts_dir / n)
    gh = TestDoubleGitHub()
    app = create_app(settings, llm=TestDoubleLLM(), github=gh)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    seen = []

    def read_sse():
        with httpx.stream("GET", base + "/events", timeout=20) as r:
            ev = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and ev and ev != "ping":
                    seen.append((ev, json.loads(line.split(":", 1)[1])))
                if sum(1 for e, _ in seen if e == "NEEDS_REVIEW") == 3 and any(
                        d["data"].get("stage") == "rejected" for e, d in seen if e == "BLOCKED"):
                    return

    t = threading.Thread(target=read_sse, daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        r = httpx.post(base + "/features", json={"prompt": DEMO}, timeout=20)
        assert r.status_code == 200, r.text
        fid = r.json()["feature_id"]
        assert [x["role"] for x in r.json()["tasks"]] == ["backend", "frontend", "qa"]
        c = httpx.get(f"{base}/features/{fid}/contract").json()
        assert c["sha256"] == r.json()["contract"]["sha256"] and "/billing/usage" in c["openapi_yaml"]
        d = httpx.post(f"{base}/features/{fid}/dispatch", timeout=30)
        assert d.status_code == 200 and all(x["status"] == "ok" for x in d.json()["runs"]), d.text
        appr = httpx.get(base + "/approvals").json()
        assert len(appr) == 3 and gh.merges == []
        rej = httpx.post(f"{base}/approvals/{appr[0]['id']}/reject", json={"reason": "nope"})
        assert rej.status_code == 200 and rej.json()["status"] == "rejected" and gh.merges == []
        ok = httpx.post(f"{base}/approvals/{appr[1]['id']}/approve")
        assert ok.status_code == 200 and ok.json()["status"] == "merged" and gh.merges == [appr[1]["pr"]["number"]]
        again = httpx.post(f"{base}/approvals/{appr[0]['id']}/approve")
        assert again.status_code == 409 and gh.merges == [appr[1]["pr"]["number"]]
        t.join(10)
        names = [e for e, _ in seen]
        assert "THINKING" in names and "WRITE_CODE" in names and names.count("PR_CREATED") == 3
        assert names.index("THINKING") < names.index("WRITE_CODE") < names.index("COMMIT") < names.index("NEEDS_REVIEW")
        print("\nG: HTTP SSE saw", len(seen), "events:", sorted(set(names)))
    finally:
        server.should_exit = True
        th.join(5)


def test_API_missing_llm_key_returns_503_with_message(tmp_path):
    from fastapi.testclient import TestClient
    app = create_app(make_settings(tmp_path, anthropic_api_key=None), llm=None)
    with TestClient(app) as c:
        r = c.post("/features", json={"prompt": DEMO})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_J_banner_lists_real_and_simulated(tmp_path):
    txt = banner(make_settings(tmp_path))
    print("\n" + txt)
    assert "REAL" in txt and "SIMULATED_WORKER_SANDBOX" in txt and "SIMULATED_TEST_EXECUTION" in txt
    assert "SIMULATED components: SIMULATED_WORKER_SANDBOX, SIMULATED_TEST_EXECUTION" in txt
    assert "test-key-not-real" not in txt and "test-token-not-real" not in txt        # never prints secrets
    unconfigured = banner(make_settings(tmp_path, anthropic_api_key=None, github_token=None))
    assert "NOT CONFIGURED" in unconfigured
