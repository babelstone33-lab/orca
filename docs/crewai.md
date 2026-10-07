# CrewAI notes

Source: https://github.com/crewAIInc/crewAI (fetched 2026-10-07, via WebFetch, README-level only)

## What it is
Framework for orchestrating role-playing, autonomous agents. MIT license, independent of
LangChain (not built on it). 59.4k stars, 8.7k forks [claimed, from GitHub page at fetch time].

## Primitives (as documented)
- Agent: role, goal, backstory, LLM, tools, behavior. The backstory/role framing is a prompting
  technique (it steers the system prompt), not a new mechanism.
- Task: a unit of work assigned to an agent, with an expected output, and `context` linking to
  prior tasks' results (e.g. `"context": ["research_task"]`), i.e. explicit task dependencies.
- Crew: a team of agents running a set of tasks together.
- Process: execution strategy for a Crew (sequential, hierarchical, etc.)
- Flow: event-driven control layer, for cases where a Crew's more autonomous task-passing is too
  loose and the caller wants precise control over execution order and branching.

## What is worth stealing
1. Task.context (dependency list by name): a task names the prior tasks whose output it needs,
   and the runner resolves that into "wait for X, Y, then pass their outputs in." This is the
   literal shape for Orca's workflow YAML `depends_on` list. Simple, readable, no graph library
   needed to express it.
2. Role/goal/backstory as agent identity: maps directly to Orca's Agent file (name, role, allowed
   tools, model, memory dir). Worth keeping as plain fields, not as a prompting ritual.
3. Process as an explicit, named execution strategy (sequential vs hierarchical) rather than an
   implicit graph: confirms Orca should ship exactly one template (sequential-with-dependencies)
   rather than open-ended graph topology on day one.
4. Flow existing as a separate, stricter layer from Crew is itself a signal: CrewAI's own authors
   found "agents improvise the order" (Crew) insufficient for production and added a
   deterministic layer (Flow). Orca should start at the deterministic layer directly: a Supervisor
   that assigns fixed subtasks from a YAML workflow, not a crew that negotiates among itself.

## What is explicitly not stolen
- No separate "Crew" object: Orca's Supervisor already plays that role (it is the one thing that
  holds a set of agents and a set of tasks together for a run).
- No "hierarchical process" auto-delegation: fan-out is capped and explicit in Orca (see
  engine-design.md, Supervisor section), not emergent from letting agents decide to delegate.
