#!/usr/bin/env python3
"""Orca: thin agent-orchestration engine. Four primitives, no agent framework.

  Agent     agents/<name>.md   YAML frontmatter (provider, model, tool_provider, tool_model,
                                fallback) plus a prompt body
  Task      one entry in a workflow file: agent, instruction, depends_on, output, remember,
                                uses_tools
  Workflow  workflows/<name>.yaml   name, max_cost_usd, tasks[]
  Run       runs/<run_id>/{state.json, log.jsonl}   durable, resumable

Providers: providers.yaml, a data table (kind: openai | cli), never a class per provider.
See docs/DESIGN.md and docs/briefs/milestone-4.md.

Usage:
  python3 engine/orca.py run workflows/hello.yaml [--provider echo]
  python3 engine/orca.py resume <run_id>
  python3 engine/orca.py status <run_id>
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import time
import urllib.request
import uuid

ROOT = pathlib.Path(os.environ.get("ORCA_HOME") or pathlib.Path(__file__).resolve().parent.parent)
RUNS = ROOT / "runs"
PRICES_PATH = ROOT / "engine" / "prices.json"
PROVIDERS_PATH = pathlib.Path(os.environ.get("ORCA_PROVIDERS") or (ROOT / "providers.yaml"))


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


def load_structured_text(text: str):
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return json.loads(text)


def load_agent(name: str) -> dict:
    path = ROOT / "agents" / f"{name}.md"
    raw = path.read_text()
    meta, body = {}, raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = load_structured_text(front)
    meta["name"] = name
    meta["prompt"] = body.strip()
    meta["memory_dir"] = ROOT / "memory" / name
    if "provider" not in meta or "model" not in meta:
        raise SystemExit(
            f"agent {name!r} must declare both provider and model in its frontmatter "
            f"(agents/{name}.md). Milestone 3's live 404 was exactly this left to a silent "
            f"default ('model: sonnet' with no provider, against a router that has no "
            f"Anthropic model at all); the engine now refuses rather than guessing.")
    return meta


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


# ---------------------------------------------------------------------------
# Provider registry: data, not classes. Two transports (openai, cli), one dispatch
# function with a fallback chain. See docs/briefs/milestone-4.md.
# ---------------------------------------------------------------------------

def load_providers() -> dict:
    """providers.yaml, one table: name -> {kind, base_url/command, auth/args,
    capabilities, model (cli only)}. No secret in this file, only the name of an env var;
    values live in .env."""
    try:
        data = load_structured(PROVIDERS_PATH)
    except FileNotFoundError:
        return {}
    return (data or {}).get("providers", {})


def agent_model_chain(agent: dict, uses_tools: bool = False) -> list:
    """The ordered (provider, model) attempts for one call: the primary (or the
    tool_provider/tool_model pair, but only when the task actually uses tools), then each
    entry of agent['fallback'] parsed as 'provider:model'."""
    if uses_tools and agent.get("tool_provider"):
        primary = (agent["tool_provider"], agent.get("tool_model") or agent["model"])
    else:
        primary = (agent["provider"], agent["model"])
    chain = [primary]
    for entry in agent.get("fallback", []):
        prov, _, model = entry.partition(":")
        chain.append((prov, model))
    return chain


def _read_json_response(resp) -> dict:
    """Some OpenAI-compatible endpoints (seen live on 9router) append trailing bytes after
    a non-streaming JSON body, e.g. a stray 'data: [DONE]' SSE terminator. raw_decode reads
    only the first complete JSON value and ignores whatever follows it, instead of failing
    the whole response on garbage the caller never asked to stream."""
    text = resp.read().decode()
    obj, _ = json.JSONDecoder().raw_decode(text.strip())
    return obj


def fetch_model_ids(provider: dict) -> list:
    base = provider["base_url"].rstrip("/")
    headers = {}
    auth = provider.get("auth", {"mode": "none"})
    if auth.get("mode") == "bearer_env":
        headers["Authorization"] = f"Bearer {os.environ.get(auth['var'], '')}"
    req = urllib.request.Request(base + "/models", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = _read_json_response(resp)
    return [m["id"] for m in data.get("data", [])]


def validate_providers(tasks: list, providers: dict, provider_override) -> None:
    """Fix for the milestone-3 404: a dangling model reference refuses the whole run at
    startup, never 404s mid-run. Checks the PRIMARY attempt of every task's agent (the
    declared, intended route): unknown provider or a missing tools capability is always a
    hard failure here. A model-list fetch that fails because the endpoint is simply
    unreachable is NOT treated as a dangling-model error: that is exactly what the fallback
    chain exists to handle at dispatch time, so it is left for dispatch to discover.
    ponytail: one /models fetch per distinct openai provider referenced, no cross-run
    caching. Upgrade path: a short-TTL cache if this is ever called often enough to matter."""
    if provider_override == "echo":
        return
    model_lists = {}
    for task in tasks:
        agent = load_agent(task["agent"])
        uses_tools = bool(task.get("uses_tools"))
        prov_name, model = agent_model_chain(agent, uses_tools)[0]
        provider = providers.get(prov_name)
        if provider is None:
            raise SystemExit(f"unknown provider {prov_name!r} referenced by agent {agent['name']!r}")
        if uses_tools and not provider.get("capabilities", {}).get("tools", False):
            raise SystemExit(
                f"task {task.get('id')!r} needs tools; provider {prov_name!r} declares "
                f"capabilities {provider.get('capabilities')}, so it is refused up front "
                f"rather than silently downgraded")
        if provider["kind"] != "openai":
            continue
        if prov_name not in model_lists:
            try:
                model_lists[prov_name] = set(fetch_model_ids(provider))
            except Exception:
                model_lists[prov_name] = None  # unreachable now; dispatch/fallback will discover it
        ids = model_lists[prov_name]
        if ids is not None and model not in ids:
            raise SystemExit(
                f"provider {prov_name!r} does not serve model {model!r}; it serves {sorted(ids)}")


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


def call_echo(agent: dict, prompt: str, model: str, prices: dict) -> dict:
    text = ECHO_SUPERVISOR_PLAN if agent["name"] == "supervisor" else (
        f"[echo:{model}] {prompt.splitlines()[0][:120]}")
    usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": len(text.split())}
    return {"text": text, "served_model": model, "cost_usd": compute_cost(prices, model, usage),
            "cost_basis": "dollars", "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"]}


def call_openai(provider: dict, model: str, agent: dict, prompt: str, prices: dict) -> dict:
    """openai transport: any OpenAI-compatible endpoint (9router, Ollama, OpenRouter, ...).
    Tokens and cost come from the response's usage block and engine/prices.json."""
    base = provider["base_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    auth = provider.get("auth", {"mode": "none"})
    if auth.get("mode") == "bearer_env":
        headers["Authorization"] = f"Bearer {os.environ.get(auth['var'], '')}"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": agent["prompt"]},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(),
                                  headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = _read_json_response(resp)
    served_model = data.get("model") or model
    usage = data.get("usage") or {}
    return {"text": data["choices"][0]["message"]["content"], "served_model": served_model,
            "cost_usd": compute_cost(prices, served_model, usage), "cost_basis": "dollars",
            "input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}


def call_cli(provider: dict, prompt: str) -> dict:
    """cli transport: shell out to a command that already holds its own authenticated
    session (claude -p, agy-ask.sh). No API key, no usage metadata, no structured HTTP
    errors: capabilities are declared false in providers.yaml and checked before dispatch
    ever reaches here. cost_usd is None, never a fabricated dollar figure for a subscription.
    ponytail: stdout captured whole, no streaming, fixed 600s timeout, one arg template per
    provider instead of per-CLI code. Upgrade path: per-provider timeout override in
    providers.yaml if one CLI ever needs it."""
    args = provider.get("args", ["-p", "{prompt}"])
    cmd = [provider["command"]] + [prompt if a == "{prompt}" else a for a in args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"cli provider {provider['command']} exited {result.returncode}: "
            f"{result.stderr.strip()[:300]}")
    return {"text": result.stdout.strip(), "served_model": provider.get("model"),
            "cost_usd": None, "cost_basis": "quota", "input_tokens": None, "output_tokens": None}


def dispatch(agent: dict, task: dict, prompt: str, providers: dict, prices: dict,
             provider_override) -> tuple:
    """Tries the primary provider/model, then each fallback entry in order, on any error
    from the call itself. Capability refusals and unknown providers/kinds are hard
    failures (SystemExit, not an Exception subclass) and are never retried via fallback:
    that is a configuration problem, not a transient one. If every entry in the chain
    fails, raises the FIRST error (LangChain's semantics: the last error in a chain usually
    just says everything failed, the first one says why). Returns (text, attempts), where
    attempts is one dict per entry tried, win or fail, for the run record."""
    uses_tools = bool(task.get("uses_tools"))
    chain = [("echo", agent["model"])] if provider_override == "echo" else agent_model_chain(agent, uses_tools)
    attempts = []
    first_error = None
    for i, (prov_name, model) in enumerate(chain):
        try:
            if prov_name == "echo":
                result = call_echo(agent, prompt, model, prices)
            else:
                provider = providers.get(prov_name)
                if provider is None:
                    raise SystemExit(f"unknown provider {prov_name!r}")
                if uses_tools and not provider.get("capabilities", {}).get("tools", False):
                    raise SystemExit(
                        f"task needs tools; provider {prov_name!r} does not support them")
                if provider["kind"] == "openai":
                    result = call_openai(provider, model, agent, prompt, prices)
                elif provider["kind"] == "cli":
                    result = call_cli(provider, prompt)
                else:
                    raise SystemExit(f"unknown provider kind {provider['kind']!r}")
        except Exception as e:
            if first_error is None:
                first_error = e
            attempts.append({"attempt": i, "requested_provider": prov_name, "requested_model": model,
                              "served_provider": None, "served_model": None, "fallback": i > 0,
                              "reason": str(e), "ok": False, "cost_usd": 0.0, "cost_basis": "dollars",
                              "input_tokens": None, "output_tokens": None})
            continue
        same_call_fallback, same_call_reason = classify_fallback(model, result["served_model"])
        fallback = (i > 0) or same_call_fallback
        reason = (f"attempt {i} after earlier failure: {first_error}" if i > 0
                  else same_call_reason)
        attempts.append({"attempt": i, "requested_provider": prov_name, "requested_model": model,
                          "served_provider": prov_name, "served_model": result["served_model"],
                          "fallback": fallback, "reason": reason, "ok": True,
                          "cost_usd": result["cost_usd"], "cost_basis": result.get("cost_basis", "dollars"),
                          "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens")})
        return result["text"], attempts
    raise first_error


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
    def __init__(self, workflow_path=None, provider=None, resume=None):
        self.provider_override = provider  # None = per-agent routing; "echo" = test stub
        self.prices = load_prices()
        self.providers = load_providers()
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
            sup = self.wf["supervisor"]
            validate_providers([{"id": "__plan__", "agent": sup.get("agent", "supervisor")}],
                                self.providers, self.provider_override)
            plan = self.plan_supervisor()
            validate_providers(plan, self.providers, self.provider_override)
            self.state["generated_tasks"] = plan
            self.wf["tasks"] = plan
        else:
            validate_providers(self.wf["tasks"], self.providers, self.provider_override)
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
        text, attempts = dispatch(agent, {"id": "__plan__"}, prompt, self.providers, self.prices,
                                   self.provider_override)
        self.record_attempts("__plan__", attempts)
        self.log({"event": "supervisor_planned", "goal": sup["goal"], "raw": text})
        plan = parse_plan(text)
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

    def record_attempts(self, task_id: str, attempts: list):
        """Every attempt, primary or fallback, win or fail, lands in both state.json and
        log.jsonl: the requested-versus-served fields plus fallback/reason, so the
        append-only log alone can audit which provider and model actually served a request,
        and why, without needing the snapshot file (fix for reviewer finding 4, extended to
        the full provider/fallback chain)."""
        for attempt in attempts:
            entry = {"task_id": task_id, **attempt}
            self.state["model_calls"].append(entry)
            self.log({"event": "model_call", **entry})
            if entry.get("cost_usd"):
                self.state["total_cost_usd"] = round(self.state["total_cost_usd"] + entry["cost_usd"], 6)

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
            text, attempts = dispatch(agent, task, prompt, self.providers, self.prices,
                                       self.provider_override)
            self.state["tasks"][name] = {"task_id": task["id"], "agent": task["agent"],
                                         "status": "done", "output": text,
                                         "started_at": started, "finished_at": time.time()}
            self.record_attempts(task["id"], attempts)
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
                remember(agent, task["remember"], text)
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
    p_run.add_argument("--provider", default=None, help="override, e.g. 'echo' for offline tests")
    p_res = sub.add_parser("resume")
    p_res.add_argument("run_id")
    p_res.add_argument("--provider", default=None)
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
