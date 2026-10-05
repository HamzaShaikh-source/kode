# K.O.D.E. - status with evidence (2026-10-04)

Nothing below is rounded up. "Proven" means a command was run and its output was seen; the commands are listed.
Test repo: https://github.com/HamzaShaikh-source/kode-demo (private, throwaway). It lives under the personal
account because no `bugged` org/account is accessible to me and I did not create one.

## Acceptance table
| | Criterion | Status | Evidence |
|---|---|---|---|
| A | `pytest` green | **PASS** | `cd backend; pytest` -> `48 passed in 22.74s` |
| B | POST /features writes + freezes contract | **PASS** | LLM-drafted contract written, validated, sha256-locked (`tests/test_pipeline.py::test_B_*`; real-model run froze `5d6ea35f...`). Hand-authored baseline `contracts/demo_openapi.yaml` (+ `.lock.json`) reused when it covers the plan (`98be7da3f7e1...`). |
| C | Two tasks claiming one file -> ScopeViolation | **PASS** | `tests/test_guard.py::test_C_*` (exact, glob, directory overlap) |
| D | Out-of-scope write blocked + log line | **PASS** | `SCOPE_VIOLATION backend agent ... attempted write outside scope: web/components/BillingUsage.tsx` (test_D_*, plus dispatch-level test) |
| E | Agents truly concurrent | **PASS** | Test: wall < 0.55 x serial. Real run (GitHub, minimal): wall 108.6s vs serial-equivalent 142.8s; earlier real run x1.96 and x2.53 |
| F | Frontend imports generated TS types | **PASS** | Real model output imports `{ ... } from "@contracts/demo_types"`; invented/redeclared types rejected (`test_F_*`) |
| G | /events streams real transitions in order | **PASS** | `test_G_*` incl. real HTTP SSE; and live in the wired e2e (UI saw THINKING, WRITE_CODE, COMMIT, PR_CREATED, NEEDS_REVIEW) |
| H | Approval per merge; reject never merges | **PASS (real GitHub)** | See "Real GitHub loop" below |
| I | Real GitHub loop | **PASS** | 3 branches, 3 distinct authors, 3 PRs, approve-merge + reject verified via GitHub REST API |
| J | Startup banner real vs SIMULATED | **PASS** | printed by `run_kode.py` / `uvicorn app:app`; `test_J_banner_lists_real_and_simulated` |

## Real GitHub loop (acceptance I / H), via GitHub REST API
Run `python demo/run_demo.py --minimal` (real gateway LLM `qwen3.8-flash:free`, real GitHub):
```
backend   ok  Naman Raghav <naman@bugged.dev>      https://github.com/HamzaShaikh-source/kode-demo/pull/5   (108.6s)
frontend  ok  Nishitha Lokesh <nishitha@bugged.dev> https://github.com/HamzaShaikh-source/kode-demo/pull/3   (16.2s)
qa        ok  Jyoti Rodrigrez <jyoti@bugged.dev>   https://github.com/HamzaShaikh-source/kode-demo/pull/4   (18.1s)
WALL-CLOCK 108.64s   SERIAL-EQUIVALENT 142.84s   SPEEDUP x1.31
```
GitHub API for each PR: head `kode/bde41e/<role>`, base `main`, commit author = committer = the synthetic identity above,
one K.O.D.E. review comment per PR, `main` sha unchanged (`a83087e434`) until a human acts.
Human path through the engine's HTTP API (`scratch_run/approve_proof.py`):
```
REJECT  qa       -> 200 rejected | main after reject: a83087e434 | unchanged: True | PR#4 state=closed merged=False
APPROVE frontend -> 200 merged   | main after approve: 0a1f4b01d5 | advanced: True
double-approve -> 409      approve-after-reject -> 409
PR #3 merged=True   PR #4 merged=False   PR #5 open (untouched)   exactly one merged: True
```

