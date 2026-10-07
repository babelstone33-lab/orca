# Engine design working notes

Scratch space for the 8 required decisions before writing docs/DESIGN.md. Decisions themselves
land in DESIGN.md; this file is the reasoning trail.

## 1. Primitives
Candidates given: Agent, Task, Workflow, Supervisor, Run. Check each against "does it need to
exist":
- Agent: yes. It is the only place role, allowed tools, model, and memory dir live. Without it
  every task would re-specify all four.
- Task: yes, but as a workflow-file concept (a YAML entry), not a Python class with its own
  lifecycle. A Task is "one step: which agent, what instruction, inputs from which prior tasks,
  what output name." No separate Task object with methods; the Run step loop reads the dict.
- Workflow: yes. The YAML file itself. It is the thing Hermes names when it calls `orca run`.
- Supervisor: yes, but it is just an Agent whose job is decompose+assign+collect. Not a 6th
  primitive, a role an Agent plays (see DESIGN.md primitives section for the final framing:
  4 primitives, Supervisor folds into Agent).
- Run: yes. The unit of durability, the thing with an id, a state file, a log, a cost total.

Net: 4 primitives (Agent, Task, Workflow, Run). Supervisor is not a 5th thing, it is an Agent
config (role: supervisor) plus the step-loop behavior of "this agent's task list is generated at
run time, not fixed in the YAML." Keeps the "at most five, justify or replace" instruction honest
by cutting one instead of padding to five.

## 2. Durability
Simplest thing that works, lifted from LangGraph's checkpoint idea (see notes/langgraph.md) but
implemented with stdlib only:
- Run directory: `runs/<run_id>/`
- `state.json`: the single source of truth. Written atomically (write to `.tmp`, os.replace) after
  every task completes. Contains: run id, workflow name, status, completed task ids with their
  outputs, the next task id to run.
- `log.jsonl`: append-only, one JSON object per line, one line per event (task started, task
  finished, model call, error). Never rewritten, so a crash mid-write only loses the last
  incomplete line, not history.
- Resume: `orca run <workflow> --resume <run_id>` reads `state.json`, finds the first task not in
  `completed`, and continues the step loop from there. No replay of finished tasks.
- Do we need LangGraph's checkpointer abstraction on day one? No. It is built for swapping
  storage backends (memory/sqlite/postgres) and multi-tenant scale. Orca has one host, one state
  file format, one owner. A pluggable backend is exactly the "config value that never changes"
  the brief says to cut, until there is a second backend anyone actually wants.

## 3. Run record
Fields `state.json` must carry, driven by the owner's actual question ("which model served each
request, when did a fallback happen"):
- `run_id`, `workflow`, `started_at`, `status` (running/done/failed)
- `tasks`: map of task_id -> {status, output, started_at, finished_at}
- `model_calls`: list of {task_id, requested_model, served_model, fallback: bool, reason,
  input_tokens, output_tokens, cost_usd, timestamp}. `requested_model != served_model` is exactly
  the "silent provider swap" the owner wants visible: it is a field, not an inference.
- `total_cost_usd`: running sum, updated after each model call, so a long run's cost is always
  readable without summing the log.
`log.jsonl` carries the same model_call events plus task start/finish/error events, for a
chronological view; `state.json` carries the current-state view. Two files because "what happened
in order" and "what is true right now" are different questions with different access patterns
(tail -f the log while watching; read state.json for a snapshot).

## 4. Supervisor and the fan-out cost trap
The trap: a supervisor that calls an LLM to plan, then calls an LLM per subtask to execute, then
calls an LLM again to merge, multiplies cost and latency with every extra subtask, and a careless
decomposition (e.g. "one subtask per file") turns a $0.50 task into a $20 one silently.
Guardrails, all explicit and configured per-workflow, not learned:
- The decomposition step itself is bounded: the workflow YAML sets `max_subtasks` (default small,
  e.g. 5). The supervisor's planning call is instructed to produce at most that many, and the
  runner rejects/truncates a larger plan rather than silently fanning out further.
