# K.O.D.E. engine - architecture

Method: **contract -> isolate -> parallelise -> verify -> human-approve.** One feature request becomes
three specialist agents (Backend, Frontend, QA) with disjoint file ownership, bound to one frozen interface contract.

```
POST /features {prompt}
   |
   v
[1 CONTRACT] planner.py  --LLM-->  FeatureAnalysis (endpoints, schemas, 3 TaskSpecs)
             contract.py ------->  contracts/<name>_openapi.yaml + <name>_types.ts   (validated, sha256-locked)
                                   source: frozen-on-disk | baseline-on-disk | llm-drafted
   |                               (baseline wins ONLY if it covers the plan; frozen files are never overwritten)
   v
[2 ISOLATE]  decompose.py -> Task(files_owned=[...])   guard.validate_ownership()
             guard.py: overlap / out-of-domain / frozen-path claim  => ScopeViolation
   |
POST /features/{id}/dispatch
   v
[3 PARALLELISE]  dispatch.py: asyncio.gather( run_one(backend), run_one(frontend), run_one(qa) )
   |   each run_one:
   |     agents/base.py  LLMAgent.run()            THINKING
   |        assert_frozen(contract)                (hash re-check: ContractDrift if edited)
   |        LiteLLM call (llm.py) w/ persona prompt (agents/prompts.py) + frozen OpenAPI + TS types
   |        GuardedWriter.write(path)  --------->  ScopeViolation => BLOCKED   (guard enforces, prompt only hints)
   |        role validators (frontend: imports only exported "@contracts/<name>_types" names)   WRITE_CODE
   |     pipeline/review.py   static diff of implementation vs frozen contract
   |     pipeline/github.py   branch -> blobs -> tree -> commit (author = mapped human) -> PR    COMMIT, PR_CREATED
   |                          review posted as PR review comment
   v
[4 VERIFY]   review.py findings (route/field/path/type-import mismatches)  +  QA agent's e2e & contract tests
   v
[5 HUMAN-APPROVE]  state.ApprovalRequest(pending) per PR                          NEEDS_REVIEW
             POST /approvals/{id}/approve  -> github.merge_pr()   <-- the ONLY merge path (refuses non-approved)
             POST /approvals/{id}/reject   -> close PR, never merges                BLOCKED(stage=rejected)

GET /events  (SSE)  <-  events.py EventBus: persist (seq) -> fan-out; replay + live; ids = seq
```

## File -> method step
| File | Step | Role |
|---|---|---|
| `contracts/demo_openapi.yaml`, `demo_types.ts` | 1 contract | hand-authored baseline, written before any agent logic |
| `billing_models.py` | 1 contract | Pydantic mirrors; `tests/test_contract_models.py` proves field-for-field equality with the OpenAPI |
| `orchestrator/planner.py` | 1 | prompt -> analysis via LLM |
| `orchestrator/contract.py` | 1 | validate (openapi-spec-validator + TS export check), write, sha256 freeze, drift detection |
| `orchestrator/decompose.py` | 2 isolate | analysis + contract -> Tasks with `files_owned` |
| `orchestrator/guard.py` | 2 isolate | `ScopeViolation`; `GuardedWriter` is the only way an agent produces files |
| `agents/base.py` + `backend/frontend/qa.py` + `prompts.py` | 3 parallelise | Worker interface + personas |
| `orchestrator/dispatch.py` | 3 parallelise, 5 approve | `asyncio.gather`; `approve()`/`reject()` |
| `pipeline/review.py` | 4 verify | contract-vs-implementation static review |
| `pipeline/github.py` | 3, 5 | PyGithub branch/commit/PR; `merge_pr` gated on APPROVED |
| `state.py`, `events.py` | all | SQLite persistence; ordered SSE |
| `routes.py`, `app.py`, `config.py`, `llm.py`, `models.py` | wiring | REST/SSE, lifespan + banner, settings, LiteLLM, models |

Files added beyond your list: `llm.py` (single LiteLLM seam), `billing_models.py`, `orchestrator/dispatch.py`.

## Where the guarantees live (code, not prompts)
- Isolation: `guard.validate_ownership` (before work) and `GuardedWriter` (at every write).
- Contract immutability: `contract.assert_frozen` called by every agent and by dispatch.
- No merge without a human: `GitHubClient.merge_pr` raises `MergeNotApproved` unless the request is `approved`;
  only `dispatch.approve()` calls it.
- No fake data: `llm.py` has no fallback path; `Settings.require_llm()` raises when the key is missing.

## UI wiring (T3)
`app.py` serves ONLY `index.html`, `app.js`, `styles.css` and `/fonts` (never the project folder: it contains `backend/.env`).
`/health` reports `mode` (`ENGINE` | `SIMULATION`) and `minimal`.
- **ENGINE mode** (default): `app.js` starts empty, opens `EventSource('/events')` (replay + live), maps the six engine event types
  onto the existing fleet/board/activity views, submits `POST /features` then `POST /features/{id}/dispatch`, and shows
  `GET /approvals` with Approve/Reject buttons (`POST /approvals/{id}/approve|reject`, the only merge path). Cards are read-only: the engine moves them.
- **SIMULATION mode** (`SIMULATION_MODE=1`, or no engine reachable): the original timer fleet, badged with a SIMULATION pill and ribbon.
Event mapping (UI taxonomy <- engine): THINKING(plan)->create, THINKING/WRITE_CODE/COMMIT/PR_CREATED(agent)->move,
NEEDS_REVIEW->pause, merge->complete, BLOCKED/rejected->error.
Other additions: `llm.RetryingLLM` (bounded retries, exponential backoff, call deadline), per-agent wall budget (`AgentTimeout`), `run_kode.py`.

## v4: project engine (hub/project.py, node/ops.py)
`ProjectEngine` (one asyncio task per project) drives plan -> bootstrap -> build -> merge -> verify -> fix -> done. It talks to laptops through
`Hub.node_op(node, name, **args)`, a request/response channel over the existing node WebSocket; `node/ops.py` executes git / `gh` / test commands
inside the member's workspace roots. Agents are ordinary provider sessions (PTY) pointed at `.kode/TASK.md`. State is one JSON document per
project in SQLite (`projects` table); a Brain restart pauses running projects and Resume continues them.
Code-enforced guarantees: disjoint ownership (`normalize_plan`), contract freeze + scope revert (`op_scope_revert`), ordered serialized merges,
commit-balance report from `git log`. LLM-decided: the plan and failure triage. Tests: `tests/test_project.py` (pure logic + a hermetic 3-laptop loop).
