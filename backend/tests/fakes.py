"""TEST DOUBLES - used ONLY by the test suite. The product has no code path to these (rule 3).

TestDoubleLLM stands in for the network so tests are deterministic and free; it returns the kind
of JSON a real model must return. It proves orchestration/guard/pipeline behaviour, NOT model quality.
TestDoubleGitHub records calls instead of hitting GitHub.
"""
import ast
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

from llm import LLMResult
from models import ApprovalRequest, ApprovalStatus, CommitRef, PRRef

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

PLAN = {
    "summary": "Enterprise Stripe billing tier with usage-based rate limits and usage graphs.",
    "required_endpoints": [{"method": "POST", "path": "/billing/tiers"}, {"method": "GET", "path": "/billing/usage"},
                           {"method": "POST", "path": "/stripe/webhook"}],
    "required_schemas": ["BillingTier", "UsageRecord", "CreateBillingTierRequest", "UsageReport"],
    "tasks": [
        {"role": "backend", "title": "Billing API", "description": "Routes, schemas, migration.",
         "files_owned": ["api/routes/billing.py", "api/schemas/billing.py", "db/migrations/*"]},
        {"role": "frontend", "title": "Usage graphs UI", "description": "Component, page, typed client.",
         "files_owned": ["web/components/BillingUsage.tsx", "web/pages/settings/billing.tsx", "web/lib/api/billingClient.ts"]},
        {"role": "qa", "title": "Billing tests", "description": "e2e + contract tests.",
         "files_owned": ["tests/e2e/billing.spec.ts", "tests/contract/test_billing_contract.py"]},
    ],
}

SCHEMAS_PY = '''from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class UsageMetric(str, Enum):
    api_requests = "api_requests"
    seats = "seats"
    storage_gb = "storage_gb"


class CreateBillingTierRequest(BaseModel):
    name: str
    monthly_price_cents: int
    rate_limit_per_minute: int
    included_requests: int
    overage_cents_per_1k: int


class BillingTier(BaseModel):
    id: str
    name: str
    stripe_price_id: str
    monthly_price_cents: int
    rate_limit_per_minute: int
    included_requests: int
    overage_cents_per_1k: int
    created_at: datetime


class UsageRecord(BaseModel):
    id: str
    customer_id: str
    tier_id: str
    metric: UsageMetric
    quantity: int
    period_start: datetime
    period_end: datetime
    recorded_at: datetime


class UsageReport(BaseModel):
    customer_id: str
    tier: BillingTier
    records: List[UsageRecord]
    total_quantity: int
    limit_exceeded: bool


class StripeWebhookEvent(BaseModel):
    id: str
    type: str
    created: int
    data: Dict[str, Any]


class WebhookAck(BaseModel):
    received: bool


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
'''

ROUTES_PY = '''from fastapi import APIRouter
from api.schemas.billing import BillingTier, CreateBillingTierRequest, StripeWebhookEvent, UsageReport, WebhookAck

router = APIRouter()


@router.post("/billing/tiers", status_code=201, response_model=BillingTier)
async def create_tier(body: CreateBillingTierRequest): ...


@router.get("/billing/usage", response_model=UsageReport)
async def get_usage(customer_id: str, period: str = "current"): ...


@router.post("/stripe/webhook", response_model=WebhookAck)
async def stripe_webhook(event: StripeWebhookEvent): ...
'''

CLIENT_TS = '''import type { BillingTier, CreateBillingTierRequest, UsageReport } from "@contracts/demo_types";

export async function createBillingTier(body: CreateBillingTierRequest): Promise<BillingTier> {
  const r = await fetch("/billing/tiers", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

export async function getBillingUsage(customerId: string, period: "current" | "previous" = "current"): Promise<UsageReport> {
  const r = await fetch(`/billing/usage?customer_id=${encodeURIComponent(customerId)}&period=${period}`);
  return r.json();
}
'''

COMPONENT_TSX = '''import type { UsageRecord, UsageReport } from "@contracts/demo_types";

export function BillingUsage({ report }: { report: UsageReport }) {
  const max = Math.max(1, ...report.records.map((r: UsageRecord) => r.quantity));
  return (<svg role="img" aria-label="usage graph">{report.records.map((r: UsageRecord, i: number) =>
    <rect key={r.id} x={i * 12} y={100 - (r.quantity / max) * 100} width={10} height={(r.quantity / max) * 100} />)}</svg>);
}
'''

PAGE_TSX = '''import type { BillingTier } from "@contracts/demo_types";
import { getBillingUsage } from "../../../lib/api/billingClient";
import { BillingUsage } from "../../../components/BillingUsage";

export default function BillingPage({ tier }: { tier: BillingTier }) { return <BillingUsage report={undefined as never} />; }
'''

