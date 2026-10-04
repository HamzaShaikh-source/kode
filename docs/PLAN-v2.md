# K.O.D.E. v2 plan - the team Brain for every AI coding provider

Status: **P0 spike DONE (results at the bottom)**; P1 onward not started. Today's repo (contract-first agents, scope guard, PR pipeline,
approval gate, SSE, UI) becomes the **orchestration layer** of this plan; it is reused, not rewritten.

## 1. The idea in one paragraph
Every teammate keeps using the AI coding tool they like (Claude Code, Command Code, Hermes, Antigravity, OpenCode,
Codex, Gemini CLI, ...) on their own laptop. A small **Node agent** on each laptop connects those tools to the lead's
**Brain** (the hub). The lead sees one screen: the Brain in the middle, every teammate's provider sessions around it as
live, minimized consoles. Click one and you see exactly what it is doing (terminal, to-dos, files, usage). The lead can
step in and drive it. Work is split by a **frozen contract**, enforced by the **scope guard**, merged only after **human approval**.

What Amoeba (useamoeba.com) proves works: local provider CLIs + a shared coordination brain + overlap warnings + advisory
file locks + separate git worktrees + approval gates. What we add: any provider (not two), live consoles of every session,
a Brain view, lead takeover, per-provider usage, and our contract/guard/approval pipeline.

## 2. Architecture
```
 Teammate laptop (x N)                          Lead's Brain (hub)                       Lead's browser
 +-----------------------------+   outbound     +----------------------------+  WS/SSE  +----------------------+
 | Provider CLIs / apps        |   WSS only     | Gateway (auth, rooms)      |<-------->| Brain view           |
 |  claude | cmd | hermes | agy |<-------------->| Session registry + presence|          | Console grid         |
 | kode-node agent             |  (no inbound   | Stream relay + ring buffers|          | Focus view (terminal,|
 |  - provider manifests       |   ports, works | Usage aggregator           |          |  todos, files, usage)|
 |  - PTY runner (ConPTY/pty)  |   behind NAT)  | Orchestrator (EXISTING:    |          | Dashboard            |
 |  - structured adapters      |                |  planner, contract, guard, |          +----------------------+
 |  - git worktree per session |                |  decompose, review, PRs,   |
 |  - redaction + consent gate |                |  approvals)                |
 +-----------------------------+                | Store (SQLite -> Postgres) |
                                                +----------------------------+
```
- **Node agent** (`kode-node`, Python first): pairs with the Brain by a short code, then keeps one outbound WebSocket open.
  Discovers installed providers, starts/stops sessions, streams output, accepts input only when consent allows.
- **Brain**: today's FastAPI app plus a WebSocket gateway, session registry, ring buffers, usage aggregator, RBAC, audit log.
- **UI**: stays no-build static, same ops-zine design language; xterm.js vendored locally (no CDN).

## 3. "Compatible with everything": three adapter tiers
Honest framing: no one can promise "everything" up front, so compatibility is a **generated matrix**, not a claim.
| Tier | How | Works for | What the Brain gets |
|---|---|---|---|
| 0 Universal | Run any CLI inside a pseudo-terminal (Windows ConPTY, macOS/Linux pty) | every terminal tool: Claude Code, Command Code, Hermes, OpenCode, Antigravity CLI (`agy`), Codex, Gemini CLI, Aider... | live terminal, keystroke injection, resize, replay |
| 1 Structured | The provider's own machine interface: JSON/stream output, hooks, ACP (Agent Client Protocol), MCP, local server APIs | tools that expose one | to-dos, tool calls, files touched, approval prompts, token counts |
| 2 API | Models via LiteLLM (what we already use) | providers with no usable CLI | text results + exact usage |
| GUI-only | opt-in window capture (low fps) | desktop apps with no CLI | picture only, no input unless OS permission granted |

**Adding a provider = adding a manifest file, no code:** command + args, install/version probe, "ready" prompt pattern,
headless flag, how to send a task, how to read usage, optional to-do parser.
**Compat probe (Phase 1):** a command that runs each installed provider through a checklist (starts? streams? accepts a task?
exposes todos? exposes usage?) and writes `COMPAT.md`. We certify providers from evidence only.

## 4. The screen
1. **Brain view (home).** The lead's cockpit as one central "brain" node; a spoke to every teammate laptop; a minimized
   live console tile per provider session on each spoke. Edge pulse = live traffic; tile border color = state
   (working / waiting-for-approval / idle / error); a red edge between two tiles when they touch the same files (overlap warning).
2. **Console grid.** Same tiles as a grid, filter by person/provider/state. Tiles render a scaled, read-only terminal
   (a few fps, from server snapshots) so 20 sessions stay cheap.
3. **Focus view (click a tile).** Left: full-rate live terminal. Right tabs: **To-dos/plan**, **Files changed + diff**,
   **Timeline** (events), **Usage**, **Chat/instruct**. Header: provider, person, branch/worktree, state, control mode.
4. **Dashboard.** Team overview, provider matrix, usage and cost, aggregated to-dos, PRs and approvals (existing),
   overlap warnings, audit log, permissions, settings.

## 5. Lead control (with consent)
Per session, a control mode the member grants and can revoke instantly:
**Watch** (default) -> **Suggest** (lead's text appears as a suggestion in the member's session) -> **Co-pilot**
(lead can type; member can veto) -> **Takeover** (exclusive input lock, member sees a banner, auto-expires, fully logged).
Every input from the lead is audited with who/what/when. A member-side **kill switch** drops all remote control at once.

