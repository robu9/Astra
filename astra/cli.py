"""Astra CLI.

  python3 -m astra demo                       # crm_at_risk x5 then tracker_triage x5 (new tool surface, same agent)
  python3 -m astra run crm_at_risk --runs 5   # one family
  python3 -m astra run tracker_triage --runs 5 --transport mcp   # same, but through a real MCP stdio process
  python3 -m astra attach --mcp "gh=npx -y @modelcontextprotocol/server-github" --task "..." --runs 3
  python3 -m astra memory                     # print what Astra knows
  python3 -m astra reset                      # wipe memory + runs
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
from pathlib import Path

from astra.engine import Engine, RESULT_COLS
from astra.tasks import FAMILIES

# Tool-name prefixes hidden from the agent unless --allow-writes is given (external MCPs are live systems).
WRITE_PREFIXES = ("create", "update", "delete", "remove", "push", "merge", "add_", "post", "send", "write", "set_",
                  "fork", "close", "assign", "edit", "put", "patch", "upload", "move", "archive", "trash", "label",
                  "publish", "reply", "execute", "run", "trigger", "approve", "reject")


def cmd_run(a):
    eng = Engine(a.memory, a.runs)
    for fam in a.family:
        eng.run_family(fam, runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    _summary(a.runs, a.memory)


def cmd_demo(a):
    eng = Engine(a.memory, a.runs)
    print(f"### Phase 1: unseen tool surface #1 (CRM), same general task, {a.runs_n} fresh data snapshots")
    eng.run_family("crm_at_risk", runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    print("\n### Phase 2: attach unseen tool surface #2 (tracker). No code change. Learning starts again.")
    eng.run_family("tracker_triage", runs=a.runs_n, start_seed=a.seed, transport=a.transport)
    _summary(a.runs, a.memory)


def cmd_attach(a):
    """Arbitrary MCP servers + arbitrary task. Graded by the LLM judge (no fixture truth available)."""
    from astra.tools.mcp_stdio import MCPStdioServer
    eng = Engine(a.memory, a.runs)
    family = a.family[0] if a.family else "user_task"
    schema = json.loads(a.schema) if a.schema else {"result": "string or object"}
    deny = () if a.allow_writes else WRITE_PREFIXES
    for i in range(a.runs_n):
        parsed = []
        for spec in a.mcp:
            name, sep, cmd = spec.partition("=")
            if not sep or not name.strip() or not cmd.strip():
                raise SystemExit(f"invalid --mcp {spec!r}; expected name=command")
            parsed.append((name.strip(), shlex.split(cmd)))
        servers = []
        try:
            for name, command in parsed:
                servers.append(MCPStdioServer(name, command, timeout=180))
        except Exception:
            for server in servers:
                server.close()
            raise
        eng.run_once(family, a.task, schema, servers, seed=a.seed + i, grader=None,
                     run_id=f"{family}-r{a.seed + i:02d}", deny=deny)
    _summary(a.runs, a.memory)


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
    """Run iterations sequentially in one AO worker so memory carries forward."""
    from astra.ao_client import AOClient, worker_prompt
    ao = AOClient()
    if not ao.available():
        raise SystemExit("AO daemon not reachable on 127.0.0.1:%s. Open the AO desktop app (or `ao start`) first."
                         % __import__("os").environ.get("AO_PORT", "3001"))
    jobs = [(fam, a.seed + i) for fam in a.family for i in range(a.runs_n)]
    first_family, first_seed = jobs[0]
    sid = ao.spawn(worker_prompt(first_family, first_seed, a.transport), name="astra learning loop")
    print(f"spawned AO worker {sid} for {first_family} seed {first_seed}")
    ao.wait_until_idle(sid, timeout=a.ao_timeout, poll=a.ao_poll)
    for fam, seed in jobs[1:]:
        ao.send(sid, worker_prompt(fam, seed, a.transport, followup=True))
        print(f"continued AO worker {sid} for {fam} seed {seed}")
        ao.wait_until_idle(sid, timeout=a.ao_timeout, poll=a.ao_poll)


def cmd_reset(a):
    from astra.memory import Memory
    memory_root = _safe_reset_path(a.memory)
    runs_root = _safe_reset_path(a.runs)
    if memory_root == runs_root or memory_root.is_relative_to(runs_root) or runs_root.is_relative_to(memory_root):
        raise SystemExit("memory and runs reset paths must be separate, non-nested directories")
    Memory(memory_root).wipe()
    runs_root.mkdir(parents=True, exist_ok=True)
    for p in runs_root.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    (runs_root / "results.tsv").write_text("\t".join(RESULT_COLS) + "\n", encoding="utf-8")
    from astra.scoreboard import build
    build(str(runs_root), str(memory_root))
    print("memory and runs wiped")


def _safe_reset_path(value: str) -> Path:
    path = Path(value).resolve()
    cwd = Path.cwd().resolve()
    if path in (Path("/").resolve(), Path.home().resolve(), cwd) or cwd.is_relative_to(path):
        raise SystemExit(f"refusing unsafe reset path: {path}")
    return path


def _summary(runs_root: str, memory_root: str):
    tsv = Path(runs_root) / "results.tsv"
    if not tsv.exists():
        return
    try:
        from astra.scoreboard import build
        build(runs_root, memory_root)
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
    p = sub.add_parser("ao-run", help="run sequential learning iterations in an AO worker")
    p.add_argument("family", nargs="+", choices=list(FAMILIES))
    p.add_argument("--runs", dest="runs_n", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transport", choices=["inprocess", "mcp"], default="inprocess")
    p.add_argument("--ao-timeout", type=float, default=1800, help="seconds to wait for each AO iteration")
    p.add_argument("--ao-poll", type=float, default=5, help="AO status polling interval")
    p.set_defaults(fn=cmd_ao_run)
    sub.add_parser("scoreboard").set_defaults(fn=cmd_scoreboard)
    sub.add_parser("memory").set_defaults(fn=cmd_memory)
    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