## Wired UI end-to-end (`python tests/e2e_wired.py`) - REAL UI -> real LLM -> real GitHub
Final run: **16/16 checks passed**. PR URLs:
- https://github.com/HamzaShaikh-source/kode-demo/pull/8  (qa, rejected via UI button, closed unmerged)
- https://github.com/HamzaShaikh-source/kode-demo/pull/10 (backend, **approved via UI button -> merged**, main = `6427715a60`)
- https://github.com/HamzaShaikh-source/kode-demo/pull/9  (frontend, rejected in cleanup)
Checks include: ENGINE badge, empty-start (no seeded fake data), SSE connected, 3 approvals surfaced in 277s, 3 distinct
authors, main untouched until a click, reject leaves main alone, approve merges exactly one, board columns, activity feed, no console errors.

## UI suite
`verify_interact.py` **56/56** against `http://localhost:8001` (backend-served UI, `SIMULATION_MODE=1`). It exercises the
timer simulation (pause/resume, sim toggle, drag-and-drop, ...), so it can only run in SIMULATION mode. Engine mode is covered by `e2e_wired.py`.
Simulation rehearsal screenshots: `shots/t4-sim-*.png`; engine screenshots: `shots/t3-engine-*.png`. 390px: no horizontal scroll.

## Real / simulated / roadmap
- **Real:** LiteLLM calls (gateway `https://tokenharbor.ai/v1`), planner, contract draft/freeze/drift check, scope guard, parallel agents,
  retry+backoff, SQLite, SSE, PyGithub branch/blob-commit/PR/review/merge, approval gate, UI wired to all of it.
- **SIMULATED (flagged in banner/UI):** `SIMULATION_MODE=1` UI timer fleet (badged SIMULATION); `SIMULATED_WORKER_SANDBOX`
  (agents are in-process asyncio tasks); `SIMULATED_TEST_EXECUTION` (QA writes tests; nothing runs them).
