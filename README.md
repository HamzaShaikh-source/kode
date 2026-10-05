# K.O.D.E.

**One brief. Your whole team builds it.**

K.O.D.E. is a command center for teams where everyone codes with an AI agent (Claude Code, Codex, Antigravity, OpenCode, ...).
The team lead writes one project brief. K.O.D.E. plans it, gives every teammate a substantial part of the work, starts each teammate's own agent on
their own laptop, commits and pushes with each laptop's own GitHub login, merges the pull requests, runs the tests, and sends
failures back as fix tasks until the project passes.

It was built for hackathons: nobody passes prompts around by hand, everyone makes a real contribution (not an equal commit count; a small task is one good commit),
and the lead sees every agent's terminal live on one screen.

---

## What you get

| | |
|---|---|
| **Projects** | Brief → plan with a frozen interface contract → tasks with disjoint file ownership → one branch and one PR per task → ordered merges → verification → automatic fix rounds → a final quality review (README, tests, repository hygiene, an AI reviewer against your brief) that opens polish tasks until it is clean. Choose the repository name and visibility; K.O.D.E. adds every teammate as a collaborator and accepts the invitation on their laptop automatically. Live task lanes, contributions per person, PR links. |
| **Your agents, your logins** | Each laptop runs its own Claude Code / Codex / Antigravity / OpenCode / Hermes session. Commits use that person's git identity and are pushed with their own `gh` login. The Brain never holds a GitHub token. |
| **Join from any laptop** | The lead sends an invite link. The teammate opens it and runs one command (PowerShell on Windows, `curl | sh` on macOS/Linux). |
| **Turbo agents** | Agents can run with every permission pre-approved (fastest), or be supervised by a model that answers their questions, or ask you. |
| **Live consoles** | Every agent terminal, live, on one page. Click into any one; type into it when you have access. |
| **Guardrails in code** | Out-of-scope and contract edits are reverted before merge; terms, logins and destructive prompts always stop for a human; secrets are redacted on each laptop before output leaves it; everything is audited. |
| **Orchestrator, API profiles, usage** | A chat-style orchestrator for ad-hoc coordination, custom API endpoints per session, token / context / cost estimates where the tools expose them. |

## Quick start (the lead's laptop)

Requirements: **Python 3.10+**, **git**, the **GitHub CLI** logged in (`gh auth login`), and at least one coding agent you are signed in to.

```bash
git clone https://github.com/<you>/kode.git
cd kode
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
python run_kode.py --lan
```

Open **http://localhost:8000/console**. (`--lan` lets teammates on the same network reach you; leave it out to stay local-only.)

No `.env` is needed for projects: the planner can be Claude Code or Codex using your own login. Copy `backend/.env.example` to
`backend/.env` only if you want an API-key model as the default.

## Run it at a hackathon

1. **Network.** Same Wi-Fi or a phone hotspot: start with `python run_kode.py --lan` and allow it through the firewall (on Windows,
   also for *Public* networks; hotspots usually count as public). Teammates elsewhere: put a tunnel in front of port 8000 (for example
   `cloudflared tunnel --url http://localhost:8000`) and start with `python run_kode.py --public-url https://<your-tunnel>`.
2. **Invite.** Console → **Team** → **Invite teammate**. Choose their name, how long the link works and your access (*Full access* is the
   default), check the address teammates use, and send them the link.
3. **Join.** Each teammate opens the link on their laptop and runs the one command it shows. On Windows it is a downloaded `kode-join.ps1` run with `powershell -ExecutionPolicy Bypass -File` (Defender blocks the one-line `irm | iex` pattern). It installs a small agent into
   `~/kode-node`, checks git / GitHub CLI / agents, and connects. Next time they run `start.ps1` or `~/kode-node/start.sh`.
   The Team page shows who is ready (git installed, `gh` logged in, commit identity).
4. **Project.** Console → **Projects** → **New project**: write the brief, pick each person's agent, pick the **Planner**
   (Claude Code / Codex login, or an API profile), choose **Turbo** permissions, and press **Plan the project**.
5. **Approve and watch.** Review the plan (tasks, owners, files, frozen contract, test command), reassign if you like, press
   **Approve and start building**. Cards appear only when something needs a human.

A full walkthrough is served by the app at **/guide**.

## How it works

```
 lead's laptop                                        teammates' laptops
┌──────────────────────────────┐   outbound WS   ┌──────────────────────────────┐
│ Brain (FastAPI)              │◀───────────────│ kode-node agent               │
│  • console (static ES modules)│                │  • hosts agent terminals (PTY)│
│  • project engine            │  ops: git / gh │  • git / gh / test commands   │
│  • orchestrator, profiles    │ ─────────────▶ │    in its own workspace       │
│  • SQLite (hashed tokens,    │                │  • redacts secrets at source  │
│    encrypted API keys, audit)│                │  • enforces access locally    │
└──────────────────────────────┘                └──────────────────────────────┘
```

* `backend/hub/project.py` – the project engine: plan validation, weighted assignment, branches, scope guard, PRs, merge order, verify,
  triage, fix rounds, contribution report, the final quality review, automatic collaborator access, the prompt supervisor.
* `backend/node/ops.py` – git / `gh` / test operations a laptop runs for the engine, always inside its workspace.
* `backend/hub/join.py` – invite links, join page, installers, agent bundle.
* `backend/hub/gateway.py` – the WebSocket hub: machines, sessions, consoles, access ladder, audit.
* `console/` – the web console (no build step).

More: [backend/ARCHITECTURE.md](backend/ARCHITECTURE.md), [backend/COMPAT.md](backend/COMPAT.md), [backend/STATUS.md](backend/STATUS.md).

## Security model

* Teammates' machines connect **outward** to the Brain; nothing listens on their laptops.
* Invite codes are single use and expire (10 min – 24 h). Wrong codes, keys and pairing attempts share a lockout per address.
* The console requires the **lead key** (stored in `backend/.hub_key`) from anywhere but the Brain's own PC, and the key is never
  served through a tunnel or proxy.
* Each laptop decides the most access it will grant (`--access-limit`) and keeps a **kill switch** in its member console.
* API keys are encrypted at rest; machine tokens are stored hashed; secrets are redacted on the laptop before output is sent.
* Turbo mode lets agents run any command inside the project folder without asking. Use it on machines and repos you are comfortable with.

## Testing

```bash
cd backend
python -m pytest -q
```

102 tests, including a hermetic three-laptop project build (real node processes, scripted agents, local git remote) and a
"new laptop" that joins using only the downloaded agent bundle.

## Known limits

* Real agents' speed and quality vary; Antigravity and free models are noticeably slower.
* Tested on Windows. The macOS/Linux installer and PTY path are implemented but not yet verified on real machines.
* One task per laptop at a time; no live re-planning mid-build (use pause, reassign, or the automatic fix rounds).

## Resilience
Designed for flaky hackathon networks (details in `/guide`, tests in `backend/tests/test_resilience.py`): a Wi-Fi blip keeps the same agent session;
offline laptops are waited for (20 min of retries with backoff) and the merge/verify role fails over to another online teammate; a killed or rebooted
agent is restarted on the same branch (Claude Code / Antigravity resume their conversation); a Brain restart resumes running projects automatically;
GitHub errors are retried; a second agent for the same machine replaces the first; start scripts restart a crashed agent.