## 6. Usage (never faked)
Per provider/person/task/day: tokens in/out, cost, and rate-limit/window remaining. Sources, best first:
structured adapter > the provider's own usage command > LiteLLM metering > estimate. Every number is labeled with its
source; if unavailable the UI says **"unknown"**, never a made-up value (same rule as the rest of the product).

## 7. How today's engine plugs in
- The existing `Worker` interface (discover / start / send_task / receive / stop / report_usage) is exactly the shape of a
  remote provider. Add `RemoteWorker`: sends a task to a node's session, collects the result from its **git worktree**.
- Contract freeze, scope guard (now enforced on the node too), review, approval-gated merge: unchanged.
- With real teammates, commits use their **real identity with consent** (the synthetic demo emails stay for demos only).

## 8. Security and privacy (this product watches people's machines, so this is a feature, not an afterthought)
- Opt-in per machine and per session. Scope = provider sessions only; no full-desktop capture by default.
- Always-visible "this session is shared / controlled" indicator on the member's machine. Kill switch.
- Secret redaction on the node before data leaves it (known token shapes + entropy), TLS everywhere, pairing codes,
  roles (lead / member / viewer), retention limits, audit log. Optional end-to-end encrypted transcripts.
- Employer-monitoring rules vary by country; consent text and data retention are explicit settings.

## 9. Phases (each ends with a demo plus verification output)
| Phase | Deliverable | Gate |
|---|---|---|
| P0 spike | One provider in a PTY on this PC, streamed over WS into a browser xterm tile; measure latency on Windows | keystroke round trip < 150 ms |
| P1 connect | `kode-node` + pairing + gateway + tiles grid + focus view (view-only) + `COMPAT.md` for installed CLIs | 3 providers live at once |
| P2 Brain | Brain graph UI, presence, multi-node, to-do/file events via structured adapters | 3 node processes on screen |
| P3 control | Control modes, consent, kill switch, audit | takeover works; revoke stops input within 1 s |
| P4 usage | Usage collectors + dashboard with source labels | numbers match provider-reported usage |
| P5 orchestrate | `RemoteWorker`, worktrees, contract/guard on nodes, PR pipeline with real identities | one feature built by 3 real providers, merged only after approval |
| P6 ship | Installers (Win/mac/Linux), manifests for more providers, docs, hardening | clean-machine install in < 5 min |

## 10. Risks and honest limits
- "Every provider" is an ongoing effort; Tier 0 makes most terminal tools work on day one, Tier 1 depth varies by provider.
- GUI-only tools (some IDEs) get picture-only support unless they ship a CLI/protocol.
- Windows pseudo-terminals have quirks; tested first in P0.
- Provider terms of service for automating their CLIs under a user's own account: check each before certifying.
- A hackathon demo with real separate laptops needs a reachable Brain (LAN or tunnel). Simulating several nodes on one PC is
  possible but must be labeled **SIMULATED** in the UI, consistent with the rules we already follow.

## 11. Decisions needed (recommended defaults in bold)
1. Where does the Brain run? **Lead's laptop + a tunnel (fastest for the demo)** / a small cloud server.
2. Demo setup: **real teammate laptops over LAN/tunnel** / several nodes on this one PC (labeled SIMULATED).
3. First providers to certify: **Claude Code, Command Code, Hermes, OpenCode, Antigravity**.
4. Consent default: **view-only until a member grants control per session**.


## 12. P0 results (measured 2026-10-04, this Windows PC)
Built: `backend/bridge/` (PTY session, provider manifests, locked-down WS endpoint, spike page with an xterm tile, latency probe,
compat probe), vendored xterm.js 5.5.0 + fit addon (MIT) in `vendor/xterm/`. Run: `KODE_ENABLE_BRIDGE=1 python run_kode.py`, open `/spike`.
| Gate / question | Result |
|---|---|
| Real CLI session streamed into a browser tile, takes keystrokes back | **PASS** (shell executes typed commands; screenshots `shots/p0-tile-*.png`) |
| Keystroke round trip < 150 ms | **PASS**: median ~2 ms, p95 ~3 ms (4 runs). Known quirk: the FIRST keystroke of a cold session takes ~1.2 s (ConPTY warm-up), every later one ~2 ms |
| Minimized vs enlarged live tile | **PASS**: same live session; type 8px mini / 13px full, re-fits (e.g. 149x28) |
| Provider coverage on this PC (Tier 0, real xterm render) | **6/6 render**: Antigravity CLI, Claude Code, Codex, Hermes, OpenCode (slow 15-30 s first paint), shell. See `backend/COMPAT.md` |
| Security of a "start programs on this host" endpoint | off by default; loopback only; per-start token; Origin check; provider ID allowlist (a smuggled `cmd=` is refused); scrubbed env (no exported API keys). 9 tests |
Findings that change the plan:
1. **A session with no viewer attached stalls some TUIs** (`agy` waits for terminal replies). The Node agent needs a headless terminal emulator to answer them (P1).
2. `agy` ships structured modes (`--output-format stream-json`, `--input-format stream-json`, `--remote-control`): a ready-made Tier 1 adapter target.
3. Provider first-paint time varies from 0.2 s to 30 s, so tiles need a "starting..." state instead of looking dead.
4. One unexplained exit of the dev server was seen once during testing and did not reproduce.
