# Milestone 4 spec: the provider layer (one agent, one model, one auth)

Mor's requirement, in his words: every agent may use a different LLM, a different API, a local
model, or an authenticated subscription. The engine must not assume one endpoint.

## Design: two transports, one interface

Everything he listed collapses into exactly two call shapes. Do not build more.

| kind | Covers | Auth modes |
|---|---|---|
| `openai` | any OpenAI-compatible HTTP endpoint: 9router, OpenRouter, OpenAI, Anthropic-compat gateways, Ollama, LM Studio, llama.cpp, vLLM | `none` (local), `bearer_env` (key from an env var) |
| `cli` | a subscription that authenticates through a logged-in CLI, not an HTTP key: Claude Code (`claude -p`), Antigravity (`agy -p`) | the CLI's own session, no key in config |

That is the whole matrix. A new provider is a config entry, never a code change.

## Config

`providers.yaml` at the repo root, one entry per endpoint:

```yaml
providers:
  router:
    kind: openai
    base_url: http://127.0.0.1:20128/v1
    auth: {mode: bearer_env, var: ORCA_API_KEY}
  ollama:
    kind: openai
    base_url: http://127.0.0.1:11434/v1
    auth: {mode: none}
  claude:
    kind: cli
    command: /home/moris/.local/bin/claude
    args: ["-p", "{prompt}", "--model", "{model}", "--output-format", "json"]
  antigravity:
    kind: cli
    command: /home/moris/.hermes/scripts/agy-ask.sh
    args: ["{prompt}", "{cwd}", "{model}"]
```

No secret ever lives in this file. Only the name of an environment variable does. `.env` holds the
values and stays gitignored.

## Agent frontmatter

```yaml
---
provider: router          # optional; defaults to the workflow's provider, then to `router`
model: ds/deepseek-v4-pro # optional; defaults per provider
fallback: [ollama:llama3, claude:haiku]   # optional, ordered
---
```

`fallback` is the part Mor already asked for in a different form: he wants to know when a provider
was swapped, never a silent swap. So on every fallback the run record writes
`requested_provider`, `served_provider`, `fallback: true` and a `reason`. A fallback that cannot be
observed is the same defect as no fallback at all.

## Run record additions

`model_calls[]` gains `requested_provider` and `served_provider` beside the existing
`requested_model` / `served_model`, plus `transport` (`openai` or `cli`) and the existing token and
cost fields. Cost for a `cli` transport is quota, not money: record it as `cost_usd: null` with
`cost_basis: "quota"`, never as a fabricated number.

## What stays out

- No plugin registry, no adapter class per provider, no dynamic import. Two branches in one
  function.
- No OAuth flow written by hand. A subscription is reached through the CLI that already holds the
  session.
- No secret on disk outside `.env`.

## Acceptance

1. One workflow with three tasks, each on a different provider: a local model with no auth, a
   hosted model through the router with a bearer key, and a CLI subscription. Runs end to end.
2. `tests/selfcheck.py` proves provider selection and the fallback path with the offline echo
   transport, and labels any live-only check loudly instead of passing it silently.
3. A forced bad provider (wrong port) falls back to the next entry in `fallback` and the run record
   shows all three fields: requested, served, reason.
