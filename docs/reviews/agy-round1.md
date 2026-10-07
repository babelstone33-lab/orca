# Reviewer round 1: Antigravity (Gemini 3.1 Pro), independent audit

Mode `plan` (read-only), effort `high`, working dir the repo root. 43,400 tokens, no Claude quota
spent. Findings below are reproduced as received. Each was then re-checked by Hermes against the
code at the line numbers given, and every one of them holds.

Source: `~/.hermes/cache/scratch/orca/agy-review.json`

## Findings, all confirmed present

| # | Location | Defect |
|---|---|---|
| 1 | `engine/orca.py:265` | `wf_outputs()` uses a task dict as a dict key, so `outputs()` raises `TypeError: unhashable type: 'dict'` |
| 2 | `engine/orca.py:329` | The report returns the physically last task in the YAML file, not the last task in dependency-resolved execution order |
| 3 | `engine/orca.py:119` | Resume hardcodes `workflows/<name>.yaml`, so a run started from another path or extension cannot resume |
| 4 | `engine/orca.py` model_calls | `state.json` records model calls but `log.jsonl` never does, so the append-only log cannot audit models or cost |
| 5 | `engine/orca.py:321` | `remember()` writes agent memory before `save()` marks the task done, so a crash duplicates the memory note on resume |
| 6 | `engine/orca.py` log ordering | The `task_finished` event is written after `save()`, so a crash in that window loses it and the log no longer reconstructs state |
| 7 | `tests/selfcheck.py:82` | The assertion written to prove a task did not re-run cannot fail, because the echo provider is deterministic |

Item 7 is the one that matters most: a test that cannot fail is worse than no test, because it
manufactures confidence. Found by a reviewer on a different model family and a different quota
pool, which is exactly the role it was given.

## Kept, per the reviewer

- `os.replace` for `state.json`: correct atomic write.
- Topological dependency resolution without a graph library: correct and well scoped.

## Found later by Hermes, not by the reviewer

`engine/orca.py` never loads `.env`, so `orca run <workflow>` cannot reach 9router even though the
key is present and valid (verified: HTTP 200 with the key, 401 without). The live path is one
env-load away from working.
