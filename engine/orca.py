#!/usr/bin/env python3
"""Orca: thin agent-orchestration engine. Four primitives, no agent framework.

  Agent     agents/<name>.md   YAML frontmatter (model, tools) plus a prompt body
  Task      one entry in a workflow file: agent, instruction, depends_on, output, remember
  Workflow  workflows/<name>.yaml   name, max_cost_usd, tasks[]
  Run       runs/<run_id>/{state.json, log.jsonl}   durable, resumable

Usage:
  python3 engine/orca.py run workflows/hello.yaml [--provider echo|9router]
  python3 engine/orca.py resume <run_id>
  python3 engine/orca.py status <run_id>
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.request
import uuid

ROOT = pathlib.Path(os.environ.get("ORCA_HOME") or pathlib.Path(__file__).resolve().parent.parent)
RUNS = ROOT / "runs"
PRICES_PATH = ROOT / "engine" / "prices.json"


def load_dotenv(path: pathlib.Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ, without overwriting a variable the
    real environment already set. Silent no-op if the file is absent: .env is optional.
    ponytail: flat KEY=VALUE parser, no quoting or multiline support. Upgrade path:
    python-dotenv if a value ever needs more than that."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")


def load_prices() -> dict:
    """Per-model USD price per million tokens. Edit engine/prices.json to add or correct a
    price, no code change needed.
    ponytail: a static table, no live pricing API call. Upgrade path: fetch the provider's
    own price endpoint if one is ever exposed, keep this file as the offline fallback."""
    try:
        return json.loads(PRICES_PATH.read_text())
    except FileNotFoundError:
        return {}


def price_for(prices: dict, model: str) -> dict:
    return prices.get(model) or prices.get("default", {"input_per_mtok": 0.0, "output_per_mtok": 0.0})


def compute_cost(prices: dict, model: str, usage: dict) -> float:
    p = price_for(prices, model)
    input_tokens = usage.get("prompt_tokens") or 0
    output_tokens = usage.get("completion_tokens") or 0
    cost = (input_tokens / 1_000_000) * p["input_per_mtok"] + (output_tokens / 1_000_000) * p["output_per_mtok"]
    return round(cost, 6)


def classify_fallback(requested: str, served: str):
    """The owner's explicit requirement: a provider swap must show up as a field on the
    record, never as a silent substitution."""
    if served == requested:
        return False, None
    return True, f"provider served {served!r} instead of requested {requested!r}"


def load_structured(path: pathlib.Path):
    """Read YAML when PyYAML is present, JSON otherwise. Both are already-installed options."""
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        return json.loads(text)


def load_agent(name: str) -> dict:
    path = ROOT / "agents" / f"{name}.md"
    raw = path.read_text()
    meta, body = {}, raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = load_structured_text(front)
    meta.setdefault("model", "sonnet")
    meta["name"] = name
    meta["prompt"] = body.strip()
    meta["memory_dir"] = ROOT / "memory" / name
    return meta


def load_structured_text(text: str):
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return json.loads(text)


def memory_context(agent: dict) -> str:
    """Read path for per-agent memory: the index plus any notes the task names."""
    index = agent["memory_dir"] / "index.md"
    if not index.exists():
        return ""
    return "KNOWN NOTES:\n" + index.read_text().strip()


def remember(agent: dict, topic: str, text: str) -> pathlib.Path:
    """Write path for per-agent memory. Explicit only: never inferred from free text."""
    notes = agent["memory_dir"] / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{topic}.md"
    with path.open("a") as fh:
        fh.write(text.rstrip() + "\n")
    index = agent["memory_dir"] / "index.md"
    line = f"- {topic}: {notes[topic] if False else text.strip().splitlines()[0][:90]}\n"
    existing = index.read_text() if index.exists() else ""
    if f"- {topic}:" not in existing:
        with index.open("a") as fh:
            fh.write(line)
    return path


ECHO_SUPERVISOR_PLAN = json.dumps([
    {"id": "research", "agent": "researcher", "depends_on": [],
     "instruction": "In one sentence, what problem does an agent orchestration engine solve?",
     "output": "research_notes"},
    {"id": "write", "agent": "writer", "depends_on": ["research"],
     "instruction": "Turn this into one plain paragraph for a non-technical reader: {{research_notes}}",
     "output": "final_copy"},
])  # ponytail: fixed fixture so the supervisor path has a deterministic offline test. Only
    # used when provider == "echo" and the agent is the supervisor; a live provider call
    # never sees this string.


def call_model(agent: dict, prompt: str, provider: str, prices: dict) -> dict:
    model = agent["model"]
    if provider == "echo":
        text = ECHO_SUPERVISOR_PLAN if agent["name"] == "supervisor" else (
            f"[echo:{model}] {prompt.splitlines()[0][:120]}")
        usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": len(text.split())}
        served = model
        fallback, reason = classify_fallback(model, served)
        return {"text": text, "served_model": served, "fallback": fallback, "reason": reason,
                "cost_usd": compute_cost(prices, served, usage),
                "input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
    base = os.environ.get("ORCA_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    key = os.environ.get("ORCA_API_KEY", "")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": agent["prompt"]},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.load(resp)
    served = data.get("model") or model
    usage = data.get("usage") or {}
    fallback, reason = classify_fallback(model, served)
    return {"text": data["choices"][0]["message"]["content"], "served_model": served,
            "fallback": fallback, "reason": reason,
            "cost_usd": compute_cost(prices, served, usage),
            "input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}


def parse_plan(text: str) -> list:
    """Supervisor output must be a JSON list of task dicts. Strip markdown fences if a model
    wrapped the JSON in them anyway; raise loudly on anything else, never guess a plan."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if not isinstance(data, list):
        raise SystemExit("supervisor plan was not a JSON list")
    return data


