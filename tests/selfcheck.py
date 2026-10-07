#!/usr/bin/env python3
"""Self-check: proves dependency order, durability on a hard kill, and resume-without-rerun.

Run: python3 tests/selfcheck.py    Exit 0 = all checks pass. Prints real output, no mocks.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ORCA = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ORCA / "engine" / "orca.py"
TEST_PY = "/usr/bin/python3"   # system python has PyYAML; the Hermes tool python does not
ENV = {**os.environ, "ORCA_HOME": str(ORCA)}

sys.path.insert(0, str(ORCA / "engine"))
import orca as engine  # noqa: E402  (pure-function checks call the module directly; the
                        # `orca` name below is the subprocess-CLI helper, kept for the
                        # existing tests)


def orca(*args):
    return subprocess.run([TEST_PY, str(ENGINE), *args], capture_output=True,
                          text=True, env=ENV)


def load_run(run_id):
    return json.loads((ORCA / "runs" / run_id / "state.json").read_text())


def test_full_run():
    r = orca("run", "workflows/hello.yaml", "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    state = load_run(report["run_id"])
    assert state["status"] == "done", state["status"]
    assert [t["task_id"] for t in state["tasks"].values()] == ["research", "write"], "order wrong"
    assert "echo:sonnet" in state["tasks"]["final_copy"]["output"], "interpolation failed"
    assert "research_notes" not in state["tasks"]["final_copy"]["output"], "placeholder left in"
    assert state["model_calls"][0]["served_model"] == "sonnet"
    assert state["model_calls"][0]["fallback"] is False
    assert (ORCA / "runs" / report["run_id"] / "log.jsonl").exists()
    print(f"  ok full run: {report['run_id']} 2 tasks, order + {{}} interpolation verified")
    return report["run_id"]


def test_resume_skips_finished():
    """Kill the runner after task 1 lands, then resume: task 1 must not re-run."""
    wf = ORCA / "workflows" / "_crash.yaml"
    wf.write_text(
        "name: _crash\nmax_cost_usd: 1.0\ntasks:\n"
        "  - id: a\n    agent: researcher\n    instruction: 'first'\n    output: out_a\n"
        "  - id: b\n    agent: writer\n    depends_on: [a]\n    instruction: 'use {{out_a}}'\n"
        "    output: out_b\n")
    code = f"""
import sys, os, json, pathlib
sys.path.insert(0, {str(ORCA / 'engine')!r})
import orca
run = orca.Run({str(wf)!r}, provider="echo")
tasks = run.order()
first = tasks[0]
agent = orca.load_agent(first["agent"])
res = orca.call_model(agent, first["instruction"], "echo", orca.load_prices())
run.state["tasks"][first.get("output", first["id"])] = {{
    "task_id": first["id"], "agent": first["agent"], "status": "done",
    "output": res["text"], "started_at": 0, "finished_at": 0}}
