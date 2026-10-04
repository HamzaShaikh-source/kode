"""prompt -> FeatureAnalysis via a real LLM call (LiteLLM). No canned plans."""
from __future__ import annotations

from typing import List, Tuple

from agents.prompts import PLANNER_SYSTEM
from llm import LLMClient, LLMResult, complete_model
from models import AgentRole, FeatureAnalysis


def _check_plan(a: FeatureAnalysis) -> None:
    roles = sorted(t.role.value for t in a.tasks)
    if roles != sorted(r.value for r in AgentRole):
        raise ValueError(f"plan must contain exactly one task each for backend, frontend, qa; got {roles}")
    if not a.required_endpoints:
        raise ValueError("plan lists no required_endpoints")
    for t in a.tasks:
        if not t.files_owned:
            raise ValueError(f"{t.role.value} task has empty files_owned")


async def plan_feature(llm: LLMClient, prompt: str, contract_hint: str = "") -> Tuple[FeatureAnalysis, List[LLMResult]]:
    if not prompt or len(prompt.strip()) < 10:
        raise ValueError("feature prompt is too short to plan")
    return await complete_model(llm, PLANNER_SYSTEM, f"Feature request:\n{prompt.strip()}" + (f"\n\n{contract_hint}" if contract_hint else ""),
                                FeatureAnalysis, validator=_check_plan)
