#!/usr/bin/env python3
"""Self-check: proves dependency order, durability on a hard kill, resume-without-rerun,
cost/model accounting, the bounded supervisor, and the provider layer (selection, tool_model,
dangling-model refusal, fallback chain). Milestone-4 provider tests use a local stdlib
http.server fake OpenAI endpoint: genuinely offline, no mocks of our own code.

Run: python3 tests/selfcheck.py    Exit 0 = all checks pass. Prints real output, no mocks.
"""
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

ORCA = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ORCA / "engine" / "orca.py"
TEST_PY = "/usr/bin/python3"   # system python has PyYAML; the Hermes tool python does not
ENV = {**os.environ, "ORCA_HOME": str(ORCA)}

sys.path.insert(0, str(ORCA / "engine"))
import orca as engine  # noqa: E402  (pure-function checks call the module directly; the
                        # `orca` name below is the subprocess-CLI helper, kept for the
                        # existing tests)


def orca(*args, env=None):
    return subprocess.run([TEST_PY, str(ENGINE), *args], capture_output=True,
                          text=True, env=env or ENV)


def load_run(run_id):
    return json.loads((ORCA / "runs" / run_id / "state.json").read_text())


def log_events(run_id):
    path = ORCA / "runs" / run_id / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def count_task_started(run_id, task_id):
    return sum(1 for e in log_events(run_id)
               if e.get("event") == "task_started" and e.get("task") == task_id)


def test_full_run():
    r = orca("run", "workflows/hello.yaml", "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    state = load_run(report["run_id"])
    assert state["status"] == "done", state["status"]
    assert [t["task_id"] for t in state["tasks"].values()] == ["research", "write"], "order wrong"
    assert "echo:ds/deepseek-v4-pro" in state["tasks"]["final_copy"]["output"], "interpolation failed"
    assert "research_notes" not in state["tasks"]["final_copy"]["output"], "placeholder left in"
    assert state["model_calls"][0]["served_model"] == "qwen3:4b"
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
run.log({{"event": "task_started", "task": first["id"]}})
agent = orca.load_agent(first["agent"])
res = orca.call_echo(agent, first["instruction"], agent["model"], orca.load_prices())
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
    # Fix for reviewer finding 7: the old assertion compared echo's output text before and
    # after, which is deterministic and so can never fail whether or not task a re-ran. This
    # counts actual task_started events in log.jsonl for task "a", which fails if it re-ran.
    starts_a = count_task_started(run_id, "a")
    assert starts_a == 1, f"task a started {starts_a} times, expected exactly 1 (no re-run)"
    assert "out_b" in after["tasks"], "task b did not run on resume"
    assert before["tasks"]["out_a"]["finished_at"] == after["tasks"]["out_a"]["finished_at"]
    print(f"  ok resume: {run_id} killed after task a, resumed, task a started exactly "
          f"{starts_a} time (log-verified), b completed")
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
    """Offline: price table lookup is exact, and a full run carries real tokens/cost for a
    priced model (the local ollama model is legitimately free, so only the router-served
    call is asserted nonzero)."""
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
    router_call = next(c for c in calls if c["requested_model"] == "ds/deepseek-v4-pro")
    assert router_call["cost_usd"] > 0, "ds/deepseek-v4-pro is priced, cost_usd must be nonzero"
    # Fix for reviewer finding 4: model calls must also land in log.jsonl, not just state.json,
    # so the append-only log alone can audit model and cost.
    logged = [e for e in log_events(report["run_id"]) if e.get("event") == "model_call"]
    assert len(logged) == len(calls), \
        f"log.jsonl has {len(logged)} model_call events, state.json has {len(calls)}"
    print(f"  ok cost accounting: compute_cost matches prices.json, "
          f"{len(calls)} model_calls carry real tokens, router call priced nonzero, "
          f"all {len(logged)} also present in log.jsonl")


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


def test_outputs_and_report_use_dependency_order():
    """Regression for findings 1 and 2: outputs()/wf_outputs() must not raise on a dict key,
    and report() must name the dependency-last task, not the YAML-last one. Task b is listed
    first in the YAML but depends on a, so dependency order is [a, b] while YAML order is
    [b, a]."""
    wf = ORCA / "workflows" / "_order.yaml"
    wf.write_text(
        "name: _order\nmax_cost_usd: 1.0\ntasks:\n"
        "  - id: b\n    agent: writer\n    depends_on: [a]\n    instruction: 'use {{out_a}}'\n"
        "    output: out_b\n"
        "  - id: a\n    agent: researcher\n    instruction: 'first'\n    output: out_a\n")
    r = orca("run", str(wf), "--provider", "echo")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("\n", 1)[1])
    state = load_run(report["run_id"])
    assert report["final_output"] == state["tasks"]["out_b"]["output"], \
        "report named the YAML-last task (a), not the dependency-last task (b)"
    run = engine.Run(resume=report["run_id"], provider="echo")
    got = run.outputs()  # must not raise TypeError: unhashable type
    assert got == {"out_a": state["tasks"]["out_a"]["output"],
                   "out_b": state["tasks"]["out_b"]["output"]}, got
    print("  ok outputs()/report(): dependency-last task picked, outputs() keyed by "
          "name, no TypeError")
    wf.unlink()


