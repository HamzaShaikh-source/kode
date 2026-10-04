"""REST + SSE. Shapes for billing live in contracts/demo_openapi.yaml (served to the UI later);
this router is the orchestration surface: features, contract, dispatch, events, approvals."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import MissingConfigError
from llm import LLMOutputError
from models import ApprovalStatus, AgentRole, EventType, new_id
from orchestrator.contract import (ContractConflict, ContractDrift, assert_frozen, baseline_hint,
                                    freeze_contract)
from orchestrator.minimal import ADDENDUM, shrink_tasks
from orchestrator.decompose import decompose
from orchestrator.dispatch import ApprovalError, approve, dispatch_feature, reject
from orchestrator.guard import ScopeViolation
from orchestrator.planner import plan_feature

router = APIRouter()


class FeatureRequest(BaseModel):
    prompt: str
    contract_name: str = "demo"


class RejectRequest(BaseModel):
    reason: str = ""


def svc(request: Request):
    return request.app.state.services


def _http(e: Exception) -> HTTPException:
    if isinstance(e, MissingConfigError):
        return HTTPException(503, f"configuration missing: {e}")
    if isinstance(e, ScopeViolation):
        return HTTPException(422, f"ScopeViolation: {e}")
    if isinstance(e, (ContractConflict, ContractDrift)):
        return HTTPException(409, str(e))
    if isinstance(e, LLMOutputError):
        return HTTPException(502, f"LLM output unusable: {e}")
    if isinstance(e, ApprovalError):
        return HTTPException(409, str(e))
    return HTTPException(500, f"{type(e).__name__}: {e}")


@router.post("/features")
async def create_feature(body: FeatureRequest, request: Request):
    s = svc(request)
    fid = new_id("feat")
    try:
        s.settings.require_llm()
        await s.bus.publish(EventType.THINKING, "planner analysing feature request", feature_id=fid)
        minimal = s.settings.kode_minimal
        analysis, _ = await plan_feature(s.llm, body.prompt + (ADDENDUM if minimal else ""),
                                         baseline_hint(s.settings.contracts_dir, body.contract_name))
        name = body.contract_name
        try:
            contract, _ = await freeze_contract(s.llm, fid, name, analysis, s.settings.contracts_dir)
        except ContractConflict:                       # frozen contracts are never overwritten: this feature gets its own
            name = f"{body.contract_name}-{fid[-4:]}"
            await s.bus.publish(EventType.THINKING, f"existing contract does not cover this plan; drafting new contract {name!r}",
                                feature_id=fid, data={"stage": "contract_fallback"})
            contract, _ = await freeze_contract(s.llm, fid, name, analysis, s.settings.contracts_dir)
        tasks = decompose(fid, analysis, contract)
        if minimal:
            shrink_tasks(tasks)
        await s.state.save_feature(fid, body.prompt, name, analysis, contract, tasks)
    except ScopeViolation as e:
        await s.bus.publish(EventType.BLOCKED, f"decomposition rejected: {e}", feature_id=fid,
                            data={"reason": "scope_violation"})
        raise _http(e)
    except Exception as e:
        raise _http(e)
    await s.bus.publish(EventType.THINKING, f"contract frozen {contract.sha256[:12]} ({contract.source}); "
                        f"{len(tasks)} disjoint tasks", feature_id=fid,
                        data={"stage": "contract_frozen", "sha256": contract.sha256})
    return {
        "feature_id": fid, "analysis": analysis,
        "contract": {"name": contract.name, "sha256": contract.sha256, "source": contract.source,
                     "openapi_path": contract.openapi_path, "types_path": contract.types_path,
                     "endpoints": contract.endpoints, "schemas": contract.schema_names},
        "tasks": tasks,
    }


@router.get("/features/{feature_id}/contract")
async def get_contract(feature_id: str, request: Request):
    f = await svc(request).state.get_feature(feature_id)
    if not f:
        raise HTTPException(404, "unknown feature")
    c = f["contract"]
    try:
        assert_frozen(c)
    except Exception as e:
        raise _http(e)
    return {"name": c.name, "sha256": c.sha256, "frozen_at": c.frozen_at, "source": c.source,
            "openapi_yaml": c.openapi_yaml, "ts_types": c.ts_types}


@router.post("/features/{feature_id}/dispatch")
async def dispatch(feature_id: str, request: Request):
    s = svc(request)
    f = await s.state.get_feature(feature_id)
    if not f:
        raise HTTPException(404, "unknown feature")
    try:
        tasks = await s.state.get_tasks(feature_id)
        agents = s.make_agents()
        github = s.get_github()
        report = await dispatch_feature(feature_id=feature_id, contract=f["contract"], tasks=tasks, agents=agents,
                                        github=github, state=s.state, bus=s.bus)
    except Exception as e:
        raise _http(e)
    return report


@router.get("/events")
async def events(request: Request, feature_id: Optional[str] = None, after: int = 0, replay: bool = True,
                 last_event_id: Optional[str] = Query(None, alias="lastEventId")):
    s = svc(request)
    hdr = request.headers.get("last-event-id") or last_event_id
    start = int(hdr) if hdr and hdr.isdigit() else after
    return EventSourceResponse(s.bus.stream(after_seq=start, feature_id=feature_id, replay=replay))


@router.get("/approvals")
async def list_approvals(request: Request, status: Optional[ApprovalStatus] = None):
    return await svc(request).state.list_approvals(status)


@router.post("/approvals/{approval_id}/approve")
async def approve_route(approval_id: str, request: Request):
    s = svc(request)
    try:
        return await approve(approval_id, s.state, s.bus, s.get_github())
    except Exception as e:
        raise _http(e)


@router.post("/approvals/{approval_id}/reject")
async def reject_route(approval_id: str, request: Request, body: RejectRequest = RejectRequest()):
    s = svc(request)
    try:
        gh = None
        try:
            gh = s.get_github()
        except MissingConfigError:
            pass                                       # rejecting must work even if GitHub is unreachable
        return await reject(approval_id, body.reason, s.state, s.bus, gh or _NullGitHub())
    except Exception as e:
        raise _http(e)


class _NullGitHub:
    """Only used by reject when GitHub is unconfigured: it can close nothing and can NEVER merge."""
    async def close_pr(self, *a, **k):
        return None
