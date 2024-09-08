"""Command line entry point: ``incidentpilot <command>``."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

from .config import Settings
from .evaluation import render, run_eval
from .generator import SCENARIOS, SERVICES, generate, to_records
from .llm import make_llm
from .report import to_markdown


def _scenario_arg(p: argparse.ArgumentParser, settings: Settings) -> None:
    p.add_argument("--scenario", default=settings.scenario, choices=sorted(SCENARIOS))
    p.add_argument("--service", choices=SERVICES, default=None)
    p.add_argument("--seed", type=int, default=settings.seed)


def cmd_simulate(a, settings: Settings) -> int:
    from .agent import TriageAgent
    from .pipeline import Pipeline
    from .store import Store

    gen = generate(a.scenario, service=a.service, seed=a.seed)
    store = Store(a.db or ":memory:")
    llm = make_llm(a.llm, settings.ollama_url, settings.ollama_model)
    pipe = Pipeline(store, TriageAgent(store, llm).investigate)
    for e in gen.events:
        pipe.process_row(e["kind"], e["row"])
    pipe.finish()
    print(f"injected {gen.truth.scenario} on {gen.truth.service}; "
          f"{pipe.stats['logs']} logs, {pipe.stats['metrics']} metrics ingested")
    for inc in pipe.incidents:
        if inc.report:
            print("\n" + to_markdown(inc.report))
        else:
            print(f"\n{inc.id}: {inc.status} {inc.error or ''}")
    return 0 if pipe.incidents else 1


def cmd_generate(a, settings: Settings) -> int:
    gen = generate(a.scenario, service=a.service, seed=a.seed)
    out = open(a.out, "w") if a.out else sys.stdout
    with out:
        for rec in to_records(gen.events):
            out.write(json.dumps(rec) + "\n")
    return 0


def cmd_publish(a, settings: Settings) -> int:
    from .bus import connect_bus

    async def main() -> None:
        bus = await connect_bus(settings.bus_url, settings.bus_subject)
        gen = generate(a.scenario, service=a.service, seed=a.seed)
        print(f"publishing {a.scenario} ({len(gen.events)} events) at {a.speed}x", flush=True)
        began = time.monotonic()
        for e, rec in zip(gen.events, to_records(gen.events)):
            due = (e["row"]["ts"] - gen.t0) / a.speed
            lag = due - (time.monotonic() - began)
            if lag > 0.01:
                await asyncio.sleep(lag)
            await bus.publish(json.dumps(rec).encode())
        await bus.close()
        print("done", flush=True)

    asyncio.run(main())
    return 0


def cmd_eval(a, settings: Settings) -> int:
    llm = make_llm(a.llm, settings.ollama_url, settings.ollama_model)
    report = run_eval(llm)
    print(render(report))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2)
    s = report["summary"]
    return 0 if s["root_cause_accuracy"] >= a.min_accuracy else 1


def cmd_serve(a, settings: Settings) -> int:
    import uvicorn
    uvicorn.run("incidentpilot.api:app_factory", factory=True, host=a.host, port=a.port)
    return 0


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def cmd_show(a, settings: Settings) -> int:
    """Wait for the first triaged incident on a running API and print its report."""
    deadline = time.time() + a.wait
    while True:
        try:
            incs = json.loads(_get(f"{a.url}/incidents"))
            done = [i for i in incs if i["status"] != "open"]
            if done:
                print(_get(f"{a.url}/incidents/{done[0]['id']}/report").decode())
                return 0
        except OSError:
            pass
        if time.time() > deadline:
            print("no triaged incident yet", file=sys.stderr)
            return 1
        time.sleep(2)


def cmd_wait(a, settings: Settings) -> int:
    """Block until the API's /health answers (used by the generator init container)."""
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        try:
            _get(f"{a.url}/health")
            return 0
        except OSError:
            time.sleep(1)
    print(f"{a.url} did not become healthy", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    ap = argparse.ArgumentParser(prog="incidentpilot", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("simulate", help="generate a scenario and triage it in-process")
    _scenario_arg(p, settings)
    p.add_argument("--llm", default=settings.llm, choices=["mock", "ollama"])
    p.add_argument("--db", default=None, help="DuckDB file to keep the data for ad-hoc queries")
    p.set_defaults(fn=cmd_simulate)

    p = sub.add_parser("generate", help="write OTLP/JSON lines for a scenario")
    _scenario_arg(p, settings)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("publish", help="replay a scenario onto the event bus")
    _scenario_arg(p, settings)
    p.add_argument("--speed", type=float, default=settings.speed, help="x real time")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("eval", help="score the agent on the scripted incidents")
    p.add_argument("--llm", default="mock", choices=["mock", "ollama"])
    p.add_argument("--json", default=None, help="also write full results here")
    p.add_argument("--min-accuracy", type=float, default=0.0)
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("serve", help="run the API (+ bus consumer if a broker is configured)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("show", help="print the first triaged incident from a running API")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--wait", type=float, default=120)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("wait", help="block until the API is healthy")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--timeout", type=float, default=120)
    p.set_defaults(fn=cmd_wait)

    a = ap.parse_args(argv)
    return a.fn(a, settings)


if __name__ == "__main__":
    sys.exit(main())
