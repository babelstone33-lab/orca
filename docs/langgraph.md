# LangGraph notes

Source: https://github.com/langchain-ai/langgraph (fetched 2026-10-07, via WebFetch, README-level only)

## What it is
Low-level orchestration framework for long-running, stateful agents, built as a graph of nodes
and edges over a shared state object. MIT license. 42.8k stars, 7.3k forks [claimed, from GitHub
page at fetch time, not independently counted].

## Primitives (as documented)
- Graph: nodes (steps) and edges (control flow), including conditional edges and subgraphs.
- State: a shared, typed object threaded through every node; nodes read and write it.
- Checkpointer: snapshots state after each step to a backend (memory, sqlite, postgres).
- Durable execution: a run "persists through failures" and "automatically resumes from exactly
  where it left off" [claimed, README wording, not benchmarked by us].

## What checkpointing buys
- Resume-from-crash: if the process dies mid-run, the next invocation can reload the last
  checkpoint and continue instead of restarting the whole graph.
- Time-travel / inspection: because every step's state is a row, you can replay or branch from
  any past step (documented capability, not verified here).
- Human-in-the-loop: a node can pause and wait for approval, because the paused state is already
  durable on disk, not held only in process memory.

## Is it worth building on (runtime dependency)? No. Reasons:
1. The brief forbids a runtime dependency on it; this is a given, not re-litigated here.
2. It is a *framework*, not a library: it wants you to express the whole app as its Graph object,
   which means debugging and reasoning happen in its vocabulary (nodes, edges, superstep), not
   ours. That is the generic "wrapper tax" the brief wants to avoid.
3. The one idea genuinely worth lifting is small: "checkpoint state to disk after each step, key
   it by run id, reload on resume." That is a few dozen lines, not a dependency.
4. Multi-backend checkpointers (sqlite/postgres) solve a scale problem (many concurrent runs,
   many users) Orca does not have on day one (one owner, one host). A single JSON/JSONL file per
   run covers it.

## Load-bearing idea extracted for Orca
Durability = (a) state is a plain serializable object, (b) it is written to disk after every
state-changing step, (c) a run id maps 1:1 to a file/directory, (d) resume = read the last
checkpoint and continue the step sequence. This is Orca's Run durability model (see
engine-design.md), implemented with stdlib json, not LangGraph's checkpointer abstraction.