class Run:
    def __init__(self, workflow_path=None, provider="9router", resume=None):
        self.provider = provider
        self.prices = load_prices()
        if resume:
            self.id = resume
            self.dir = RUNS / resume
            self.state = json.loads((self.dir / "state.json").read_text())
            # Fix for reviewer finding 3: reload from the exact path the run was started
            # with, not a hardcoded workflows/<name>.yaml. Fall back to the old guess only
            # for run directories saved before this field existed.
            wf_path = self.state.get("workflow_path")
            if wf_path:
                self.wf = load_structured(pathlib.Path(wf_path))
            else:
                self.wf = load_structured(ROOT / "workflows" / f"{self.state['workflow']}.yaml")
            if not self.wf.get("tasks"):
                self.wf["tasks"] = self.state.get("generated_tasks") or []
            return
        path = pathlib.Path(workflow_path)
        self.wf = load_structured(path)
        self.id = uuid.uuid4().hex[:12]
        self.dir = RUNS / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = {"run_id": self.id, "workflow": self.wf.get("name", path.stem),
                      "workflow_path": str(path.resolve()),
                      "status": "running", "started_at": time.time(), "tasks": {},
                      "model_calls": [], "total_cost_usd": 0.0, "generated_tasks": None}
        if not self.wf.get("tasks"):
            if not self.wf.get("supervisor"):
                raise SystemExit("workflow needs a tasks list or a supervisor block")
            plan = self.plan_supervisor()
            self.state["generated_tasks"] = plan
            self.wf["tasks"] = plan
        self.save()

    def plan_supervisor(self) -> list:
        """One bounded planning call: a goal becomes at most max_subtasks tasks. The cap is
        enforced here in code after the call returns, not trusted to the prompt. Runs exactly
        once, from __init__: there is no path that calls this twice for the same run, so there
        is no re-planning mid-run."""
        sup = self.wf["supervisor"]
        cap = self.wf.get("max_subtasks", 5)
        agent_name = sup.get("agent", "supervisor")
        agent = load_agent(agent_name)
        agent_names = sorted(p.stem for p in (ROOT / "agents").glob("*.md") if p.stem != agent_name)
        prompt = (
            f"Goal: {sup['goal']}\n\n"
            f"Produce a JSON list of at most {cap} tasks to achieve this goal. Each task is an "
            f"object with keys: id (short slug), agent (one of: {', '.join(agent_names)}), "
            f"instruction (string), depends_on (list of earlier task ids, [] if none), output "
            f"(short result name). Respond with ONLY the JSON list, no prose, no code fences."
        )
        result = call_model(agent, prompt, self.provider, self.prices)
        self.record_model_call("__plan__", agent["model"], result)
        self.log({"event": "supervisor_planned", "goal": sup["goal"], "raw": result["text"]})
        plan = parse_plan(result["text"])
        if len(plan) > cap:
            plan = plan[:cap]
        if not plan:
            raise SystemExit("supervisor produced zero tasks")
        for task in plan:
            missing = [k for k in ("id", "agent", "instruction", "output") if k not in task]
            if missing:
                raise SystemExit(f"supervisor task missing fields {missing}: {task}")
            if not (ROOT / "agents" / f"{task['agent']}.md").exists():
                raise SystemExit(f"supervisor assigned unknown agent {task['agent']!r}")
            task.setdefault("depends_on", [])
        return plan

    def save(self):
        """Durability: atomic replace after every task, so a crash never leaves a half state."""
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.dir / "state.json")

    def log(self, event: dict):
        event["timestamp"] = time.time()
        with (self.dir / "log.jsonl").open("a") as fh:
            fh.write(json.dumps(event) + "\n")

    def record_model_call(self, task_id: str, requested_model: str, result: dict):
        """Fix for reviewer finding 4: every model call lands in log.jsonl as well as
        state.json, so the append-only log alone can audit which model served a request and
        what it cost, without needing the snapshot file."""
        entry = {"task_id": task_id, "requested_model": requested_model,
                  "served_model": result["served_model"], "fallback": result["fallback"],
                  "reason": result["reason"], "cost_usd": result["cost_usd"],
                  "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
                  "timestamp": time.time()}
        self.state["model_calls"].append(entry)
        self.log({"event": "model_call", **entry})
        self.state["total_cost_usd"] = round(self.state["total_cost_usd"] + result["cost_usd"], 6)

    def outputs(self) -> dict:
        return {name: v["output"] for name, v in self.wf_outputs().items()}

    def wf_outputs(self):
        """Fix for reviewer finding 1: key by output name (a string), not the task dict
        itself, which is unhashable and raised TypeError on every call."""
        out = {}
        for task in self.wf["tasks"]:
            name = task.get("output", task["id"])
            if name in self.state["tasks"]:
                out[name] = self.state["tasks"][name]
        return out

    def order(self) -> list:
        tasks = self.wf["tasks"]
        names = [t["id"] for t in tasks]
        done, order = [], []
        while len(order) < len(tasks):
            progressed = False
            for t in tasks:
                if t["id"] in order:
                    continue
                if all(d in order for d in t.get("depends_on", [])):
                    order.append(t["id"])
                    progressed = True
            if not progressed:
                stuck = [n for n in names if n not in order]
                raise SystemExit(f"dependency cycle or unknown depends_on: {stuck}")
        by_id = {t["id"]: t for t in tasks}
        return [by_id[i] for i in order]

    def render(self, text: str) -> str:
        for task in self.wf["tasks"]:
            out = task.get("output", task["id"])
            if out in self.state["tasks"]:
                text = text.replace("{{" + out + "}}", self.state["tasks"][out]["output"])
        return text

    def execute(self) -> dict:
        cap = self.wf.get("max_cost_usd")
        for task in self.order():
            name = task.get("output", task["id"])
            if name in self.state["tasks"] and self.state["tasks"][name]["status"] == "done":
                continue
            if cap is not None and self.state["total_cost_usd"] >= cap:
                self.state["status"] = "cost_capped"
                self.save()
                return self.report()
            agent = load_agent(task["agent"])
            prompt = self.render(task["instruction"])
            prompt = (memory_context(agent) + "\n\n" + prompt).strip()
            self.log({"event": "task_started", "task": task["id"]})
            started = time.time()
            result = call_model(agent, prompt, self.provider, self.prices)
            self.state["tasks"][name] = {"task_id": task["id"], "agent": task["agent"],
                                         "status": "done", "output": result["text"],
                                         "started_at": started, "finished_at": time.time()}
            self.record_model_call(task["id"], agent["model"], result)
            # Fix for reviewer findings 5 and 6: log the task_finished event, then make the
            # state durable (save), and only then perform remember()'s disk write. A crash
            # right after save() means the task is already marked done, so a resume will
            # skip it and never repeat remember()'s write; the old order wrote remember()
            # first, so a crash between remember() and save() duplicated the memory note on
            # resume.
            # ponytail: this trades duplication for loss. A crash between save() and
            # remember() now means the memory note is silently never written, since resume
            # sees the task as done and skips it. Upgrade path if that matters more than
            # duplication: persist "remembered: false" in the task's state entry and flip it
            # to true only after remember() returns, so resume can tell the two cases apart.
            self.log({"event": "task_finished", "task": task["id"]})
            self.save()
            if task.get("remember"):
                remember(agent, task["remember"], result["text"])
        self.state["status"] = "done"
        self.save()
        return self.report()

    def report(self) -> dict:
        # Fix for reviewer finding 2: the last task to actually run is the last one in
        # dependency-resolved order, not the last one written in the YAML file.
        final = self.order()[-1]
        last = self.state["tasks"].get(final.get("output", final["id"]), {})
        return {"run_id": self.id, "status": self.state["status"],
                "total_cost_usd": self.state["total_cost_usd"],
                "tasks": [{"id": v["task_id"], "agent": v["agent"],
                           "summary": v["output"].strip().splitlines()[0][:140]}
                          for v in self.state["tasks"].values()],
                "model_calls": self.state["model_calls"],
                "final_output": last.get("output", ""),
                "log": str(self.dir / "log.jsonl")}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="orca")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("workflow")
    p_run.add_argument("--provider", default="9router")
    p_res = sub.add_parser("resume")
    p_res.add_argument("run_id")
    p_res.add_argument("--provider", default="9router")
    p_st = sub.add_parser("status")
    p_st.add_argument("run_id")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        run = Run(args.workflow, provider=args.provider)
        print(json.dumps({"run_id": run.id, "log": str(run.dir / "log.jsonl")}))
        report = run.execute()
    elif args.cmd == "resume":
        report = Run(resume=args.run_id, provider=args.provider).execute()
    else:
        state = json.loads((RUNS / args.run_id / "state.json").read_text())
        print(json.dumps({"run_id": args.run_id, "status": state["status"],
                          "tasks": {k: v["status"] for k, v in state["tasks"].items()}}, indent=2))
        return 0
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
