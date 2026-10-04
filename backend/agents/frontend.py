"""Frontend persona: components, routes, hooks, typed clients.

Extra guarantee: code must import the GENERATED contract types - never invented ones (acceptance F).
"""
import re
from typing import Dict

from agents.base import ContractViolation, LLMAgent
from agents.prompts import FRONTEND_SYSTEM
from models import AgentRole, Contract, Task
from orchestrator.contract import ts_exports

IMPORT_RE = re.compile(r"import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+[\"']([^\"']+)[\"']")


def contract_import_report(files: Dict[str, str], contract: Contract) -> Dict[str, set]:
    """Return {file: names imported from the contract types module}; raises ContractViolation
    if a file imports a name the contract does not export, or redeclares a contract type."""
    exported = set(ts_exports(contract.ts_types))
    module_suffix = f"@contracts/{contract.name}_types"
    report: Dict[str, set] = {}
    for path, src in files.items():
        if not path.endswith((".ts", ".tsx")):
            continue
        names: set = set()
        for m in IMPORT_RE.finditer(src):
            if m.group(2) == module_suffix:
                for part in m.group(1).split(","):
                    n = part.strip().split(" as ")[0].replace("type ", "").strip()
                    if n:
                        names.add(n)
        invented = sorted(n for n in names if n not in exported)
        if invented:
            raise ContractViolation(f"{path} imports types not exported by the contract: {invented}")
        redeclared = sorted(n for n in exported
                            if re.search(rf"\b(?:interface|type|enum)\s+{n}\b", src))
        if redeclared:
            raise ContractViolation(f"{path} redeclares contract types instead of importing them: {redeclared}")
        report[path] = names
    return report


class FrontendAgent(LLMAgent):
    role = AgentRole.frontend
    persona = "React/TypeScript engineer: components, routes, hooks, typed API clients"
    system_prompt = FRONTEND_SYSTEM
    capabilities = ["react", "typescript", "typed-clients", "charts"]

    def validate_output(self, task: Task, contract: Contract, files: Dict[str, str]) -> None:
        report = contract_import_report(files, contract)
        if not any(report.values()):
            raise ContractViolation(
                f'no file imports contract types; use: import type {{ ... }} from "@contracts/{contract.name}_types"')
        client = [p for p in files if "api/" in p and p.endswith((".ts", ".tsx"))]
        for p in client:
            if not report.get(p):
                raise ContractViolation(f"API client {p} must import its types from @contracts/{contract.name}_types")
