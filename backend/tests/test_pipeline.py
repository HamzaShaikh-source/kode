"""Acceptance B, D (integrated), E, F, H - plus review + GitHub-client unit checks."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
import yaml

from agents.base import ContractViolation
from agents.frontend import FrontendAgent
from config import MissingConfigError
from models import AgentRole, ApprovalStatus, EventType
from orchestrator.contract import ContractConflict, ContractDrift, assert_frozen, freeze_contract
from orchestrator.decompose import decompose
from orchestrator.dispatch import ApprovalError, approve, dispatch_feature, reject
from orchestrator.guard import ScopeViolation
from orchestrator.planner import plan_feature
from pipeline.github import GitHubClient, MergeNotApproved
from pipeline.review import review
from tests.conftest import make_settings, run
from tests.fakes import PLAN, TestDoubleGitHub, TestDoubleLLM

DEMO = "Add an enterprise Stripe billing tier with usage-based rate limits and frontend usage graphs."


async def _planned(services, seeded=True, **kw):
    s = await services(**kw)
    analysis, _ = await plan_feature(s.llm, DEMO)
    contract, _ = await freeze_contract(s.llm, "feat_test01", "demo", analysis, s.settings.contracts_dir)
    tasks = decompose("feat_test01", analysis, contract)
    return s, analysis, contract, tasks


# ───────────── B: contract-first, written to disk and frozen ─────────────
def test_B_llm_drafted_contract_is_written_validated_and_frozen(services, tmp_path):
    async def go():
        s, analysis, contract, tasks = await _planned(services)      # empty contracts dir -> LLM drafts
        d = s.settings.contracts_dir
        assert (d / "demo_openapi.yaml").exists() and (d / "demo_types.ts").exists()
        assert contract.source == "llm-drafted"
        lock = json.loads((d / "demo_openapi.lock.json").read_text())
        assert lock["sha256"] == contract.sha256
        spec = yaml.safe_load((d / "demo_openapi.yaml").read_text())
        assert {"/billing/tiers", "/billing/usage", "/stripe/webhook"} <= set(spec["paths"])
        assert {"BillingTier", "UsageRecord"} <= set(spec["components"]["schemas"])
        assert len(tasks) == 3 and all(t.contract_sha256 == contract.sha256 for t in tasks)
    run(go())


def test_B_existing_baseline_is_used_not_overwritten(services, seeded_contracts):
    async def go():
        before = (seeded_contracts / "demo_openapi.yaml").read_bytes()
        s, _, contract, _ = await _planned(services)
        assert contract.source == "baseline-on-disk" and (seeded_contracts / "demo_openapi.yaml").read_bytes() == before
        assert "contract" not in s.llm.calls             # no LLM draft when the baseline satisfies the plan
    run(go())


def test_frozen_contract_drift_is_detected(services, seeded_contracts):
    async def go():
        _, _, contract, _ = await _planned(services)
        assert_frozen(contract)
        p = seeded_contracts / "demo_types.ts"
        p.write_text(p.read_text() + "\nexport interface Sneaky { x: string }\n")
        with pytest.raises(ContractDrift):
            assert_frozen(contract)
    run(go())


def test_uncovered_plan_never_overwrites_existing_contract(services, seeded_contracts):
    async def go():
        s = await services()
        analysis, _ = await plan_feature(s.llm, DEMO)
        analysis.required_endpoints.append(type(analysis.required_endpoints[0])(method="GET", path="/invoices"))
        with pytest.raises(ContractConflict):
            await freeze_contract(s.llm, "f", "demo", analysis, s.settings.contracts_dir)
    run(go())


def test_decompose_rejects_cross_domain_claim_end_to_end(services, seeded_contracts):
    async def go():
        s = await services()
        analysis, _ = await plan_feature(s.llm, DEMO)
        contract, _ = await freeze_contract(s.llm, "f", "demo", analysis, s.settings.contracts_dir)
        analysis.tasks[2].files_owned = ["api/routes/billing.py"]    # QA tries to claim a backend file
        with pytest.raises(ScopeViolation):
            decompose("f", analysis, contract)
    run(go())


# ───────────── E: true concurrency ─────────────
def test_E_agents_run_concurrently_wall_clock_vs_serial(services, seeded_contracts, capsys):
    async def go():
        s, _, contract, tasks = await _planned(services, llm=TestDoubleLLM(latency=0.5), github=TestDoubleGitHub(latency=0.1))
        report = await dispatch_feature(feature_id="feat_test01", contract=contract, tasks=tasks,
                                        agents=s.make_agents(), github=s.get_github(), state=s.state, bus=s.bus)
        print("\n" + report.table())
        assert all(r.status == "ok" for r in report.runs), report.table()
        assert report.serial_s > 1.5                       # 3 x (0.5 llm + 3 x 0.1 github)
        assert report.wall_s < report.serial_s * 0.55      # parallel: ~one agent's time, not three
        assert report.speedup > 2.0
    run(go())


# ───────────── D (integrated): rogue agent is blocked, others unaffected, nothing committed for it ─────────────
def test_D_rogue_agent_blocked_in_dispatch(services, seeded_contracts):
    async def go():
        llm = TestDoubleLLM(extra_files={"backend": {"web/components/BillingUsage.tsx": "stolen"}})
        s, _, contract, tasks = await _planned(services, llm=llm)
        gh = s.get_github()
        report = await dispatch_feature(feature_id="feat_test01", contract=contract, tasks=tasks,
                                        agents=s.make_agents(), github=gh, state=s.state, bus=s.bus)
        by = {r.role.value: r for r in report.runs}
        assert by["backend"].status == "blocked" and "ScopeViolation" in by["backend"].error
        assert by["frontend"].status == "ok" and by["qa"].status == "ok"
        assert {c.branch.split("/")[-1] for c in gh.commits} == {"frontend", "qa"}   # backend wrote nothing
        evs = await s.state.list_events(feature_id="feat_test01")
        assert any(e.type == EventType.BLOCKED and e.agent == AgentRole.backend for e in evs)
    run(go())


# ───────────── F: frontend imports generated TS types, never invented ones ─────────────
def test_F_frontend_imports_generated_types(services, seeded_contracts):
    async def go():
        s, _, contract, tasks = await _planned(services)
        fe = FrontendAgent(s.llm, ("Finn", "f@x.io"))
        res = await fe.run(tasks[1], contract)
        for f in res.files:
            if f.path.endswith(".ts") or f.path.endswith(".tsx"):
                assert '@contracts/demo_types' in f.content, f.path
        client = next(f for f in res.files if f.path.endswith("billingClient.ts")).content
        assert "BillingTier" in client and "UsageReport" in client
    run(go())


def test_F_invented_types_are_rejected(services, seeded_contracts):
    async def go():
        bad = lambda files: {**files, "web/components/BillingUsage.tsx":
                             'import type { BillingChartData } from "@contracts/demo_types";\nexport const X = 1;\n'}
        s, _, contract, tasks = await _planned(services, llm=TestDoubleLLM(mutate={"frontend": bad}))
        with pytest.raises(ContractViolation, match="BillingChartData"):
            await FrontendAgent(s.llm, ("Finn", "f@x.io")).run(tasks[1], contract)
        redecl = lambda files: {**files, "web/components/BillingUsage.tsx":
                                'import type { UsageReport } from "@contracts/demo_types";\ninterface BillingTier { id: string }\n'}
        s2, _, c2, t2 = await _planned(services, llm=TestDoubleLLM(mutate={"frontend": redecl}))
        with pytest.raises(ContractViolation, match="redeclares"):
            await FrontendAgent(s2.llm, ("Finn", "f@x.io")).run(t2[1], c2)
    run(go())


# ───────────── H: approval gate ─────────────
async def _dispatched(services, **kw):
    s, _, contract, tasks = await _planned(services, **kw)
    report = await dispatch_feature(feature_id="feat_test01", contract=contract, tasks=tasks,
                                    agents=s.make_agents(), github=s.get_github(), state=s.state, bus=s.bus)
    return s, report


def test_H_every_pr_has_an_approval_request(services, seeded_contracts):
    async def go():
        s, report = await _dispatched(services)
        appr = await s.state.list_approvals()
        gh = s.get_github()
        assert len(appr) == len(gh.prs) == 3 and all(a.status == ApprovalStatus.pending for a in appr)
        assert gh.merges == []                                  # nothing merged by dispatch
        authors = {c.author_email for c in gh.commits}
        assert authors == {"ada@example.com", "finn@example.com", "quinn@example.com"}   # 3 distinct identities
    run(go())


def test_H_reject_leaves_main_unchanged_and_never_merges(services, seeded_contracts):
    async def go():
        s, _ = await _dispatched(services)
        gh = s.get_github()
        main_before = await gh.head_sha("main")
        for a in await s.state.list_approvals():
            r = await reject(a.id, "not good enough", s.state, s.bus, gh)
            assert r.status == ApprovalStatus.rejected and r.merge_sha is None
        assert gh.merges == [] and await gh.head_sha("main") == main_before and len(gh.closed) == 3
        a0 = (await s.state.list_approvals())[0]
        with pytest.raises(ApprovalError):                   # a rejected request can no longer be approved
            await approve(a0.id, s.state, s.bus, gh)
        assert gh.merges == []
    run(go())


def test_H_approve_is_the_only_merge_path(services, seeded_contracts):
    async def go():
        s, _ = await _dispatched(services)
        gh = s.get_github()
        a = (await s.state.list_approvals())[0]
        merged = await approve(a.id, s.state, s.bus, gh)
        assert merged.status == ApprovalStatus.merged and gh.merges == [a.pr.number]
        assert await gh.head_sha("main") == merged.merge_sha
        with pytest.raises(ApprovalError):
            await approve(a.id, s.state, s.bus, gh)          # double-approve refused
        assert len(gh.merges) == 1
    run(go())


def test_H_real_github_client_refuses_to_merge_unapproved():
    repo = MagicMock()
    gh = GitHubClient(make_settings(__import__("pathlib").Path(".")), gh=MagicMock(get_repo=MagicMock(return_value=repo)))
    from models import ApprovalRequest, CommitRef, PRRef
    ap = ApprovalRequest(feature_id="f", task_id="t", role=AgentRole.qa, pr=PRRef(number=7, url="u", branch="b"),
                         commit=CommitRef(branch="b", sha="s", author_name="n", author_email="e", files=[]))
    for status in (ApprovalStatus.pending, ApprovalStatus.rejected):
        ap.status = status
        with pytest.raises(MergeNotApproved):
            run(gh.merge_pr(ap))
    repo.get_pull.assert_not_called()                        # not even looked up


def test_real_github_client_commits_under_mapped_author_with_blobs():
    repo = MagicMock()
    repo.get_git_ref.return_value.object.sha = "parent"
    repo.create_git_commit.return_value.sha = "newsha"
    repo.create_git_blob.side_effect = lambda content, enc: MagicMock(sha="blob" + content)
    gh = GitHubClient(make_settings(__import__("pathlib").Path(".")), gh=MagicMock(get_repo=MagicMock(return_value=repo)))
    ref = run(gh.commit_files("kode/x/backend", {"api/a.py": "a", "api/b.py": "b"}, "msg", "Ada Backend", "ada@example.com"))
    assert repo.create_git_blob.call_count == 2
    kw = repo.create_git_commit.call_args.kwargs
    assert kw["author"]._identity["email"] == "ada@example.com" and kw["committer"]._identity["name"] == "Ada Backend"
    assert ref.sha == "newsha" and ref.files == ["api/a.py", "api/b.py"]


# ───────────── review against the frozen contract ─────────────
def test_review_clean_on_good_output_and_flags_drift(services, seeded_contracts):
    async def go():
        s, _, contract, tasks = await _planned(services)
        from tests.fakes import _files_for
        good = _files_for("backend", [])
        assert review(AgentRole.backend, good, contract).errors == 0
        drift = dict(good)
        drift["api/routes/billing.py"] = good["api/routes/billing.py"].replace('"/billing/usage"', '"/billing/usages"')
        drift["api/schemas/billing.py"] = good["api/schemas/billing.py"].replace("    overage_cents_per_1k: int\n    created_at", "    created_at")
        rep = review(AgentRole.backend, drift, contract)
        msgs = " | ".join(f.message for f in rep.findings)
        assert "GET /billing/usage" in msgs and "/billing/usages" in msgs and "overage_cents_per_1k" in msgs
        fe = _files_for("frontend", [])
        assert review(AgentRole.frontend, fe, contract).errors == 0
        fe["web/lib/api/billingClient.ts"] += '\nfetch("/billing/invoices");\n'
        assert review(AgentRole.frontend, fe, contract).errors == 1
    run(go())


def test_missing_llm_key_fails_loudly(services):
    async def go():
        from llm import LiteLLMClient
        s = make_settings(__import__("pathlib").Path("."), anthropic_api_key=None)
        with pytest.raises(MissingConfigError, match="ANTHROPIC_API_KEY"):
            await LiteLLMClient(s).complete("sys", "user")
    run(go())


def test_review_understands_one_line_pydantic_classes(services, seeded_contracts):
    """Regression from the real GitHub run: a real model wrote `class X(BaseModel): a: str; b: int`."""
    async def go():
        s, _, contract, _ = await _planned(services)
        from tests.fakes import ROUTES_PY, SCHEMAS_PY
        import re as _re
        one_line = _re.sub(r"class (\w+)\(BaseModel\):\n((?:    \w+: [^\n]+\n)+)",
                           lambda m: f"class {m.group(1)}(BaseModel): " + "; ".join(x.strip() for x in m.group(2).strip().splitlines()) + "\n",
                           SCHEMAS_PY)
        assert "class BillingTier(BaseModel): id: str;" in one_line
        rep = review(AgentRole.backend, {"api/routes/billing.py": ROUTES_PY, "api/schemas/billing.py": one_line}, contract)
        assert rep.errors == 0, rep.findings
    run(go())
