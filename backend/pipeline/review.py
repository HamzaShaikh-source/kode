"""Diff implementation against the FROZEN contract and flag signature mismatches.

Honest scope: this is deterministic regex/static analysis (no AST, no type-checker, no execution).
It catches: missing/extra routes, Pydantic field drift, API paths used by frontend/tests that are not
in the contract, and invented TS type imports. Findings are posted as a PR review comment.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set

from pydantic import BaseModel

from models import AgentRole, Contract
from orchestrator.contract import parse_spec

ROUTE_RE = re.compile(r"@\s*\w+\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"']")
PREFIX_RE = re.compile(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']*)[\"']")
CLASS_RE = re.compile(r"^class\s+(\w+)\(.*BaseModel.*\)\s*:", re.M)
STR_PATH_RE = re.compile(r"[\"'`](/[A-Za-z0-9_\-/${}.:]+)(?:\?[^\"'`]*)?[\"'`]")


class Finding(BaseModel):
    severity: str          # error | warning
    message: str


class ReviewReport(BaseModel):
    role: AgentRole
    findings: List[Finding]

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    def markdown(self, contract_sha: str) -> str:
        head = f"### K.O.D.E. contract review ({self.role.value})\nFrozen contract `{contract_sha[:12]}`\n\n"
        if not self.findings:
            return head + "No signature mismatches found by static analysis."
        return head + "\n".join(f"- **{f.severity.upper()}** {f.message}" for f in self.findings)

    def summary(self) -> str:
        w = len(self.findings) - self.errors
        return f"{self.errors} error(s), {w} warning(s)"


def _norm(p: str) -> str:
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}/]*\}", "{}", p)
    return p.rstrip("/") or "/"


def _contract_paths(spec: dict) -> Dict[str, Set[str]]:
    return {_norm(p): {m.upper() for m in item if m in ("get", "post", "put", "patch", "delete")}
            for p, item in spec["paths"].items()}


def _backend_routes(files: Dict[str, str]):
    routes = set()
    for path, src in files.items():
        if not path.endswith(".py"):
            continue
        prefixes = PREFIX_RE.findall(src)
        prefix = prefixes[0] if prefixes else ""
        for m, p in ROUTE_RE.findall(src):
            routes.add((m.upper(), _norm(prefix + p)))
    return routes


def _model_fields(src: str, cls: str) -> Set[str]:
    """Field names of `class cls(...BaseModel...)`, for both block bodies and one-line bodies
    (`class X(BaseModel): a: str; b: int`)."""
    m = re.search(rf"^class\s+{cls}\(.*?\):[ \t]*([^\n]*)\n?((?:[ \t]+.*\n?|\n)*)", src, re.M)
    if not m:
        return set()
    inline, block = m.group(1), m.group(2)
    fields = set(re.findall(r"^[ \t]+(\w+)\s*:", block, re.M))
    if inline.strip() and not inline.strip().startswith("#"):
        for part in inline.split(";"):
            f = re.match(r"\s*(\w+)\s*:", part)
            if f and f.group(1) not in ("pass", "return"):
                fields.add(f.group(1))
    return fields


def review(role: AgentRole, files: Dict[str, str], contract: Contract) -> ReviewReport:
    spec = parse_spec(contract.openapi_yaml)
    cpaths = _contract_paths(spec)
    out: List[Finding] = []
    roots = {p.split("/")[1] for p in cpaths if p.count("/") >= 1 and p != "/"}

    if role == AgentRole.backend:
        routes = _backend_routes(files)
        for path, methods in cpaths.items():
            for m in methods:
                if (m, path) not in routes:
                    out.append(Finding(severity="error", message=f"contract endpoint `{m} {path}` is not implemented"))
        for m, p in sorted(routes - {(m, p) for p, ms in cpaths.items() for m in ms}):
            out.append(Finding(severity="error", message=f"route `{m} {p}` is not in the frozen contract"))
        for name, schema in spec["components"]["schemas"].items():
            want = set(schema.get("properties", {}))
            for src in files.values():
                if re.search(rf"^class\s+{name}\(", src, re.M):
                    have = _model_fields(src, name)
                    if want - have:
                        out.append(Finding(severity="error", message=f"`{name}` missing contract fields {sorted(want - have)}"))
                    if have - want:
                        out.append(Finding(severity="warning", message=f"`{name}` has fields not in contract {sorted(have - want)}"))
    else:  # frontend / qa: any API path they reference must exist in the contract
        for path, src in files.items():
            if not path.endswith((".ts", ".tsx", ".py")):
                continue
            for lit in set(STR_PATH_RE.findall(src)):
                n = _norm(lit)
                if n.split("/")[1:2] and n.split("/")[1] in roots and n not in cpaths:
                    out.append(Finding(severity="error", message=f"`{path}` references `{lit}` which is not a contract path"))
    return ReviewReport(role=role, findings=out)
