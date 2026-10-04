# K.O.D.E. v3 plan - team, orchestrator, API profiles, context & usage

Research done 2026-10-05 (web search; the research sub-agents and WebFetch were unavailable on this account, so claims below come from
search summaries and are marked where unverified). Items marked VERIFY are checked on this machine before being shipped.

## What the research says
| Topic | Finding | Decision |
|---|---|---|
| Competitors (Conductor, Claude Squad, Vibe Kanban, Crystal/Nimbalyst) | All isolate agents with git worktrees on ONE machine; Conductor is Mac-only, Claude Squad needs tmux (no Windows), Vibe Kanban's cloud shut down Apr 2026. None offers a team lead cross-machine live consoles with consent. Table stakes: diff review, checkpoints, notifications, kanban/MCP planning | Own the gap: cross-machine, Windows-first, consent ladder. Add needs-input alerts, session logs, search across sessions |
| Orchestrator design (Anthropic multi-agent research write-up) | Each delegated task needs objective + boundaries + output format; scaling rules guide but BUDGETS BIND; ~15x token cost; shared-state coding is not the sweet spot | Orchestrator = delegating lead with hard step/spend caps, file-ownership briefs, "ask before acting" default |
| Access model (Teleport) | Just-in-time access requests with TTL, moderators who can terminate, full audit | Keep the ladder; add team policy `ask` / `copilot` / `full` chosen with the member's explicit acceptance; takeover always time-boxed + visible |
| Custom endpoint: Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (Bearer). Needs Anthropic Messages-compatible endpoint. A saved subscription login wins unless the token var is set; unset `ANTHROPIC_API_KEY` | Profile protocol `anthropic`; inject env per session |
| Custom endpoint: Codex | `[model_providers.x]` (base_url, env_key, wire_api="responses" ONLY since Feb 2026); `-c key=value` per run (VERIFY); local project config cannot set providers | Profile protocol `openai-responses`; inject `-c` flags + key env |
| Custom endpoint: OpenCode | `OPENCODE_CONFIG_CONTENT` inline JSON; `@ai-sdk/openai-compatible` + `baseURL` + `apiKey: "{env:VAR}"` (VERIFY) | Profile protocol `openai-chat`; inject env per session |
| Custom endpoint: Hermes | Only `~/.hermes/config.yaml` / `hermes model` | NOT injectable per session; UI says so |
| Custom endpoint: Antigravity | No custom endpoint mechanism found | Not supported; UI says so |

## Scope of this build
1. **Team**: invite a teammate (name + access policy) -> join link + one-line command; roster page; member accepts the policy explicitly.
2. **Leader access**: standing policies `ask` (default) / `copilot` (sessions start co-pilot) / `full` (takeover requests auto-approved, 10 min cap, banner, kill switch always works).
3. **Orchestrator agent**: LLM agent with tools (list team, read screen/plan/usage, request control, send task, suggest, start session, wait);
   autonomy: observe / ask-before-acting (default) / auto-within-consent; step + token budgets; terminal output treated as untrusted data.
4. **API profiles**: base URL + key (encrypted at rest) + protocol + models + optional prices; test button; chosen per session; also used by the orchestrator.
5. **Usage & context**: model, context-window % (Claude transcripts, Codex `model_context_window`), tokens, cost ESTIMATE from user prices only.
6. **Needs-input detection**, session log download, search across sessions.
7. Tests (including a real orchestrated run), UI screens, docs.
