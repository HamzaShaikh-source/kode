# K.O.D.E. v4 plan - projects: one brief, the whole team builds it

Why: at a hackathon everyone has a coding agent, but prompts are shared by hand, nobody knows who is doing what, and commits are lopsided.
K.O.D.E. now plans one project, assigns it fairly, drives each laptop's agent, commits through each laptop's own `gh`, merges, tests and fixes.

## Decisions
| Topic | Decision |
|---|---|
| Lead access | Total by default: invites default to `full`; a node accepts any policy up to `--access-limit` (default `full`); the lead can raise a machine to full from the Team page. The teammate keeps the kill switch. |
| Who commits | Each laptop, with its own git identity and `gh` login (demo fleets pass `--git-name/--git-email`). The Brain never holds a GitHub token. |
| How work reaches an agent | The Brain writes `.kode/TASK.md` in the task's clone and types ONE line into the agent's terminal telling it to read it (multi-line prompts submit early in TUIs). |
| Isolation | Planner must give disjoint `files_owned`; code rejects overlap. On finish, code reverts out-of-scope and contract edits. |
| Contract | Files in the plan's `contract_files` are committed first and frozen (sha256 kept); no task may own them; any edit is reverted. |
| Fairness | Weighted longest-processing-time assignment; contribution means a meaningful share, not equal commit counts (one commit is fine for a simple task); a contribution report is built from git history; a final quality gate (hygiene audit + AI review) opens polish tasks; repository collaborators are invited and accepted automatically. |
| Merge | `gh pr create` from the owner's laptop, `gh pr merge --merge` (no squash, so authorship survives) from the coordinator, serialized, in dependency order. Conflicts: agent is asked to rebase, then an automatic rebase prefers the branch's own side. |
| Verify / fix | Coordinator pulls the base branch and runs setup + test commands. Failure -> LLM triage -> fix tasks for the owner of the failing area, max 3 rounds. |
| Unattended agents | Per-provider launch flags (`opencode --agent build --auto`, `claude --permission-mode acceptEdits`), plus a prompt policy: Enter on routine prompts, a card for anything destructive. Node-side allowlist enforces which flags a hub may inject. |
| Offline rehearsal | Provider `demo-agent` follows a script embedded in the task file; used by the hermetic end-to-end test. Forge `local` uses a bare git repo instead of GitHub. |

## Not done / known limits
Codex and Hermes/Antigravity launch flags for unattended work are untested; planner quality depends on the model; one task per laptop at a time;
no live re-planning mid-build (use pause, re-assign, or fix rounds).
