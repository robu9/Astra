"""Astra CLI.

  python -m astra demo                       # crm_at_risk x5 then tracker_triage x5 (new tool surface, same agent)
  python -m astra run crm_at_risk --runs 5   # one family
  python -m astra run tracker_triage --runs 5 --transport mcp   # same, but through a real MCP stdio process
  python -m astra attach --mcp "gh=npx -y @modelcontextprotocol/server-github" --task "..." --runs 3
  python -m astra memory                     # print what Astra knows
  python -m astra reset                      # wipe memory + runs
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
from pathlib import Path

from astra.engine import Engine
from astra.tasks import FAMILIES

# Tool-name prefixes hidden from the agent unless --allow-writes is given (external MCPs are live systems).
WRITE_PREFIXES = ("create", "update", "delete", "remove", "push", "merge", "add_", "post", "send", "write", "set_",
                  "fork", "close", "assign", "edit", "put", "patch", "upload", "move", "archive", "trash", "label")


def cmd_run(a):
    eng = Engine(a.memory, a.runs)
    for fam in a.family:
        eng.run_family(fam, runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    _summary(a.runs)


def cmd_demo(a):
    eng = Engine(a.memory, a.runs)
    print("### Phase 1: unseen tool surface #1 (CRM), same general task, 5 fresh data snapshots")
    eng.run_family("crm_at_risk", runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    print("\n### Phase 2: attach unseen tool surface #2 (tracker). No code change. Learning starts again.")
    eng.run_family("tracker_triage", runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    _summary(a.runs)


def cmd_attach(a):
    """Arbitrary MCP servers + arbitrary task. Graded by the LLM judge (no fixture truth available)."""
    from astra.tools.mcp_stdio import MCPStdioServer
    eng = Engine(a.memory, a.runs)
    family = a.family[0] if a.family else "user_task"
    schema = json.loads(a.schema) if a.schema else {"result": "string or object"}
    deny = () if a.allow_writes else WRITE_PREFIXES
    for i in range(a.runs_n):
        servers = []
        for spec in a.mcp:
            name, _, cmd = spec.partition("=")
            servers.append(MCPStdioServer(name.strip(), shlex.split(cmd), timeout=180))
        eng.run_once(family, a.task, schema, servers, seed=a.seed + i, grader=None,
                     run_id=f"{family}-r{a.seed + i:02d}", deny=deny)
    _summary(a.runs)


def cmd_memory(a):
    from astra.memory import Memory
    m = Memory(a.memory)
    print(f"facts: {m.semantic.count()}  skills: promoted={m.skills.count('promoted')} candidate={m.skills.count('candidate')} "
          f"rejected={m.skills.count('rejected')}  tools tracked: {len(m.tool_model.tools)}  episodes: {len(m.episodic.all())}\n")
    for f in m.semantic.facts:
        print(f"  [{f.get('server') or 'general'} {f['confidence']:.2f}] {f['fact']}")
    for s in m.skills.skills:
        print(f"\n  skill '{s['name']}' v{s['version']} ({s['status']}) wins {s['wins']}/{s['uses']}")
        for st in s["steps"]:
            print(f"     - {st}")
    print()
    for q, t in m.tool_model.tools.items():
        if t["calls"] or t["conventions"]:
            print(f"  {q}: {t['ok']}/{t['calls']} ok, {t['latency_total']:.2f}s; {'; '.join(t['conventions'])}")


def cmd_scoreboard(a):
    from astra.scoreboard import build
    p = build(a.runs, a.memory)
    print(f"wrote {p}. Open scoreboard/index.html (or `ao preview scoreboard/index.html` inside an AO session).")


def cmd_ao_run(a):
    """Spawn one AO worker per learning run so every iteration is a visible, isolated AO session."""
    from astra.ao_client import AOClient, worker_prompt
    ao = AOClient()
    if not ao.available():
        raise SystemExit("AO daemon not reachable on 127.0.0.1:%s. Open the AO desktop app (or `ao start`) first."
                         % __import__("os").environ.get("AO_PORT", "3001"))
    for fam in a.family:
        for i in range(a.runs_n):
            seed = a.seed + i
            sid = ao.spawn(worker_prompt(fam, seed, a.transport), name=f"astra {fam} r{seed:02d}")
            print(f"spawned AO worker {sid} for {fam} seed {seed}")


def cmd_reset(a):
    for p in (a.memory, a.runs):
        shutil.rmtree(p, ignore_errors=True)
    print("memory and runs wiped")


def _summary(runs_root: str):
    tsv = Path(runs_root) / "results.tsv"
    if not tsv.exists():
        return
    try:
        from astra.scoreboard import build
        build(runs_root, "memory" if runs_root == "runs" else str(Path(runs_root).parent / "astra_mem"))
    except Exception:
        pass
    rows = [l.split("\t") for l in tsv.read_text(encoding="utf-8").splitlines() if l.strip()]
    head, body = rows[0], rows[1:]
    ix = {c: i for i, c in enumerate(head)}
    print("\n" + "-" * 100)
    print(f"{'run':24} {'mode':11} {'quality':>7} {'ok':>3} {'calls':>5} {'errs':>4} {'cost$':>8} {'lat s':>6} {'facts':>5} {'skills':>6} kept")
    for r in body:
        print(f"{r[ix['run_id']]:24} {r[ix['mode']]:11} {float(r[ix['quality']]):7.2f} {r[ix['success']][:1]:>3} "
              f"{r[ix['tool_calls']]:>5} {r[ix['tool_errors']]:>4} {float(r[ix['cost_usd']]):8.4f} {float(r[ix['latency_s']]):6.1f} "
              f"{r[ix['facts']]:>5} {r[ix['skills_promoted']]:>6} {r[ix['kept']]}")
    print("-" * 100)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="astra")
    ap.add_argument("--memory", default="memory")
    ap.add_argument("--runs", default="runs", help="runs output dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("family", nargs="+", choices=list(FAMILIES))
    p.add_argument("--runs", dest="runs_n", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transport", choices=["inprocess", "mcp"], default="inprocess")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("demo")
    p.add_argument("--runs", dest="runs_n", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transport", choices=["inprocess", "mcp"], default="inprocess")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("attach")
    p.add_argument("--mcp", action="append", required=True, help='name=command, e.g. "gh=npx -y @modelcontextprotocol/server-github"')
    p.add_argument("--task", required=True)
    p.add_argument("--family", nargs=1)
    p.add_argument("--schema", help="JSON answer schema")
    p.add_argument("--runs", dest="runs_n", type=int, default=3)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--allow-writes", action="store_true", help="expose create/update/delete-style tools (default: hidden)")
    p.set_defaults(fn=cmd_attach)
    p = sub.add_parser("ao-run", help="spawn AO workers, one per learning run")
    p.add_argument("family", nargs="+", choices=list(FAMILIES))
    p.add_argument("--runs", dest="runs_n", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transport", choices=["inprocess", "mcp"], default="inprocess")
    p.set_defaults(fn=cmd_ao_run)
    sub.add_parser("scoreboard").set_defaults(fn=cmd_scoreboard)
    sub.add_parser("memory").set_defaults(fn=cmd_memory)
    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
