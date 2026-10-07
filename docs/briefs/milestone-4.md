# Milestone 4 spec: the provider layer (rewritten against source)

This supersedes the earlier draft of this file, which was written from memory. The corrections come
from reading real source, recorded in `docs/research/provider-layer-map.md`: CrewAI's `llm.py` and
`agent/core.py`, LangChain's `chat_models/base.py` and `runnables/fallbacks.py`. Every line
reference there was read this session by two subagents and re-checked by the orchestrator.

Mor's requirement, in his words: every agent may use a different LLM, a different API, a local
model, or an authenticated subscription. The engine must not assume one endpoint.

## Two transports, one interface

| kind | Covers | Auth |
|---|---|---|
| `openai` | any OpenAI-compatible endpoint: 9router, OpenRouter, OpenAI, Ollama, LM Studio, vLLM | `none` or `bearer_env` |
| `cli` | a subscription with no API key: `claude -p`, `agy -p` | the CLI's own logged-in session |

Capabilities are declared per provider, never assumed:

```yaml
capabilities: {tools: true, usage: true, structured_errors: true}
```

A CLI provider declares `tools: false, usage: false, structured_errors: false`. The engine refuses
to route a task that needs tools to a provider without them. A hard failure at startup or dispatch,
never a silent downgrade. This answers the live 404 found in milestone 3: an agent pointing at a
model the endpoint does not expose must stop the run, not fall through to another provider by
accident.

## The registry is data, not classes

`providers.yaml`, one table. No class per provider, no import per provider, no LiteLLM dependency.
LangChain's `_BUILTIN_PROVIDERS` (name to module/class/factory, `base.py:56-99`) proves a table
covers this. CrewAI pays a class per provider plus a LiteLLM fallback; that weight buys nothing here.

```yaml
providers:
  router:  {kind: openai, base_url: http://127.0.0.1:20128/v1, auth: {mode: bearer_env, var: ORCA_API_KEY}}
  ollama:  {kind: openai, base_url: http://127.0.0.1:11434/v1, auth: {mode: none}}
  claude:  {kind: cli, command: /home/moris/.local/bin/claude, model: sonnet,
            capabilities: {tools: false, usage: false, structured_errors: false}}
  agy:     {kind: cli, command: /home/moris/.hermes/scripts/agy-ask.sh, model: gemini-3.1-pro-high,
            capabilities: {tools: false, usage: false, structured_errors: false}}
```

No secret in this file, only the name of an environment variable. Values live in `.env`, gitignored.

## Two models per agent

Taken from CrewAI's `llm` plus `function_calling_llm` split (`agent/core.py:270,275`), cut down to
the useful two. This is the real cost lever: a cheap model for text, a capable one for tool calls.

```yaml
---
provider: router
model: ds/deepseek-v4-pro        # text work
tool_provider: router            # optional; only consulted when the task uses tools
tool_model: MyThinkingcombo
fallback: [ollama:llama3]        # ordered, tried on failure of any attempt
---
```

`model` alone is enough. `tool_model` is opt-in. CrewAI carries five model fields at crew level
(`manager_llm`, `manager_agent`, `function_calling_llm`, `planning_llm`, `chat_llm`); for a single
owner, two at agent level covers the ground.

**Model ids must exist.** Every declared model is validated against the provider's live model list
at startup; a dangling reference refuses the run. Verified basis: this 9router instance exposes 25
ids, none Anthropic, so `model: sonnet` against it must fail loudly rather than 404 mid-run.

## Fallback, observable by construction

An ordered list, tried on error, not on a poor answer. When every entry fails, raise the FIRST error
(LangChain's semantics, `fallbacks.py:211`; raising the last hides the real cause).

Every attempt writes to the run record:

```
requested_provider, requested_model, served_provider, served_model, fallback, reason
```

Neither framework does this, and it is the cheapest thing in this design to build:

- CrewAI has no cross-provider fallback at all, and its events carry the requested model only
  (`llms/base_llm.py:674`).
- LangChain has `with_fallbacks`, but the winning runnable appears only in callback child-run
  structure, never in the returned data (`fallbacks.py:178-190`), and its own code notes
  `model_name` reflects the request unless a gateway re-routed.

## Cost

`openai` transport: tokens from the response, price from `engine/prices.json`, `cost_usd` a real
number. `cli` transport: `cost_usd: null`, `cost_basis: "quota"`. Never fabricate a dollar figure
for a subscription.

## What stays out

- No class per provider, no dynamic import, no LiteLLM. Two branches in one function.
- No hand-written OAuth. A subscription is reached through the CLI that already holds the session.
- No secret on disk outside `.env`.

## How to work

Incremental, because a quota wall can land mid-run. After each of the four build steps below,
run the self-check, then commit locally (`git add -A && git commit`), so an interruption costs at
most one step rather than the whole milestone. Do not push.

Order: (1) `providers.yaml` plus the loader and capability validation, (2) the `openai` transport
with tokens and cost, (3) the `cli` transport declared as degraded, (4) the ordered fallback chain
plus the four requested-versus-served fields. Steps 2 and 3 are independent of 4; if the window
runs short, 1 to 3 landing complete and verified is a good result and 4 is a fine next-session task.

## Acceptance

1. A three-task workflow, each task on a different provider: no-auth local, bearer-key hosted, CLI
   subscription. Runs end to end with the real output pasted.
2. `tests/selfcheck.py` proves offline: per-task provider selection, `tool_model` chosen only when
   tools are used, dangling-model refusal, the fallback chain, and that the requested-versus-served
   fields are written on a forced failure. Live checks skip loudly.
3. Force a bad provider (wrong port). The next fallback entry serves it and the run record shows
   requested, served and reason for both attempts.
