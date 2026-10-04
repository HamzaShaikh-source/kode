"""End-to-end demo with the real demo prompt. REAL LLM calls (LiteLLM); REAL GitHub (PyGithub) unless
--stop-before-github, in which case the GitHub half is SKIPPED (printed as such) - never simulated.

  python demo/run_demo.py                       # full loop: 3 branches, 3 authors, 3 PRs, 3 approvals
  python demo/run_demo.py --stop-before-github  # real LLM only: plan, contract, 3 parallel agents, review
  python demo/run_demo.py --contracts-dir ./scratch_contracts   # force an LLM-drafted contract
Merging is NOT done here: approve via POST /approvals/{id}/approve (human decision).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.backend import BackendAgent                 # noqa: E402
from agents.frontend import FrontendAgent               # noqa: E402
from agents.qa import QAAgent                           # noqa: E402
from app import Services                                # noqa: E402
from config import Settings, banner                     # noqa: E402
from events import EventBus                             # noqa: E402
from llm import build_llm                                # noqa: E402
from models import AgentRole, new_id                    # noqa: E402
from orchestrator.contract import freeze_contract       # noqa: E402
from orchestrator.decompose import decompose            # noqa: E402
from orchestrator.dispatch import dispatch_feature      # noqa: E402
from orchestrator.planner import plan_feature           # noqa: E402
from pipeline.review import review                      # noqa: E402
from state import State                                 # noqa: E402

DEMO_PROMPT = "Add an enterprise Stripe billing tier with usage-based rate limits and frontend usage graphs."


async def main(args) -> int:
    settings = Settings()
    if args.contracts_dir:
        settings.kode_contracts_dir = args.contracts_dir
    print(banner(settings))
    settings.require_llm()                                       # loud failure, never simulate
    state = await State(args.db).init()
    bus = EventBus(state)
    if args.minimal:
        settings.agent_wall_budget_s = settings.agent_wall_budget_s or 180.0
    llm = build_llm(settings)
    fid = new_id("feat")

    async def printer():
        async for fr in bus.stream(replay=False, heartbeat=3600):
            import json
            e = json.loads(fr["data"])
            print(f"  [event #{e['seq']:>3}] {e['type']:<12} {e['agent'] or '-':<9} {e['message']}")
    ptask = asyncio.create_task(printer())

    print("\n== 1. PLAN (real LLM) ==")
    prompt = args.prompt
    if args.minimal:
        prompt += (" MINIMAL SCOPE: each of the 3 agents owns exactly ONE file (backend api/routes/billing.py, frontend"
                   " web/lib/api/billingClient.ts, qa tests/contract/test_billing_contract.py). Use EXACTLY these"
                   " endpoints: POST /billing/tiers, GET /billing/usage, POST /stripe/webhook; and exactly these"
                   " required_schemas: BillingTier, UsageRecord, CreateBillingTierRequest, UsageReport.")
    analysis, u = await plan_feature(llm, prompt)
    print(analysis.summary)
    for t in analysis.tasks:
        print(f"  {t.role.value:<9} owns {t.files_owned}")

    print("\n== 2. CONTRACT (freeze BEFORE any agent code) ==")
    contract, _ = await freeze_contract(llm, fid, "demo", analysis, settings.contracts_dir)
    print(f"  source={contract.source} sha256={contract.sha256}")
    print(f"  {contract.openapi_path}\n  {contract.types_path}")
    print(f"  endpoints: {[f'{e.method} {e.path}' for e in contract.endpoints]}")

    print("\n== 3. DECOMPOSE (guard-validated disjoint ownership) ==")
    tasks = decompose(fid, analysis, contract)
    if args.minimal:                                   # shrink work AFTER planning; ownership stays valid (subset)
        for t in tasks:
            t.files_owned = t.files_owned[:1]
            t.description += " Write ONLY the single owned file, under 40 lines, no commentary."
            if t.role.value == "backend":
                t.description += (" Keep it trivial: three thin FastAPI route stubs matching the contract paths, returning"
                                  " Pydantic model instances; no database, no stripe package, no auth.")
    for t in tasks:
        print(f"  {t.id} {t.role.value:<9} {t.files_owned}")

    if args.stop_before_github:
        print("\n== 4. PARALLEL AGENTS (real LLM) -- GITHUB STAGE SKIPPED (not simulated) ==")
        try:
            owners = {r: ("Demo Owner", "demo@example.invalid") for r in AgentRole}
            agents = {AgentRole.backend: BackendAgent(llm, owners[AgentRole.backend], bus.emit, settings.agent_wall_budget_s),
                      AgentRole.frontend: FrontendAgent(llm, owners[AgentRole.frontend], bus.emit, settings.agent_wall_budget_s),
                      AgentRole.qa: QAAgent(llm, owners[AgentRole.qa], bus.emit, settings.agent_wall_budget_s)}
            t0 = time.perf_counter()

            async def one(task):
                s = time.perf_counter()
                try:
                    return task, await agents[task.role].run(task, contract), time.perf_counter() - s, None
                except Exception as e:                       # report, don't hide
                    return task, None, time.perf_counter() - s, e
            results = await asyncio.gather(*(one(t) for t in tasks))
            wall = time.perf_counter() - t0
            for task, res, el, err in results:
                if err:
                    print(f"  {task.role.value:<9} FAILED after {el:.1f}s: {type(err).__name__}: {err}")
                    continue
                rep = review(task.role, {f.path: f.content for f in res.files}, contract)
                print(f"  {task.role.value:<9} {el:6.1f}s  tokens in/out={res.usage.prompt_tokens}/{res.usage.completion_tokens}"
                      f"  files={[f.path for f in res.files]}  review: {rep.summary()}")
                for f in rep.findings:
                    print(f"      - {f.severity}: {f.message}")
            serial = sum(r[2] for r in results)
            print(f"  WALL-CLOCK {wall:.1f}s  SERIAL-EQUIVALENT {serial:.1f}s  SPEEDUP x{serial / wall:.2f}")
            outdir = Path(args.dump_dir)
            for _, res, _, _ in results:
                for f in (res.files if res else []):
                    p = outdir / f.path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(f.content, encoding="utf-8")
            print(f"  generated files written for inspection under {outdir}/ (NOT committed anywhere)")
        finally:
            await asyncio.sleep(0.1)
            ptask.cancel()
            await state.close()
        return 0

    print("\n== 4. DISPATCH: parallel agents -> branches -> commits (mapped authors) -> PRs -> review ==")
    services = Services(settings, state, bus, llm)
    report = await dispatch_feature(feature_id=fid, contract=contract, tasks=tasks, agents=services.make_agents(),
                                    github=services.get_github(), state=state, bus=bus)
    print(report.table())
    print("\n== 5. PULL REQUESTS ==")
    for r in report.runs:
        print(f"  {r.role.value:<9} {r.pr_url}   author={r.author}   branch={r.branch}   review={r.review_summary}")
    print("\n== 6. AWAITING HUMAN APPROVAL (nothing merged) ==")
    for a in await state.list_approvals():
        print(f"  {a.id}  PR #{a.pr.number}  {a.status.value}   approve: POST /approvals/{a.id}/approve")
    await asyncio.sleep(0.1)
    ptask.cancel()
    await state.close()
    return 0 if all(r.status == "ok" for r in report.runs) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEMO_PROMPT)
    ap.add_argument("--contracts-dir")
    ap.add_argument("--db", default="./demo_state.db")
    ap.add_argument("--stop-before-github", action="store_true")
    ap.add_argument("--dump-dir", default="./demo_output")
    ap.add_argument("--minimal", action="store_true",
                    help="small contract + ONE tiny file per agent (fast smoke run with slow/cheap models)")
    sys.exit(asyncio.run(main(ap.parse_args())))
