# Provider layer: what the two frameworks actually do

Source-verified by reading their code, not their READMEs. Every line reference below was read
during this session. Where a claim is a judgement rather than a fact it says so.

## CrewAI

| Question | Finding | Source |
|---|---|---|
| How an agent gets a model | `Agent.llm` accepts `str \| BaseLLM \| None`; a string goes through `create_llm` to `LLM(model=...)` | `lib/crewai/src/crewai/agent/core.py:270`, `utilities/llm_utils.py:38` |
| Per-agent override | Yes. Every agent runs the model it declared; a crew never assigns one | `agent/core.py:409-423` |
| Separate model for tools | Yes. `Agent.function_calling_llm` overrides the crew-level one, used only for tool calls | `agent/core.py:275-283` |
| Crew-level model fields | `manager_llm`, `manager_agent`, `function_calling_llm` (deprecated at crew level), `planning_llm`, `chat_llm` | `crew.py:278,283,287,351,379` |
| String to client | `LLM.__new__` is a factory: overlay map, then a route resolver, then a native class, else LiteLLM | `llm.py:283-346`, `_resolve_route` `llm.py:452-515` |
| Native providers | A dispatch returning a class per provider: OpenAI, Anthropic, Azure, Gemini, Bedrock, Snowflake, OpenAICompatible | `_get_native_provider` `llm.py:610-661` |
| Aliases | A dict, e.g. `claude` to `anthropic`, `google` to `gemini`, `aws` to `bedrock` | `llm.py:182-224` |
| Credentials | Per-provider env vars read inside each provider client, e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`; optional `api_key`/`base_url` fields; no credential file | `providers/openai/completion.py:554`, `providers/anthropic/completion.py:360`, `constants.py:29` |
| Cross-provider fallback | **Not present.** Only transient rate-limit retries on the same provider (3 attempts), a LiteLLM construction fallback, and an OpenAI Responses-vs-chat API-shape retry | `llms/retry.py:124`, `llm.py:327`, `providers/openai/completion.py:658-724` |
| Which model served | **Not recorded.** Events carry the requested `model`; usage is tracked, served model is not | `llms/base_llm.py:674-693`, `events/types/llm_events.py:12` |

## LangChain and LangGraph

| Question | Finding | Source |
|---|---|---|
| String to client | A data table `_BUILTIN_PROVIDERS`: provider key to (module, class, factory). A colon splits `provider:model` only when the prefix is in the table, otherwise the model name infers the provider | `libs/langchain_v1/langchain/chat_models/base.py:56-99`, `_parse_model:623` |
| Credentials | `init_chat_model` never reads them; the provider class resolves env vars. Precedence: explicit value, then provider env var, then gateway | `libs/core/langchain_core/utils/_gateway.py:27,120` |
| Fallback chain | Real. `with_fallbacks(fallbacks, exceptions_to_handle=(Exception,), exception_key=None)`. On total failure it re-raises the FIRST error. `exception_key` can inject the prior error into the next attempt's input | `runnables/base.py:2189`, `runnables/fallbacks.py:37,93,211` |
| Which runnable served | **Not in the returned data.** Visible only in callback child-run structure. No field names the winner | `runnables/fallbacks.py:178-190` |
| Runtime model choice | `_ConfigurableModel`, a private class, chosen per call via `config={"configurable": {"model": ...}}` | `chat_models/base.py:529,715-731` |
| Per-node model in LangGraph | Yes, a dynamic-model callable: `select_model(state, runtime)` resolving from `runtime.context` | `libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py:278,599` |
| Which model served | `AIMessage.response_metadata["model_name"]` and `["model_provider"]`, plus `usage_metadata`. Caveat in their own code: this reflects the requested model unless a gateway re-routes | `libs/core/langchain_core/messages/ai.py:176`, `openai_base.py:5184` |

## The three things worth taking

1. **Dual models per agent.** A cheap model for text, a stronger one for tool calls, declared
   separately. This is the real cost lever, and it is CrewAI's best idea. For a single owner the
   useful form is two fields on the agent, `llm` and `tool_llm`, not CrewAI's five.

2. **The provider registry is data, not code.** LangChain's table of name to module/class/factory
   is the shape that lets a new provider be a config entry. CrewAI needs a class per provider plus
   an optional LiteLLM dependency; that weight buys nothing here.

3. **Raise the first error when every fallback fails.** LangChain's semantics. Easy to get wrong by
   raising the last one, which hides the real cause.

## The two gaps neither framework closes

Both are gaps Mor asked to close, before reading either.

- **No cross-provider fallback in CrewAI at all.** Orca's ordered `fallback` list is not
  reinventing anything; it fills a hole.
- **Neither reports which model actually served when a fallback fired.** CrewAI records the
  requested model only. LangChain records `model_name` and admits it reflects the request unless a
  gateway re-routed, and `with_fallbacks` never names the winning runnable. So the run record's
  `requested_provider` / `served_provider` / `fallback` / `reason` quartet is ahead of both, and it
  is the cheapest thing in this design to build.

## Rejected, with reasons

- **A per-provider class hierarchy.** A data table plus two transports covers the same ground in
  fewer lines. Reach for classes when a provider genuinely needs different code, not different
  strings.
- **Bundling LiteLLM** for provider breadth. It is a large dependency that would sit between the
  engine and any local endpoint already speaking the OpenAI schema.
- **A graph engine for task ordering.** Neither framework's graph layer earns its weight for a
  workflow that is a small DAG. See `docs/DESIGN.md`.
- **agy's position that the CLI transport should not exist at all.** Correct on the facts it cites
  (no tool calls, no HTTP error codes, no clean token metadata), wrong on the conclusion: a Claude
  Pro/Max or Google AI Pro subscription has no API key, so the CLI is the only way to spend that
  quota. The transport stays, declared as degraded (`tools: false`, `usage: false`,
  `structured_errors: false`) and costed in quota, never in dollars.