- **Roadmap:** sandboxed execution + feeding failures back to agents; TS type-check of generated code; AST-based review; API auth;
  per-user real identities with consent (demo emails are SYNTHETIC; production would map to members' noreply emails with consent).

## Known weaknesses (honest)
- **Model latency varies.** The free model took 92s for the backend agent in one run and exceeded a 180s budget in another
  (agent BLOCKED loudly, other two delivered). Live server uses `AGENT_WALL_BUDGET_S=300`; `--minimal` demo uses 180s.
  One real run needed a retry of the whole e2e for this reason (first e2e attempt: 2 of 3 PRs).
- **Review is regex-based.** The real run exposed a false positive (one-line Pydantic classes) -> fixed + regression test. It also
  cannot catch a real bug the model made (`from fastapi import JSONResponse`, wrong module). Nothing executes generated code.
- **`--minimal`/`KODE_MINIMAL=1`** gives each agent ONE tiny file; this is a stage mode, not the full decomposition.
- **GitHub token is the `gh` CLI OAuth token** (scopes `repo, workflow, gist, read:org`) because creating the requested
  fine-grained PAT hit GitHub's sudo-mode 2FA prompt. You authorized the `gh` token. It is broader than least-privilege.
- The planner is given the baseline contract as a hint; if the plan still does not fit, a new contract is drafted (never overwrites).
- The browser UI replays the whole persisted event history on every load (fine for a demo, not for scale).
- Tested on Python 3.12, not 3.11.

## Open items for the user (see ../BLOCKERS.md)
1. **Old gateway key not revoked** (T7): your shell still exports it as `OPENAI_API_KEY`; revoking would break any other tool using it.
2. Optionally replace the `gh` token with a fine-grained PAT (needs your 2FA code).


## v2 / P0 spike (terminal bridge) - see ../PLAN-v2.md section 12 and COMPAT.md
`pytest`: **57 passed** (48 engine + 9 bridge security/PTY). Latency, per-provider render evidence, and the provider matrix are in `COMPAT.md`.
Not built yet: Node agent, pairing, multi-viewer relay, Brain graph, control modes, usage, remote workers (P1-P6).
The bridge is OFF unless `KODE_ENABLE_BRIDGE=1` and is localhost-only; it is a spike, not production remote access.


## v2 Brain + Console (P1-P4) - status with evidence (2026-10-04)
| Piece | State | Evidence |
|---|---|---|
| Pairing (one-time code, hashed node tokens), node agent, outbound-only WS | **built + tested** | `tests/test_hub.py` (12 tests incl. real `kode-node` subprocesses) |
| Session registry, replay buffers, output relay to viewers | **built + tested** | same; a real bug (double `sid` kwarg) was found and fixed by these tests |
| Consent ladder watch/suggest/co-pilot/takeover, member approval, takeover lock, expiry, kill switch, isolation between machines | **built + tested** | `test_consent_ladder_takeover_lock_killswitch_and_isolation`, `test_takeover_expires_automatically` |
| Defence in depth: the node re-checks the mode locally | **tested** | `test_node_drops_lead_input_unless_its_own_mode_allows` |
| Secret redaction on the node, workspace-root confinement, terminal-reply channel can't type | **tested** | `test_output_is_redacted...`, `test_node_refuses_workspace_escape`, reply-regex check |
| Real data collectors: git status, Claude Code transcripts (plan + tokens), Codex transcripts (plan + tokens) | **tested on fixtures shaped like real files**; structure inspected on this PC | `test_claude_collector...`, `test_codex_collector...`, `test_git_info...`. NOT yet confirmed live against a running Claude/Codex session |
| Demo fleet: 3 real node processes on this PC, 5 real provider sessions | **running, verified over the protocol** | 3 machines online, 5 sessions live, all `watch`, `same_host=True` (so the UI labels them "same PC") |
| Console UI (Brain map, sessions grid, focus view, machines, usage, activity, settings, member view) | **BUILT, NOT VISUALLY VERIFIED** | JS syntax checked (12 modules), assets served, lead key only to loopback. I could not render it: my request to load the page in the browser was denied by the permission system, and I did not work around it. No screenshots exist yet, no design critique loop was run, and layout bugs are likely. |
Totals: `pytest` **69 passed**.
Known limits: demo machines share one PC; hub trusted by nodes (no signed grants yet); redaction is per-output-chunk pattern matching; only Claude Code and Codex expose plan/tokens; no cost numbers anywhere; Windows only tested.

## Session wrap-up (2026-10-05): real-world tests, real task, UI v2
- **pytest: 78 passed** (adds `tests/test_realworld.py`: Brain restart + node reattach/replay, link flap, provider exit + restart, 2.5 MB output flood with bounded buffer, unicode, 12 sessions on 3 machines, brute-force lockout, cross-origin rejection, end-to-end keystroke latency).
- **Measured:** lead->hub->node->PTY->lead keystroke p50 2 ms / p95 4 ms; 12 sessions live in 0.8 s; replay buffer capped at 240k chars; keystroke 49 ms right after a flood.
- **Real task (OpenCode free model, lead drove it in co-pilot after the member approved in 228 ms):** TUI ready 20 s, first reaction 5.7 s, `fib.py` written in 34 s after a permission prompt the lead approved, output correct (0 1 1 2 3 5 8 13 21 34), ~90 s total. Claude Code / Codex token readers verified on fixtures only, not on a live paid session.
- **Hardening added:** resolved working folder reported by the node, restart/remove for ended sessions, brute-force lockout (8 failures / 5 min), security headers + CSP on console pages, /healthz /readyz, lead-key gate for remote access.
- **UI v2 (visually reviewed with screenshots, light/dark, 1440/900/390, lead and member):** fixed "null" bug, stray focus ring, dead space in the focus view, misleading unknown-usage bars, mobile overflow (now a bottom tab bar); added first-run checklist, keyboard shortcuts + help, loading skeleton, tab-title request count.
- **NOT DONE (requested, not built):** public landing page and a /guide how-to page (the in-app checklist and shortcuts exist; the guide is only the README/PLAN-v2 text), pyproject/installer packaging, CI workflow, service/installer scripts. Docker not available here, so no Dockerfile. Dark-mode mobile and the 900 px tablet layout were captured but not critiqued. Codex exited once with code 0 at its trust prompt on the first demo run and did not reproduce; cause unknown.
- Everything above ran on ONE Windows PC; "machines" are separate processes. No macOS/Linux or multi-computer LAN test was done.

## v3 UI (2026-10-04)
Built: Team/Invite dialog, Models page + profile dialog, Orchestrator page, profile/model picker in New session, context gauge / needs-input chip / cost estimate, landing page (`/`), guide (`/guide`), `/classic`.
Evidence: screenshots in `shots/v3-*` and `shots/site-*` (light/dark 1440, landing/guide light 390; no horizontal overflow, no console errors via `scratch_run/console_shots.py` and `site_shots.py`); real orchestrator run (8 steps, 23k tokens) shown on the Orchestrator page.
Not verified: Claude Code / Codex through a custom endpoint (needs an Anthropic-/Responses-compatible gateway; only an OpenAI-chat gateway was available). The guide's LAN instructions are untested.

## v4 project engine (2026-10-05)
Built: `node/ops.py` (git / gh / test operations on each laptop), `hub/project.py` (plan, contract freeze, fair assignment, per-task branches, scope guard, PRs, ordered merge, verify, LLM triage, fix rounds, commit-balance report), Projects page, total-access default, `demo-agent` provider, landing v2 + guide.
Evidence:
- Hermetic: `tests/test_project.py` (pure logic + 3 real node processes, scripted agents, local bare remote): plan -> contract -> 6 tasks -> guard reverts -> merges -> failing verify -> triage -> fix task -> pass. Full suite: 95 tests (93 passed in the last full run; the 2 failures there were a hub restart I triggered mid-run and an access-clamp bug, fixed and re-run green individually; a final full run is below if present).
- REAL run (`scratch_run/real_project.py`, log `real_project3.log`): real planner model (qwen3.8-flash:free via Token Harbor profile), real private GitHub repo `HamzaShaikh-source/idea-board-39e1bc`, OpenCode on three laptops (one PC, synthetic identities). 9 tasks, 9 merged PRs, commits Naman 15 / Jyoti 12 / Nishitha 10, scope guard reverted 6 stray files, 70 tests pass from a fresh `gh repo clone` (checked independently). ~35 min wall time with a slow free model.
Issues found by the real run and fixed: OpenCode starts in read-only Plan mode (now launched with `--agent build --auto`); the model flagged a "rules" file as injection (reworded as project conventions); reminders fired too early for a slow model (now only after a few commit-less minutes); OpenCode crashed at startup twice (the engine restarted it automatically).
Not verified: Claude Code / Codex / Antigravity as project agents (flags for Claude and OpenCode only; Codex untested); fix-round path against a real model (only the hermetic test exercised it); multiple real machines (all laptops were one PC).
Throwaway private repos created on GitHub: idea-board-1c91ee, idea-board-39e1bc (earlier attempts were abandoned runs). Delete them when you no longer need them.

## v5 join + turbo (2026-10-04)
Evidence:
- `tests/test_join.py` (5 tests): bundle allow-list has no secrets or lead key; join API / installers / page with CSP; unknown codes lock the address
  out (429); hostile member names stay quoted in both installers; lead key not served with X-Forwarded-For / CF-Connecting-IP / foreign Host;
  a "new laptop" running ONLY the extracted bundle pairs and reports readiness; used invites cannot be installed again (410).
- Real Windows one-liner (`scratch_run/installer_e2e.py`): `irm <brain>/join/<code>/install.ps1 | iex` with a throwaway USERPROFILE downloaded the
  bundle, created a venv, pip-installed, found git/gh/claude/codex/agy/opencode/hermes, paired as "Priya's LENOVO-IDEAPAD" and reported
  readiness in 59 s; `start.ps1` written.
- Turbo mode (`scratch_run/probe_project.py claude agy codex`, supervisor = Claude Code CLI): all three tasks merged and verified in 253 s;
  Antigravity needed no permission answers at all (before Turbo: about one prompt per command).
Not verified: macOS/Linux installer on a real Mac/Linux machine (script reviewed, not run); a real second physical laptop over Wi-Fi or a
tunnel (all joins ran on this PC); cloudflared is not installed here.

## v6 resilience (2026-10-05)
Bugs found and fixed: an offline laptop's session was treated as dead (task restarted, blocked after ~9 s); a network error during merge left the task
stuck in "review" forever; a dead agent with partial commits was merged half-finished; a Brain restart paused projects until a manual Resume and could
start a second agent on a task that already had one; two agents for one machine fought over the connection.
Evidence (`tests/test_resilience.py`, 10 tests, real hub + real node processes + scripted agents): offline-is-not-dead, backoff and 20-minute grace,
review-never-stranded, coordinator failover (unit); Wi-Fi blip mid-task (same sid, no restart, project done); agent process killed and restarted
(same branch, project done); Brain restart (auto-resume, project done); double start (old agent exits); revoked token (exit code 3).
Not verified: real Wi-Fi loss / laptop sleep (simulated by closing the socket and killing the process); `--continue` resume was checked to start
fresh when there is no conversation, not across a real mid-task kill of Claude Code or Antigravity.

## v6.1 late setup + rejoin (2026-10-05)
Reported: a teammate installed and signed in to Antigravity AFTER joining and it never showed up; rejoining with a new invite said "auth failed".
Causes: the agent reported providers only at connect time (and a running Windows process never sees a new PATH); `--pair` was ignored whenever a saved token existed, so the stale token was sent.
Fixes: 15 s rescan with PATH refresh (registry + common folders), `providers` update message, Rescan button, signed-in check (Claude `auth status`, Codex `login status`; others unknown), pre-flight error before a project starts; a fresh invite always re-pairs, a bad invite falls back to the saved login, and the old offline entry for the same person/machine is replaced.
Tests (`test_resilience.py`): tool appearing after start without restart (real node process), stale token + new invite, bad invite + good saved login, rejoin leaves one entry. Not verified: Antigravity sign-in state (it has no cheap status command, shown as unknown).
Windows Defender finding: the one-liner `powershell -ExecutionPolicy Bypass -Command "irm <url> | iex"` was BLOCKED by Defender on this PC (Get-MpThreatDetection shows the command line; CreateProcess returns Access denied), after working earlier. The Windows join flow now uses a downloaded `kode-join.ps1` (BOM + CRLF) run with `-File`; verified end to end over the LAN address with a stale saved login present (joined in 30 s, registration replaced). Defender settings were not changed.

## Repository access, contributions and the quality gate

- Repository name and visibility (private or public) are chosen when a project is created; an existing `owner/name` can be used instead.
- A private repo made by one laptop is invisible to the others, so the engine invites each teammate's GitHub login (`collab_add`, run on the owner's laptop), accepts on that teammate's laptop (`collab_accept`) and verifies write access (`repo_access`) before any task starts. Unit-tested with a recording `gh`; **not yet exercised against GitHub with a second real account**.
- Equal commit counts are no longer required (`min_commits` is 1); the report is "Contributions".
- After verification passes, a quality review (`repo_audit` hygiene checks plus an AI reviewer against the brief) opens up to two rounds of polish tasks; the result is shown as the Quality review card.

## Hackathon mode, code judge, session reuse, lead PC

- `hub/ideate.py`: four rounds run by the teammates' own agents (propose with research, anonymous critique, rebuttal, vote; never for your own idea). Unit-tested with scripted agents. **Research quality depends on whether the agent's CLI can search the web in non-interactive mode**; the prompt asks for it and records when it could not.
- `hub/judge.py`: blind, independent judges, fixed rubric, deterministic caps, median aggregation, disagreement reported. Unit-tested; wired into the hermetic project loop. A model still has preferences: the scorecard lists every judge's score.
- One agent session per teammate is reused across that teammate's tasks (at most 4, then a fresh one; a retried or restarted task always gets a fresh agent). Done markers only count output printed since the task started.
- `run_kode.py` joins the lead's own PC as a teammate by default (`--no-lead-node` to disable); the lead PC coordinates merges, so contract, plan and final commits are the lead's, and it receives tasks like everyone else.
- Relevant installed skills (from `~/.claude/skills`, `~/.agents/skills`, `~/.codex/skills`) are listed in each task for the agent to use.
