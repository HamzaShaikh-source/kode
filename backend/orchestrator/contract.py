"""Generate + FREEZE the OpenAPI contract and TS types, writing them to contracts/.

Frozen = a sha256 lock file written next to the contract. After the freeze, any change to either
file is detected by assert_frozen() (ContractDrift) and the contract can never be silently re-drafted.

Source of the contract for a given name:
  1. already frozen on disk (lock matches files)   -> reuse (idempotent)
  2. a hand-authored baseline exists on disk       -> use it IFF it covers the planner's required
                                                      endpoints/schemas (else ContractConflict)
  3. nothing on disk                               -> the LLM drafts it, then it is validated
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import List, Tuple

import yaml
from pydantic import BaseModel

from agents.prompts import CONTRACT_SYSTEM
from llm import LLMClient, LLMResult, complete_model
from models import Contract, Endpoint, FeatureAnalysis, now
from openapi_spec_validator import validate as validate_spec
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

METHODS = {"get", "post", "put", "patch", "delete"}


class ContractConflict(RuntimeError):
    """Existing contract on disk does not cover the plan (never overwritten)."""


class ContractDrift(RuntimeError):
    """A frozen contract file was modified after the freeze."""


class _Draft(BaseModel):
    openapi_yaml: str
    ts_types: str


def _paths(contracts_dir: Path, name: str) -> Tuple[Path, Path, Path]:
    return (contracts_dir / f"{name}_openapi.yaml", contracts_dir / f"{name}_types.ts",
            contracts_dir / f"{name}_openapi.lock.json")


def contract_hash(openapi_text: str, ts_text: str) -> str:
    h = hashlib.sha256()
    h.update(openapi_text.replace("\r\n", "\n").encode("utf-8"))
    h.update(b"\0")
    h.update(ts_text.replace("\r\n", "\n").encode("utf-8"))
    return h.hexdigest()


def parse_spec(openapi_text: str) -> dict:
    spec = yaml.safe_load(openapi_text)
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI document is not a mapping")
    try:
        validate_spec(spec)
    except OpenAPIValidationError as e:
        raise ValueError(f"invalid OpenAPI: {e}") from e
    return spec


def spec_endpoints(spec: dict) -> List[Endpoint]:
    return [Endpoint(method=m.upper(), path=p)
            for p, item in spec.get("paths", {}).items() for m in item if m in METHODS]


def spec_schemas(spec: dict) -> List[str]:
    return list(spec.get("components", {}).get("schemas", {}).keys())


def ts_exports(ts_text: str) -> List[str]:
    return re.findall(r"export\s+(?:interface|type|enum|const)\s+([A-Za-z0-9_]+)", ts_text)


def check_ts_matches_spec(spec: dict, ts_text: str) -> None:
    exported = set(ts_exports(ts_text))
    missing = [s for s in spec_schemas(spec) if s not in exported]
    if missing:
        raise ValueError(f"TS types missing exports for schemas: {missing}")


def check_covers_plan(spec: dict, analysis: FeatureAnalysis) -> None:
    have = {(e.method.upper(), e.path.rstrip("/") or "/") for e in spec_endpoints(spec)}
    need = {(e.method.upper(), e.path.rstrip("/") or "/") for e in analysis.required_endpoints}
    miss = sorted(need - have)
    if miss:
        raise ValueError(f"contract lacks planned endpoints: {miss}")
    schemas = set(spec_schemas(spec))
    smiss = sorted(set(analysis.required_schemas) - schemas)
    if smiss:
        raise ValueError(f"contract lacks planned schemas: {smiss}")


def baseline_hint(contracts_dir: Path, name: str) -> str:
    """If a contract with this name already exists on disk, tell the planner what it covers so the plan reuses it."""
    oa_path, _, _ = _paths(contracts_dir, name)
    if not oa_path.exists():
        return ""
    try:
        spec = parse_spec(oa_path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    eps = ", ".join(f"{e.method} {e.path}" for e in spec_endpoints(spec))
    return (f"A frozen contract named {name!r} already exists with endpoints [{eps}] and schemas "
            f"{spec_schemas(spec)}. If this request can be served by it, use EXACTLY those endpoints and "
            "required_schemas; only add new ones if the request truly needs them.")


def _write_lock(lock_path: Path, name: str, feature_id: str, sha: str, source: str) -> None:
    lock_path.write_text(json.dumps(
        {"name": name, "feature_id": feature_id, "sha256": sha, "source": source,
         "frozen_at": now().isoformat(), "algorithm": "sha256(openapi + 0x00 + ts)"}, indent=2), encoding="utf-8")


def _build(feature_id, name, oa_path, ts_path, oa_text, ts_text, source) -> Contract:
    spec = parse_spec(oa_text)
    return Contract(feature_id=feature_id, name=name, openapi_yaml=oa_text, ts_types=ts_text,
                    sha256=contract_hash(oa_text, ts_text), source=source,
                    openapi_path=str(oa_path), types_path=str(ts_path),
                    endpoints=spec_endpoints(spec), schema_names=spec_schemas(spec))


def assert_frozen(contract: Contract) -> None:
    """Re-hash the files on disk; raise ContractDrift if they differ from the frozen hash."""
    oa, ts = Path(contract.openapi_path), Path(contract.types_path)
    cur = contract_hash(oa.read_text(encoding="utf-8"), ts.read_text(encoding="utf-8"))
    if cur != contract.sha256:
        raise ContractDrift(f"contract {contract.name} changed after freeze: {contract.sha256[:12]} -> {cur[:12]}")


async def freeze_contract(llm: LLMClient, feature_id: str, name: str, analysis: FeatureAnalysis,
                          contracts_dir: Path) -> Tuple[Contract, List[LLMResult]]:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    oa_path, ts_path, lock_path = _paths(contracts_dir, name)
    usage: List[LLMResult] = []

    if oa_path.exists() and ts_path.exists():
        oa_text, ts_text = oa_path.read_text(encoding="utf-8"), ts_path.read_text(encoding="utf-8")
        try:
            spec = parse_spec(oa_text)
            check_ts_matches_spec(spec, ts_text)
            check_covers_plan(spec, analysis)
        except ValueError as e:
            raise ContractConflict(
                f"contract {name!r} already exists on disk and does not satisfy this plan ({e}). "
                "Frozen contracts are never overwritten; choose a new contract_name.") from e
        sha = contract_hash(oa_text, ts_text)
        if lock_path.exists():
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if lock["sha256"] != sha:
                raise ContractDrift(f"{name}: files on disk do not match the freeze lock")
            source = "frozen-on-disk"
        else:
            source = "baseline-on-disk"
            _write_lock(lock_path, name, feature_id, sha, source)
        return _build(feature_id, name, oa_path, ts_path, oa_text, ts_text, source), usage

    user = (f"Feature analysis:\n{analysis.model_dump_json(indent=2)}\n\n"
            f"Contract name: {name}. Draft the OpenAPI + TS types now.")

    def check(d: _Draft) -> None:
        spec = parse_spec(d.openapi_yaml)
        check_ts_matches_spec(spec, d.ts_types)
        check_covers_plan(spec, analysis)

    draft, usage = await complete_model(llm, CONTRACT_SYSTEM, user, _Draft, max_tokens=12000, validator=check)
    oa_path.write_text(draft.openapi_yaml, encoding="utf-8")
    ts_path.write_text(draft.ts_types, encoding="utf-8")
    sha = contract_hash(draft.openapi_yaml, draft.ts_types)
    _write_lock(lock_path, name, feature_id, sha, "llm-drafted")
    return _build(feature_id, name, oa_path, ts_path, draft.openapi_yaml, draft.ts_types, "llm-drafted"), usage
