# Orca

A thin, personal agent-orchestration engine. Hermes calls it; it is a tool, not another agent.

Four primitives, no agent framework, no runtime dependency on LangGraph or CrewAI:

```
Agent      agents/<name>.md      YAML frontmatter (model) plus a prompt body
Task       one entry in a workflow file
Workflow   workflows/<name>.yaml  name, max_cost_usd, tasks[] with depends_on
Run        runs/<run_id>/         state.json (atomic), log.jsonl (append-only)
```

## Quick start

```bash
/usr/bin/python3 engine/orca.py run workflows/hello.yaml --provider echo   # offline, no model
/usr/bin/python3 engine/orca.py run workflows/hello.yaml                   # real, via 9router
/usr/bin/python3 engine/orca.py status <run_id>
/usr/bin/python3 engine/orca.py resume <run_id>                            # continue after a crash
/usr/bin/python3 tests/selfcheck.py                                        # 3 checks, real output
```

`--provider echo` runs the whole workflow with no model call. That is how the engine is tested
without burning quota.

## Layout

```
engine/      the engine (stdlib only)
agents/      one markdown file per specialist
workflows/   one YAML file per workflow
memory/      per-agent memory, markdown, committed to git
docs/        DESIGN.md (the full design) and ROLES.md (who does what)
tests/       selfcheck.py
runs/        local run state, not committed
```

## Environment

- `ORCA_HOME`: repo root. Defaults to the parent of the engine directory.
- `ORCA_BASE_URL`: OpenAI-compatible endpoint. Default `http://127.0.0.1:20128/v1` (9router).
- `ORCA_API_KEY`: bearer token for that endpoint.

## Deliberately not built yet

`ponytail:` markers in the code name each ceiling and its upgrade path. The short list: no
supervisor decomposition yet, no parallel task execution, cost accounting is inert until a
provider returns pricing, and dependencies resolve into a sequential order. Each is one milestone
away, not one architecture away.

Built by Mor and Hermes, with Claude as the builder and Antigravity as the reviewer. See
`docs/ROLES.md`.