def test_resume_nonstandard_path():
    """Regression for finding 3: resume must reload the workflow from wherever it actually
    was, not a hardcoded workflows/<name>.yaml guess. Uses a .json file outside workflows/."""
    tmp_dir = pathlib.Path(tempfile.mkdtemp())
    wf = tmp_dir / "outside.json"
    wf.write_text(json.dumps({
        "name": "_outside", "max_cost_usd": 1.0,
        "tasks": [
            {"id": "a", "agent": "researcher", "instruction": "first", "output": "out_a"},
            {"id": "b", "agent": "writer", "depends_on": ["a"],
             "instruction": "use {{out_a}}", "output": "out_b"},
        ]}))
    code = f"""
import sys, os
sys.path.insert(0, {str(ORCA / 'engine')!r})
import orca
run = orca.Run({str(wf)!r}, provider="echo")
first = run.order()[0]
agent = orca.load_agent(first["agent"])
res = orca.call_echo(agent, first["instruction"], agent["model"], orca.load_prices())
run.state["tasks"][first.get("output", first["id"])] = {{
    "task_id": first["id"], "agent": first["agent"], "status": "done",
    "output": res["text"], "started_at": 0, "finished_at": 0}}
run.save()
os._exit(9)
"""
    p = subprocess.run([TEST_PY, "-c", code], capture_output=True, text=True, env=ENV)
    assert p.returncode == 9, f"expected hard kill, got {p.returncode}: {p.stderr}"
    runs = sorted((ORCA / "runs").iterdir(), key=lambda d: d.stat().st_mtime)
    run_id = runs[-1].name
    r = orca("resume", run_id, "--provider", "echo")
    assert r.returncode == 0, r.stderr
    after = load_run(run_id)
    assert after["status"] == "done", after["status"]
    assert "out_b" in after["tasks"], "resume could not reload a .json workflow outside workflows/"
    print(f"  ok resume nonstandard path: {run_id}, workflow was {wf} (.json, outside "
          f"workflows/), resumed fine")
    wf.unlink()
    tmp_dir.rmdir()


# ---------------------------------------------------------------------------
# Milestone 4: provider layer. A local stdlib http.server stands in for a real
# OpenAI-compatible endpoint, so selection, dangling-model refusal, and the fallback
# chain are provable fully offline, not just asserted.
# ---------------------------------------------------------------------------

