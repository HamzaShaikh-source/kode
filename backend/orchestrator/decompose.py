"""contract + analysis -> Task objects with explicit, guard-validated `files_owned`."""
from __future__ import annotations

from typing import List

from models import AgentRole, Contract, FeatureAnalysis, Task
from orchestrator.guard import normalize, validate_ownership

ORDER = [AgentRole.backend, AgentRole.frontend, AgentRole.qa]


def decompose(feature_id: str, analysis: FeatureAnalysis, contract: Contract) -> List[Task]:
    by_role = {t.role: t for t in analysis.tasks}
    tasks: List[Task] = []
    for role in ORDER:
        spec = by_role[role]
        tasks.append(Task(
            feature_id=feature_id, role=role, title=spec.title,
            description=spec.description,
            files_owned=[normalize(f) for f in spec.files_owned],
            contract_sha256=contract.sha256,
        ))
    validate_ownership(tasks)          # raises ScopeViolation on overlap / out-of-domain
    return tasks
