# K.O.D.E. backend engine

One feature request -> three parallel specialist agents (Backend, Frontend, QA), each owning disjoint files,
each committing to its own branch under a named human, all bound to one frozen contract, nothing merged
without human approval. Python 3.11+ (developed on 3.12), FastAPI, LiteLLM, PyGithub, SQLite.
The existing frontend is untouched; it will consume `contracts/demo_openapi.yaml` and the REST/SSE surface below.

## Run
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env            # fill in values (never committed, never logged)
pytest                          # 48 tests, no network, no keys needed
uvicorn app:app --port 8000     # prints the REAL / SIMULATED_* banner on startup
python demo/run_demo.py [--minimal]   # full loop against YOUR test repo: 3 branches, 3 authors, 3 PRs
python ../run_kode.py           # UI + engine on http://localhost:8000 (see root README)
python demo/run_demo.py --stop-before-github   # real LLM only (no GitHub token needed); GitHub stage is SKIPPED, not faked
```

## Environment (`.env.example` documents each)
`GITHUB_TOKEN`, `GITHUB_REPO` (owner/name, must already have a `main` commit), `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`,
`LLM_MODEL` (any LiteLLM model id; the key matching its provider is required), `AUTHOR_BACKEND|FRONTEND|QA`
(`Name <email>` of the human who owns each agent's commits). A missing value fails loudly at the point of use
(HTTP 503 with the variable's name), never silently simulates. Any OpenAI-compatible gateway works through the standard
LiteLLM env (`OPENAI_API_BASE`) and `LLM_MODEL=openai/<model>`.

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/features` `{prompt, contract_name?="demo"}` | plan (LLM) + write/freeze contract + decompose; returns plan, contract info, 3 tasks |
| GET | `/features/{id}/contract` | frozen OpenAPI + TS types + sha256 (re-verified against disk) |
| POST | `/features/{id}/dispatch` | run 3 agents concurrently, commit, open PRs, review, create approvals; returns timing table |
| GET | `/events` | SSE: `THINKING WRITE_CODE COMMIT PR_CREATED NEEDS_REVIEW BLOCKED`; `Last-Event-ID`/`?after=` replay |
| GET | `/approvals` | approval requests |
| POST | `/approvals/{id}/approve` | the ONLY path to a merge |
| POST | `/approvals/{id}/reject` `{reason}` | closes the PR; never merges |
| GET | `/health` | component truth table |

## What is real today
- **Real:** LLM calls via LiteLLM for planner, contract drafting (when no contract exists) and all three agents;
  OpenAPI validation; sha256 contract freeze + drift detection; ownership/scope guard (code-enforced);
  true `asyncio.gather` concurrency; SQLite state; ordered, replayable SSE; approval gate; PyGithub
  branch / blob-tree-commit (author+committer = mapped human) / PR / review comment / merge.
- **Verified by tests (48):** doubles stand in for the LLM and GitHub *inside tests only*, labelled `TestDouble*` in
  `tests/fakes.py`. They prove orchestration, guard, concurrency, SSE order and approval logic - not model quality.
- **Verified against a real model:** planner + LLM-drafted contract + 3 parallel agents (see the run log in the final report).
- **SIMULATED (flagged in the banner):** `SIMULATED_WORKER_SANDBOX` (agents are in-process asyncio tasks, not isolated
  sandboxes) and `SIMULATED_TEST_EXECUTION` (QA agent writes tests; nothing executes them).
- **Real GitHub loop: PROVEN** against a private throwaway repo (3 branches, 3 authors, 3 PRs, approve-merge, reject-safe,
  and through the UI). Evidence and PR URLs: `STATUS.md`. The full product runbook is the root `README.md`.

## Deliberately left for Phase 3/4
- Executing generated code/tests in sandboxes (docker/firecracker) and feeding failures back to agents.
- A real type-check of generated TS and import-resolution of `@contracts/*` (alias is asserted textually).
- Review uses regex static analysis (no AST); LLM-assisted review notes; PR merge-order/conflict handling beyond GitHub's own refusal.
- Auth on the API, multi-repo, per-feature branch cleanup, retry/resume of half-finished dispatches, rate limiting.
- Platform-level OpenAPI for K.O.D.E.'s own REST surface (only the demo billing contract is frozen/Pydantic-mirrored).
- The UI status names are mapped 1:1 (`THINKING`...`BLOCKED`); merge/rejection are carried as `data.stage` on `COMMIT`/`BLOCKED`.