run.save()
os._exit(9)   # hard kill, no cleanup, exactly like a crashed process
"""
    p = subprocess.run([TEST_PY, "-c", code], capture_output=True, text=True, env=ENV)
    assert p.returncode == 9, f"expected hard kill, got {p.returncode}: {p.stderr}"
    runs = sorted((ORCA / "runs").iterdir(), key=lambda d: d.stat().st_mtime)
    run_id = runs[-1].name
    before = load_run(run_id)
    assert list(before["tasks"]) == ["out_a"], before["tasks"]
    r = orca("resume", run_id, "--provider", "echo")
    assert r.returncode == 0, r.stderr
    after = load_run(run_id)
    assert after["status"] == "done", after["status"]
    assert after["tasks"]["out_a"]["output"] == before["tasks"]["out_a"]["output"], "task a re-ran"
    assert "out_b" in after["tasks"], "task b did not run on resume"
    assert before["tasks"]["out_a"]["finished_at"] == after["tasks"]["out_a"]["finished_at"]
    print(f"  ok resume: {run_id} killed after task a, resumed, a untouched, b completed")
    wf.unlink()


def test_dependency_cycle_detected():
    wf = ORCA / "workflows" / "_cycle.yaml"
    wf.write_text("name: _cycle\ntasks:\n"
                  "  - id: a\n    agent: researcher\n    depends_on: [b]\n    instruction: x\n"
                  "  - id: b\n    agent: writer\n    depends_on: [a]\n    instruction: y\n")
    r = orca("run", str(wf), "--provider", "echo")
    assert "dependency cycle" in (r.stderr + r.stdout), r.stdout
    print("  ok cycle detected, refused instead of hanging")
    wf.unlink()


def test_cost_and_model_accounting():
    """Offline: price table lookup is exact, and a full run carries real tokens/cost."""
    prices = engine.load_prices()
    assert "sonnet" in prices, "engine/prices.json missing a sonnet entry"
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    p = prices["sonnet"]
    expected = round(1000 / 1e6 * p["input_per_mtok"] + 500 / 1e6 * p["output_per_mtok"], 6)
    got = engine.compute_cost(prices, "sonnet", usage)
    assert got == expected, (got, expected)
    assert engine.classify_fallback("sonnet", "sonnet") == (False, None)
    fallback, reason = engine.classify_fallback("sonnet", "haiku")
    assert fallback is True and "haiku" in reason and "sonnet" in reason, (fallback, reason)

    r = orca("run", "workflows/hello.yaml", "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    calls = load_run(report["run_id"])["model_calls"]
    assert all("input_tokens" in c and "output_tokens" in c for c in calls), calls
    assert all(c["cost_usd"] > 0 for c in calls), "sonnet is priced, cost_usd must be nonzero"
    print(f"  ok cost accounting: compute_cost matches prices.json, "
          f"{len(calls)} model_calls carry real tokens and nonzero cost")


def test_supervisor_bounded_plan():
    """Offline: one planning call turns a goal into tasks that run through the normal
    executor, and the max_subtasks cap is enforced in code even when the plan exceeds it."""
    r = orca("run", "workflows/launch-plan.yaml", "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    state = load_run(report["run_id"])
    assert state["status"] == "done", state["status"]
    assert len(state["generated_tasks"]) == 2, state["generated_tasks"]
    assert [t["task_id"] for t in state["tasks"].values()] == ["research", "write"], state["tasks"]
    plan_calls = [c for c in state["model_calls"] if c["task_id"] == "__plan__"]
    assert len(plan_calls) == 1, "supervisor planned more than once"
    print(f"  ok supervisor plan: {report['run_id']} goal -> 2 tasks, ran end to end, "
          f"planned exactly once")

    wf = ORCA / "workflows" / "_captest.yaml"
    wf.write_text("name: _captest\nmax_cost_usd: 1.0\nmax_subtasks: 1\n"
                  "supervisor:\n  goal: test goal\n")
    r = orca("run", str(wf), "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    state = load_run(report["run_id"])
    assert len(state["generated_tasks"]) == 1, "max_subtasks cap not enforced in code"
    assert list(state["tasks"]) == ["research_notes"], state["tasks"]
    wf.unlink()
    print("  ok cap enforced: a 2-task echo plan truncated to 1 under max_subtasks=1, in code")


def test_live_fallback_reporting():
    """Live-only: proves requested/served model, fallback, reason and cost_usd come from a
    real OpenAI-compatible response. Needs ORCA_BASE_URL reachable; if it is not, this is
    reported plainly as skipped and never counted as a pass."""
    import urllib.error
    agent = engine.load_agent("researcher")
    prices = engine.load_prices()
    try:
        result = engine.call_model(agent, "ping", "9router", prices)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        print(f"  live-only: provider unreachable ({e}); skipped, not counted as a pass")
        return "skipped"
    for key in ("served_model", "fallback", "reason", "cost_usd"):
        assert key in result, result
    print(f"  ok live fallback: requested=sonnet served={result['served_model']} "
          f"fallback={result['fallback']} cost_usd={result['cost_usd']}")
    return "ran"


if __name__ == "__main__":
    print("orca self-check")
    test_full_run()
    test_resume_skips_finished()
    test_dependency_cycle_detected()
    test_cost_and_model_accounting()
    test_supervisor_bounded_plan()
    print("5/5 ok")
    live = test_live_fallback_reporting()
    print("+1 live-only ran" if live == "ran" else "+1 live-only skipped (offline)")
