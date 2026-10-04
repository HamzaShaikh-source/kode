import asyncio
import shutil
from pathlib import Path

import pytest

from app import Services
from config import Settings
from events import EventBus
from state import State

REAL_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def make_settings(tmp_path, **kw) -> Settings:
    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    base = dict(_env_file=None, anthropic_api_key="test-key-not-real", github_token="test-token-not-real",
                github_repo="org/repo", llm_model="anthropic/claude-sonnet-5-5",
                author_backend="Ada Backend <ada@example.com>", author_frontend="Finn Frontend <finn@example.com>",
                author_qa="Quinn QA <quinn@example.com>", kode_db_path=str(tmp_path / "state.db"),
                kode_contracts_dir=str(contracts))
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def seeded_contracts(tmp_path):
    """Copy the hand-authored baseline contract into a scratch dir (keeps the repo's lock file clean)."""
    d = tmp_path / "contracts"
    d.mkdir(exist_ok=True)
    for n in ("demo_openapi.yaml", "demo_types.ts"):
        shutil.copy(REAL_CONTRACTS / n, d / n)
    return d


@pytest.fixture
def services(tmp_path):
    from tests.fakes import TestDoubleGitHub, TestDoubleLLM

    async def build(llm=None, github=None, **kw):
        settings = make_settings(tmp_path, **kw)
        state = await State(settings.kode_db_path).init()
        OPEN_STATES.append(state)
        return Services(settings, state, EventBus(state), llm or TestDoubleLLM(),
                        github_override=github or TestDoubleGitHub())
    return build


OPEN_STATES = []


def run(coro):
    """Run a coroutine to completion, then close every SQLite connection opened during it."""
    async def wrapper():
        try:
            return await coro
        finally:
            while OPEN_STATES:
                await OPEN_STATES.pop().close()
    return asyncio.run(wrapper())