class _FakeOpenAIHandler(http.server.BaseHTTPRequestHandler):
    model_ids = ["fake-text"]

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"data": [{"id": m} for m in self.model_ids]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model")
        self._json(200, {"model": model,
                          "choices": [{"message": {"content": f"[fake:{model}] ok"}}],
                          "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    def log_message(self, fmt, *args):
        pass  # keep test output quiet


def start_fake_server(model_ids):
    handler = type("Handler", (_FakeOpenAIHandler,), {"model_ids": model_ids})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _fake_provider(port):
    return {"kind": "openai", "base_url": f"http://127.0.0.1:{port}/v1",
            "auth": {"mode": "none"},
            "capabilities": {"tools": True, "usage": True, "structured_errors": True}}


def test_provider_selection_and_tool_model():
    """Offline: a task uses its agent's provider/model normally, and only switches to
    tool_provider/tool_model when the task declares uses_tools: true. The cap enforcing
    this is in agent_model_chain(), exercised here through a real run against a fake
    server, not by calling the helper directly."""
    server, port = start_fake_server(["fake-text", "fake-tools"])
    providers_file = ORCA / "workflows" / "_fake_providers_select.json"
    agent_file = ORCA / "agents" / "_faketool.md"
    wf = ORCA / "workflows" / "_tool_select.yaml"
    try:
        providers_file.write_text(json.dumps({"providers": {"fakeserver": _fake_provider(port)}}))
        agent_file.write_text(
            "---\nprovider: fakeserver\nmodel: fake-text\n"
            "tool_provider: fakeserver\ntool_model: fake-tools\n---\n"
            "You are a fake test agent.\n")
        wf.write_text(
            "name: _tool_select\nmax_cost_usd: 1.0\ntasks:\n"
            "  - id: plain\n    agent: _faketool\n    instruction: 'a'\n    output: out_plain\n"
            "  - id: tooled\n    agent: _faketool\n    depends_on: [plain]\n    uses_tools: true\n"
            "    instruction: 'b'\n    output: out_tooled\n")
        env = {**ENV, "ORCA_PROVIDERS": str(providers_file)}
        r = orca("run", str(wf), env=env)
        assert r.returncode == 0, r.stderr
        report = json.loads(r.stdout.split("\n", 1)[1])
        state = load_run(report["run_id"])
        calls = {c["task_id"]: c for c in state["model_calls"]}
        assert calls["plain"]["requested_model"] == "fake-text", calls["plain"]
        assert calls["tooled"]["requested_model"] == "fake-tools", calls["tooled"]
        print("  ok provider selection: plain task used fake-text, uses_tools task used "
              "fake-tools (tool_model), both via a local fake server")
    finally:
        server.shutdown()
        for f in (providers_file, agent_file, wf):
            f.unlink(missing_ok=True)


def test_dangling_model_refusal():
    """Offline: a model id absent from the provider's live /models list refuses the whole
    run at startup with a clear message, never a 404 mid-run (the exact milestone-3 bug)."""
    server, port = start_fake_server(["fake-text"])
    providers_file = ORCA / "workflows" / "_fake_providers_dangle.json"
    agent_file = ORCA / "agents" / "_fakedangling.md"
    wf = ORCA / "workflows" / "_dangling.yaml"
    try:
        providers_file.write_text(json.dumps({"providers": {"fakeserver": _fake_provider(port)}}))
        agent_file.write_text("---\nprovider: fakeserver\nmodel: does-not-exist\n---\nTest.\n")
        wf.write_text("name: _dangling\ntasks:\n"
                      "  - id: a\n    agent: _fakedangling\n    instruction: x\n    output: out_a\n")
        env = {**ENV, "ORCA_PROVIDERS": str(providers_file)}
        r = orca("run", str(wf), env=env)
        assert r.returncode != 0, "a dangling model must refuse, not run"
        assert "does not serve model" in r.stderr, r.stderr
        print("  ok dangling-model refusal: 'does-not-exist' rejected at startup, before "
              "any task ran, with a named reason")
    finally:
        server.shutdown()
        for f in (providers_file, agent_file, wf):
            f.unlink(missing_ok=True)


def test_fallback_chain_and_requested_vs_served():
    """Offline: the primary provider points at a closed port (connection refused); the
    ordered fallback entry serves it instead. Both attempts land in the run record with
    their own requested/served/fallback/reason, and the primary's failure is never hidden."""
    server, port = start_fake_server(["fallback-model"])
    providers_file = ORCA / "workflows" / "_fake_providers_fallback.json"
    agent_file = ORCA / "agents" / "_fakefallback.md"
    wf = ORCA / "workflows" / "_fallback.yaml"
    try:
        bad = {"kind": "openai", "base_url": "http://127.0.0.1:1/v1",  # nothing listens here
               "auth": {"mode": "none"},
               "capabilities": {"tools": True, "usage": True, "structured_errors": True}}
        providers_file.write_text(json.dumps(
            {"providers": {"badserver": bad, "goodserver": _fake_provider(port)}}))
        agent_file.write_text(
            "---\nprovider: badserver\nmodel: bad-model\n"
            "fallback: [\"goodserver:fallback-model\"]\n---\nTest.\n")
        wf.write_text("name: _fallback\ntasks:\n"
                      "  - id: a\n    agent: _fakefallback\n    instruction: x\n    output: out_a\n")
        env = {**ENV, "ORCA_PROVIDERS": str(providers_file)}
        r = orca("run", str(wf), env=env)
        assert r.returncode == 0, r.stderr
        report = json.loads(r.stdout.split("\n", 1)[1])
        state = load_run(report["run_id"])
        attempts = [c for c in state["model_calls"] if c["task_id"] == "a"]
        assert len(attempts) == 2, attempts
        assert attempts[0]["ok"] is False, attempts[0]
        assert attempts[0]["requested_provider"] == "badserver"
        assert attempts[0]["served_provider"] is None
        assert attempts[0]["reason"], "a failed attempt must still carry a reason"
        assert attempts[1]["ok"] is True, attempts[1]
        assert attempts[1]["requested_provider"] == "goodserver"
        assert attempts[1]["served_provider"] == "goodserver"
        assert attempts[1]["served_model"] == "fallback-model"
        assert attempts[1]["fallback"] is True
        print(f"  ok fallback chain: attempt 0 (badserver) failed and was recorded, "
              f"attempt 1 (goodserver) served 'fallback-model', fallback=True, both in "
              f"the run record")
    finally:
        server.shutdown()
        for f in (providers_file, agent_file, wf):
            f.unlink(missing_ok=True)


def test_live_fallback_reporting():
    """Live-only: proves requested/served provider+model, fallback, reason and cost_usd
    come from a real OpenAI-compatible response (the 'router' provider). Needs the real
    9router endpoint and key reachable; if not, this is reported plainly as skipped and
    never counted as a pass."""
    import urllib.error
    agent = engine.load_agent("writer")  # provider: router, model: ds/deepseek-v4-pro
    providers = engine.load_providers()
    prices = engine.load_prices()
    try:
        text, attempts = engine.dispatch(agent, {"id": "live"}, "ping", providers, prices, None)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        print(f"  live-only: provider unreachable ({e}); skipped, not counted as a pass")
        return "skipped"
    a = attempts[0]
    for key in ("served_provider", "served_model", "fallback", "reason", "cost_usd"):
        assert key in a, a
    print(f"  ok live fallback: requested={a['requested_provider']}:{a['requested_model']} "
          f"served={a['served_provider']}:{a['served_model']} fallback={a['fallback']} "
          f"cost_usd={a['cost_usd']}")
    return "ran"


if __name__ == "__main__":
    print("orca self-check")
    test_full_run()
    test_resume_skips_finished()
    test_dependency_cycle_detected()
    test_cost_and_model_accounting()
    test_supervisor_bounded_plan()
    test_outputs_and_report_use_dependency_order()
    test_resume_nonstandard_path()
    test_provider_selection_and_tool_model()
    test_dangling_model_refusal()
    test_fallback_chain_and_requested_vs_served()
    print("10/10 ok")
    live = test_live_fallback_reporting()
    print("+1 live-only ran" if live == "ran" else "+1 live-only skipped (offline)")
