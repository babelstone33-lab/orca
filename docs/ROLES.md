# Orca: roles and routing

Four actors. Each has one job, and no job is done by two of them.

| Actor | Role | Runs on | Cost |
|---|---|---|---|
| Hermes | Orchestrator: memory, git, briefing, routing, reporting to Mor | Mycombo (DeepSeek via 9router) | cheap tokens |
| Claude Code | Builder: writes code, closes design gaps, deep implementation | Mor's Claude Pro/Max subscription | quota, not tokens |
| Antigravity (`agy`) | Reviewer: audits what the builder produced, second opinion | Mor's Google AI Pro subscription | quota, not tokens |
| Orca | The product under construction | local, stdlib only | free |

## Why the builder does not review itself

A review by the model that wrote the code adds little. Claude writes, `agy` audits. Two different
model families, two separate quota pools, so the audit costs no Claude window.

## Routing rules

1. **Design and code** go to Claude, `sonnet`, one notes file per lane written incrementally. Fixed
   session id for the topic, resumed rather than restarted:
   `7b888a7b-5e3f-4209-8d54-6e63a7e13b94`.
   A coding launch may open **at most two children, reviewers only, never writers**, and the cap is
   written into the brief. `Task` must be in `--allowedTools` for that to be possible at all: the
   tool is refused mechanically when it is absent from the list.
2. **Audit and second opinion** go to `agy` via `~/.hermes/scripts/agy-ask.sh`, mode `plan`
   (read-only). It reads the tree and reports; it does not edit.
3. **Integration, commits, tests** stay with Hermes. Nothing else pushes to the repo.
4. **A claim from either actor is a self-report until Hermes verifies it.** Claude saying "written"
   means nothing without a path and a line count. `agy` saying "looks right" means nothing without
   a specific finding.

## Cost gates

- Gate on headroom before every Claude dispatch, never at the start of the turn.
- Never more than one Claude session in flight for this project.
- No subagents in a Claude brief for this project. A fan-out once burned a full 5-hour window in
  34 minutes and produced nothing.
