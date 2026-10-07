# Orca: design document

Session: research and design only (session id 7b888a7b-5e3f-4209-8d54-6e63a7e13b94). No code
shipped in this session. Reasoning trail lives in ../notes/{langgraph,crewai,curriculum,
engine-design}.md.

Orca is a thin, personal agent-orchestration engine. It is a tool Hermes calls, not a second
orchestrator and not a separate agent brain. Standard library and already-installed packages
only; no runtime dependency on LangGraph or CrewAI.

## 1. Primitives: four, not five

Agent, Task, Workflow, Run. Supervisor is cut as a standalone primitive and folded into Agent.

- **Agent**: name, role, allowed tools, model, memory directory. The only place these four
  things live, so a Task never re-specifies them.
- **Task**: one workflow-file entry: which agent, what instruction, which prior tasks' outputs it
  needs (`depends_on`), what name its own output is stored under. Not a Python class with a
  lifecycle; the step loop reads it as a dict.
- **Workflow**: a YAML file naming a list of tasks (optionally with a `supervisor` block). This is
  the thing Hermes names when it calls `orca run <workflow>`.
- **Run**: one execution of a workflow. Has an id, a state file, a log, a running cost total. The
  unit durability and the Run record are built around.

Why cut Supervisor: everything a supervisor needs (a role, a model, a memory dir) is already an
Agent field. What makes it a supervisor is behavior, not structure: its task list is generated at
run time from a goal, instead of fixed in the workflow YAML. Adding a 5th primitive for that
would be an interface with effectively one implementation, the thing the brief says to avoid.

## 2. Durability

Lifted idea from LangGraph (notes/langgraph.md): checkpoint plain state to disk after every step,
key it by run id, resume by reloading the last checkpoint. Not lifted: LangGraph's checkpointer
abstraction (pluggable memory/sqlite/postgres backends). That solves multi-tenant scale; Orca has
one host and one owner, so it is a config value that never changes until proven otherwise.

Implementation, stdlib only:
- `runs/<run_id>/state.json`: single source of truth. Written atomically (write `.tmp`, then
  `os.replace`) after every task finishes. Holds run id, workflow name, status, completed task
  ids with outputs, next task id.
- `runs/<run_id>/log.jsonl`: append-only, one JSON object per event (task started/finished/error,
  model call). Never rewritten, so a crash mid-write loses at most one incomplete line, not
  history.
- Resume: `orca run <workflow> --resume <run_id>` reads `state.json`, finds the first task not
  marked completed, continues from there. Finished tasks are never re-run.

Do we need checkpointing's full power on day one? No. We need exactly the "resume without
re-running finished work" guarantee, which is the json-file-plus-atomic-write pattern above, not
a backend-swappable store.

## 3. Run record

The owner's question is concrete: which model served each request, and when did a fallback
happen, visibly, not as a silent swap. `state.json` carries:

- `run_id`, `workflow`, `started_at`, `status` (running / done / failed / cost_capped)
- `tasks`: `{task_id: {status, output, started_at, finished_at}}`
- `model_calls`: list of `{task_id, requested_model, served_model, fallback: bool, reason,
  input_tokens, output_tokens, cost_usd, timestamp}`. `requested_model != served_model` is a field
  the owner can grep for, not something inferred after the fact.
- `total_cost_usd`: running sum updated after every model call.