- Decomposition is one model call, not iterative; the supervisor does not get to re-plan mid-run
  without hitting the same cap again.
- Subtasks run against the Run's cost ceiling (a `max_cost_usd` on the workflow); the step loop
  checks `total_cost_usd` before starting each task and stops (status: failed, reason: cost_cap)
  rather than finishing an over-budget run quietly.
- No recursive supervisors by default: a subtask's agent is a specialist (fixed role, fixed
  model), not another supervisor, so fan-out cannot compound across levels unless a workflow
  explicitly nests one (and nesting is a workflow-author choice, visible in the YAML, not emergent
  agent behavior).

## 5. Per-agent memory ("information that sits in git")
Layout: `memory/<agent_name>/` as a directory of markdown files, committed to the same monorepo.
- `memory/<agent_name>/profile.md`: static identity (role, goal, constraints), hand-written or
  rarely updated, same spirit as this project's own CLAUDE.md-style memory.
- `memory/<agent_name>/notes/<topic>.md`: one file per topic the agent has accumulated durable
  knowledge about, written by the agent itself at the end of a run (append, not rewrite) when it
  learns something worth keeping across runs.
- `memory/<agent_name>/index.md`: one-line-per-file pointer list, same pattern as this session's
  own MEMORY.md, so reading the index costs little and finding the right notes file is cheap.
Write path: at the end of a task, if the agent's output includes a `memory_update` field (explicit,
not inferred from free text), the runner appends it to the right notes file and updates index.md.
Read path: before a task starts, the runner reads that agent's `index.md` plus any notes file the
task explicitly references (by topic name in the workflow YAML), and puts that text in the
agent's prompt. No embedding search, no vector store: one owner, a few agents, a few dozen notes
files at most, grep-sized. This is deliberately smaller than LangGraph's long-term memory store;
upgrade path if it ever stops fitting in a prompt is a local keyword grep over memory/, still no
new dependency.

## 6. Workflow schema
See DESIGN.md for the actual YAML; the shape in short: a list of tasks, each with `id`, `agent`,
`instruction` (or a path to one), `depends_on` (list of task ids, CrewAI's Task.context idea), and
`output` (a name the result is stored under, referenced by later tasks via `{{output_name}}`
interpolation). One `supervisor` block up top (goal text, max_subtasks, max_cost_usd) that, when
present, means the task list is generated at run time instead of fixed in the file. The one
template that covers most real cases: linear-with-fan-in, i.e. a small number of independent
tasks that each depend on one shared setup task and feed one shared final task (research x2 ->
synthesize; or: plan -> [build, test] -> report).

## 7. Hermes plugin interface
One tool, `orca_run(workflow: str, params: dict) -> RunReport`. Contract:
- Synchronous call returns once the run reaches a terminal state (done/failed/cost_capped).
- For a long run, progress is not polled by Hermes mid-call (no second tool, no callback): Hermes
  gets the run_id immediately in the first line of stdout, and can separately read
  `runs/<run_id>/log.jsonl` (tail) if the owner asks "how's it going" while the main call is still
  blocked. This matches how Hermes already treats a long shell command (run in background,
  inspect its output file), instead of inventing a second notification channel in Orca itself.
- Return value (the "report"): run_id, status, total_cost_usd, per-task one-line summaries, and
  the final task's output. Hermes relays that to the user; Orca does not format chat prose.

## 8. First milestone
Smallest end-to-end run that proves every piece: a two-task workflow, no supervisor fan-out yet.
Task 1 (agent: researcher) answers a fixed small question. Task 2 (agent: writer) depends_on
task 1's output and writes one paragraph using it. Proves: Agent config loading, Task dependency
resolution and output interpolation, Run durability (kill the process after task 1, resume, confirm
task 2 still runs and the state file shows task 1 was not re-run), Run record (model used, cost,
log present and correct), and the Hermes plugin call returning a report. Deliberately excludes the
Supervisor decomposition step (can be a second milestone) to keep this one finishable.