E2E_TS = 'import { test, expect } from "@playwright/test";\ntest("usage graph renders", async ({ page }) => { await page.goto("/settings/billing"); await expect(page.getByRole("img", { name: "usage graph" })).toBeVisible(); });\n'

CONTRACT_PY = '''import yaml
from pathlib import Path

SPEC = yaml.safe_load((Path(__file__).parents[2] / "contracts" / "demo_openapi.yaml").read_text())


def test_billing_paths_in_contract():
    for p in ("/billing/tiers", "/billing/usage", "/stripe/webhook"):
        assert p in SPEC["paths"]
'''


def _files_for(role: str, owned: List[str]) -> Dict[str, str]:
    if role == "backend":
        return {"api/routes/billing.py": ROUTES_PY, "api/schemas/billing.py": SCHEMAS_PY,
                "db/migrations/001_billing.sql": "CREATE TABLE billing_tiers (id TEXT PRIMARY KEY);\n"}
    if role == "frontend":
        return {"web/components/BillingUsage.tsx": COMPONENT_TSX, "web/pages/settings/billing.tsx": PAGE_TSX,
                "web/lib/api/billingClient.ts": CLIENT_TS}
    return {"tests/e2e/billing.spec.ts": E2E_TS, "tests/contract/test_billing_contract.py": CONTRACT_PY}


class TestDoubleLLM:
    __test__ = False

    def __init__(self, latency: float = 0.0, extra_files: Optional[Dict[str, Dict[str, str]]] = None,
                 mutate: Optional[Dict[str, callable]] = None):
        self.latency = latency
        self.extra_files = extra_files or {}      # role -> {path: content} injected to simulate a rogue agent
        self.mutate = mutate or {}                # role -> fn(files)->files
        self.calls: List[str] = []

    async def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> LLMResult:
        await asyncio.sleep(self.latency)
        if "planning stage of K.O.D.E." in system:
            self.calls.append("planner")
            return LLMResult(json.dumps(PLAN), 100, 200)
        if "contract author" in system:
            self.calls.append("contract")
            return LLMResult(json.dumps({"openapi_yaml": (CONTRACTS / "demo_openapi.yaml").read_text(encoding="utf-8"),
                                         "ts_types": (CONTRACTS / "demo_types.ts").read_text(encoding="utf-8")}), 100, 900)
        role = "backend" if "BACKEND agent" in system else "frontend" if "FRONTEND agent" in system else "qa"
        self.calls.append(role)
        owned = ast.literal_eval(user.split("FILES YOU OWN (write nothing else): ")[1].split("\n")[0])
        files = _files_for(role, owned)
        files.update(self.extra_files.get(role, {}))
        if role in self.mutate:
            files = self.mutate[role](files)
        return LLMResult(json.dumps({"files": [{"path": p, "content": c} for p, c in files.items()],
                                     "notes": f"{role} done"}), 300, 1200)


class TestDoubleGitHub:
    """Records every call. `main_sha` only changes through an approved merge."""
    __test__ = False

    def __init__(self, latency: float = 0.0):
        self.latency = latency
        self.main_sha = "main000"
        self.branches: Dict[str, str] = {}
        self.commits: List[CommitRef] = []
        self.prs: List[PRRef] = []
        self.merges: List[int] = []
        self.closed: List[int] = []
        self.comments: List[tuple] = []

    async def head_sha(self, ref): return self.main_sha if ref == "main" else self.branches[ref]

    async def create_branch(self, name):
        await asyncio.sleep(self.latency)
        self.branches[name] = self.main_sha
        return self.main_sha

    async def commit_files(self, branch, files, message, author_name, author_email):
        await asyncio.sleep(self.latency)
        c = CommitRef(branch=branch, sha=f"sha{len(self.commits)+1:03d}", author_name=author_name,
                      author_email=author_email, files=sorted(files))
        self.branches[branch] = c.sha
        self.commits.append(c)
        return c

    async def open_pr(self, branch, title, body):
        await asyncio.sleep(self.latency)
        pr = PRRef(number=len(self.prs) + 1, url=f"https://github.test/org/repo/pull/{len(self.prs)+1}", branch=branch)
        self.prs.append(pr)
        return pr

    async def comment(self, pr_number, body): self.comments.append((pr_number, body))

    async def merge_pr(self, approval: ApprovalRequest) -> str:
        if approval.status != ApprovalStatus.approved:
            raise PermissionError("merge refused: not approved")
        self.merges.append(approval.pr.number)
        self.main_sha = f"merged{approval.pr.number}"
        return self.main_sha

    async def close_pr(self, pr_number, reason): self.closed.append(pr_number)
