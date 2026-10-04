"""Worker interface + the shared LLM-backed implementation.

Worker lifecycle: discover() -> start() -> send_task(task, contract) -> receive() -> stop(),
with report_usage() available at any time. Every file an agent emits passes through
orchestrator.guard.GuardedWriter - the guard, not the prompt, is the isolation guarantee.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel

from llm import LLMClient, LLMOutputError, complete_model
from models import (AgentProfile, AgentResult, AgentRole, Contract, EventType, FileWrite, Task, TaskStatus,
                    UsageReport)
from orchestrator.contract import assert_frozen
from orchestrator.guard import GuardedWriter, ScopeViolation, _covers, _is_glob, normalize

Emit = Callable[..., Awaitable[None]]


class AgentTimeout(RuntimeError):
    """Agent exceeded its wall-clock budget (blocked loudly, never silently truncated)."""


class ContractViolation(RuntimeError):
    """Agent output does not conform to the frozen contract (after one corrective retry)."""


class AgentOutput(BaseModel):
    files: List[FileWrite]
    notes: str = ""


class Worker(ABC):
    role: AgentRole

    @abstractmethod
    async def discover(self) -> AgentProfile: ...
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def send_task(self, task: Task, contract: Contract) -> None: ...
    @abstractmethod
    async def receive(self) -> AgentResult: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def report_usage(self) -> UsageReport: ...


async def _noop(*a, **k) -> None:
    return None


class LLMAgent(Worker):
    """Concrete worker. Subclasses set role/persona/system prompt and may add validate_output()."""

    role: AgentRole
    persona: str = ""
    system_prompt: str = ""
    capabilities: List[str] = []

    def __init__(self, llm: LLMClient, owner: Tuple[str, str], emit: Optional[Emit] = None,
                 wall_budget_s: Optional[float] = None):
        self.llm, self.owner_name, self.owner_email = llm, owner[0], owner[1]
        self.wall_budget_s = wall_budget_s
        self.emit: Emit = emit or _noop
        self._usage = UsageReport(role=self.role)
        self._pending: Optional[Tuple[Task, Contract]] = None
        self._started = False

    # ── Worker interface ──
    async def discover(self) -> AgentProfile:
        return AgentProfile(role=self.role, persona=self.persona, owner_name=self.owner_name,
                            owner_email=self.owner_email, capabilities=self.capabilities)

    async def start(self) -> None:
        self._started = True

    async def send_task(self, task: Task, contract: Contract) -> None:
        if not self._started:
            raise RuntimeError("agent not started")
        if task.role != self.role:
            raise ValueError(f"{self.role.value} agent received a {task.role.value} task")
        self._pending = (task, contract)

    async def receive(self) -> AgentResult:
        if not self._pending:
            raise RuntimeError("no task sent")
        task, contract = self._pending
        self._pending = None
        try:
            return await asyncio.wait_for(self._execute(task, contract), self.wall_budget_s)
        except asyncio.TimeoutError:
            task.status = TaskStatus.blocked
            msg = f"{self.role.value} agent exceeded its {self.wall_budget_s:.0f}s wall budget"
            await self.emit(EventType.BLOCKED, msg, feature_id=task.feature_id, agent=self.role,
                            data={"task_id": task.id, "reason": "wall_budget"})
            raise AgentTimeout(msg) from None

    async def stop(self) -> None:
        self._started = False

    async def report_usage(self) -> UsageReport:
        return self._usage.model_copy()

    async def run(self, task: Task, contract: Contract) -> AgentResult:
        await self.start()
        await self.send_task(task, contract)
        try:
            return await self.receive()
        finally:
            await self.stop()

    # ── hooks ──
    def validate_output(self, task: Task, contract: Contract, files: Dict[str, str]) -> None:
        """Role-specific contract conformance. Raise ContractViolation to trigger one retry."""

    def build_user_prompt(self, task: Task, contract: Contract) -> str:
        return (
            f"TASK: {task.title}\n{task.description}\n\n"
            f"FILES YOU OWN (write nothing else): {task.files_owned}\n"
            f"CONTRACT NAME: {contract.name}   CONTRACT SHA256: {contract.sha256}\n\n"
            f"=== FROZEN OPENAPI ({contract.name}_openapi.yaml) ===\n{contract.openapi_yaml}\n\n"
            f"=== FROZEN TS TYPES ({contract.name}_types.ts) ===\n{contract.ts_types}\n"
        )

    # ── execution ──
    async def _execute(self, task: Task, contract: Contract) -> AgentResult:
        t0 = time.perf_counter()
        wall0 = time.time()
        task.status = TaskStatus.running
        rid = dict(feature_id=task.feature_id, agent=self.role)
        await self.emit(EventType.THINKING, f"{self.role.value} agent planning: {task.title}", **rid,
                        data={"task_id": task.id, "owner": self.owner_name})
        assert_frozen(contract)                                    # contract must be untouched
        system = self.system_prompt.replace("{CONTRACT_NAME}", contract.name)
        user = self.build_user_prompt(task, contract)
        feedback = ""
        for attempt in (1, 2):
            out, results = await complete_model(self.llm, system, user + feedback, AgentOutput, max_tokens=16000)
            for r in results:
                self._usage.llm_calls += 1
                self._usage.prompt_tokens += r.prompt_tokens
                self._usage.completion_tokens += r.completion_tokens
            writer = GuardedWriter(task)
            try:
                for f in out.files:
                    writer.write(f.path, f.content)                # <- ScopeViolation raised here
            except ScopeViolation as v:
                task.status = TaskStatus.blocked
                await self.emit(EventType.BLOCKED, f"{self.role.value} agent BLOCKED: {v}", **rid,
                                data={"task_id": task.id, "reason": "scope_violation", "path": v.path})
                raise
            problems = self._completeness(task, writer.files)
            try:
                if problems:
                    raise ContractViolation(problems)
                self.validate_output(task, contract, writer.files)
                break
            except ContractViolation as cv:
                if attempt == 2:
                    task.status = TaskStatus.blocked
                    await self.emit(EventType.BLOCKED, f"{self.role.value} agent BLOCKED: {cv}", **rid,
                                    data={"task_id": task.id, "reason": "contract_violation"})
                    raise
                feedback = f"\n\nYour previous output was rejected: {cv}\nFix it and return the full JSON again."
        for path in writer.files:
            await self.emit(EventType.WRITE_CODE, f"{self.role.value} wrote {path}", **rid,
                            data={"task_id": task.id, "path": path, "bytes": len(writer.files[path])})
        self._usage.elapsed_s += time.perf_counter() - t0
        return AgentResult(
            task_id=task.id, role=self.role,
            files=[FileWrite(path=p, content=c) for p, c in writer.files.items()],
            notes=out.notes, usage=await self.report_usage(), started_at=wall0, finished_at=time.time())

    @staticmethod
    def _completeness(task: Task, files: Dict[str, str]) -> str:
        """Every concrete owned file must be produced; every owned glob must match something."""
        missing = []
        for o in task.files_owned:
            o = normalize(o)
            if o.endswith("/"):
                continue
            if _is_glob(o):
                if not any(_covers(o, p) for p in files):
                    missing.append(o)
            elif o not in files:
                missing.append(o)
        return f"missing owned files: {missing}" if missing else ""