`log.jsonl` carries the same model-call and task events in chronological order (for `tail -f`
while a run is in flight); `state.json` carries the current-state snapshot (for "where are we
now"). Two files because those are two different access patterns, not two sources of truth; state
is always derivable by replaying the log, kept as a file only so callers do not have to.

## 4. Supervisor, and avoiding the fan-out cost trap

The trap (seen in CrewAI's own split between Crew and Flow, notes/crewai.md): a planner that
free-decomposes a goal can turn one $0.50 task into a $20 one by fanning out without limit, and
nothing stops it from re-planning again after execution.

The Supervisor is an Agent (role: supervisor) whose task list is generated, not fixed, bounded by
explicit, workflow-level config, not learned behavior:
- `max_subtasks` on the workflow (small default, e.g. 5). The planning call is instructed to
  produce at most that many; the runner truncates/rejects a larger plan rather than honoring it.
- Decomposition is exactly one model call. The supervisor does not get to re-plan mid-run; a
  second planning pass would hit the same cap again, by construction, not by willpower.
- `max_cost_usd` on the workflow. Before starting each task, the step loop checks
  `total_cost_usd` against it and stops the run (status `cost_capped`) rather than finishing an
  over-budget run quietly.
- No recursive supervisors by default: a subtask's agent is a fixed-role specialist, not another
  supervisor. Fan-out cannot compound across levels unless a workflow author explicitly nests one,
  which is then visible in the YAML, not an emergent choice an agent made on its own.

## 5. Per-agent memory: information that sits in git

Layout, one directory per agent, committed to the monorepo:

```
memory/
  <agent_name>/
    profile.md       # role, goal, constraints; hand-written, rarely changes
    index.md          # one line per notes file, same pattern as this session's MEMORY.md
    notes/
      <topic>.md      # durable knowledge the agent accumulated, appended over time
```

Write path: a task's output may include an explicit `memory_update: {topic, text}` field (never
inferred from free text). The runner appends `text` to `memory/<agent>/notes/<topic>.md` and adds
or updates the one-line pointer in `index.md`.

Read path: before a task starts, the runner loads that agent's `index.md` plus any notes file the
workflow YAML names for that task, and includes it in the agent's prompt.

No embedding search, no vector store: one owner, a handful of agents, at most a few dozen notes
files, all grep-sized. This is smaller on purpose than LangGraph's long-term memory store; if it
ever stops fitting in a prompt, the upgrade path is a local keyword grep over `memory/`, still no
new dependency.

## 6. Workflow schema

```yaml
name: build-landing-page
max_subtasks: 5          # only meaningful if a supervisor block is present
max_cost_usd: 2.00

supervisor:               # optional; omit for a fixed task list
  goal: "Draft and review a landing page for product X"

tasks:
  - id: research
    agent: researcher
    instruction: "Summarize what competitors' landing pages for X do well."
    output: research_notes

  - id: draft
    agent: writer
    depends_on: [research]
    instruction: "Write landing page copy using {{research_notes}}."
    output: draft_copy

  - id: review
    agent: reviewer
    depends_on: [draft]
    instruction: "Review {{draft_copy}} for clarity and tone."
    output: final_report
```

`depends_on` is CrewAI's `Task.context` idea (notes/crewai.md): a task names the prior tasks whose
output it needs; `{{output_name}}` interpolates that output into the instruction text. The runner
resolves dependencies into an execution order; no graph library needed for this shape.

The one template that covers most real cases: **linear-with-fan-in**. A small number of tasks,
each depending on one shared setup task, funnelling into one shared final task (as above:
research -> draft -> review). This covers the overwhelming majority of real workflows without
needing arbitrary graph topology on day one.

## 7. Hermes plugin interface

One tool: `orca_run(workflow: str, params: dict) -> RunReport`.

Contract:
- Synchronous: returns once the run reaches a terminal state (`done`, `failed`, `cost_capped`).
- The run id is emitted on the first line of output immediately, before the run blocks, so a
  caller that wants to check progress mid-run can read `runs/<run_id>/log.jsonl` (e.g. `tail -f`)
  without a second tool or callback. This mirrors how Hermes already treats a long shell command:
  run it, inspect its output file; Orca does not need its own notification channel.
- Return value (the report): `run_id`, `status`, `total_cost_usd`, one-line-per-task summaries,
  and the final task's output. Hermes formats this for the user; Orca returns data, not chat
  prose.

## 8. First milestone

A two-task workflow, no supervisor fan-out yet:
1. `research` (agent: researcher) answers one fixed small question.
2. `write` (agent: writer), `depends_on: [research]`, writes one paragraph using the research
   output.

What this proves end to end: Agent config loading; Task dependency resolution and `{{output}}`
interpolation; Run durability (kill the process after task 1 completes, resume with
`--resume <run_id>`, confirm task 1 is not re-run and task 2 completes); Run record correctness
(model used, cost, log present and consistent with state.json); and the Hermes plugin call
returning a report shaped as above. The Supervisor's decomposition step is deliberately excluded,
to keep this milestone finishable; it is milestone two.

## Sources worth building on: verdicts

**LangGraph** (42.8k stars [claimed, GitHub page at fetch time], MIT): not worth a runtime
dependency. It is a framework that wants the whole app expressed as its Graph object; that is
the wrapper tax the brief is written to avoid. The one idea worth lifting, checkpoint-state-after-
every-step-and-resume-from-it, is a few dozen lines of stdlib json, not a library. Full reasoning
in notes/langgraph.md.

**CrewAI** (59.4k stars [claimed, GitHub page at fetch time], MIT, independent of LangChain): not
worth a runtime dependency either, but its vocabulary maps closely onto Orca's own: Agent
(role/goal/backstory -> role/goal/constraints), Task.context (-> `depends_on`), and the
Crew-vs-Flow split (CrewAI's own authors found loose task-passing insufficient for production and
added a deterministic Flow layer; Orca starts at that deterministic layer directly, via the
Supervisor's bounded plan). Full reasoning in notes/crewai.md.

**langraph-tutorials** (10 notebooks, LangGraph 1.1.9 [claimed, per repo at fetch time]): per-
notebook adopt/adapt/skip verdicts are in notes/curriculum.md. Short version: notebooks 1, 2, 4,
5, 8, 9 are worth working through because they teach ideas Orca reimplements (routing, state
persistence, human-in-the-loop, long-term memory, multi-agent coordination); 3, 6, 7, and the
platform half of 10 are skip or skim-only, either API-specific to LangChain tool-calling or aimed
at scale Orca does not have on day one.

## What this session did not do

No code was written. No repo was created under /home/moris/orca. This is the design the next
session should build against: 4 primitives, a json-checkpoint Run, a git-backed per-agent memory
tree, one workflow template, and a single Hermes tool contract.
