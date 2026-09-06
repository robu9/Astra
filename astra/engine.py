"""Engine: run -> grade -> keep/revert -> reflect -> record. One iteration per fresh data snapshot.

Every run writes:
  runs/<run_id>/trace.json        full step trace
  runs/<run_id>/reflection.json   what memory was written
  runs/results.tsv                one metrics row per run (the scoreboard reads this)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from astra.actor import Actor
from astra.controller import Controller
from astra.gateway import ToolGateway
from astra.llm import make_llm
from astra.memory import Memory
from astra.reflector import Reflector
from astra.tasks import FAMILIES, TaskFamily, fixture_for
from astra.tools.base import ToolServer
from astra.tools.mcp_stdio import MCPStdioServer

RESULT_COLS = ["run_id", "family", "servers", "seed", "mode", "model", "quality", "success", "tool_calls", "tool_errors",
               "cached_calls", "llm_tokens", "cost_usd", "reflect_cost_usd", "latency_s", "facts", "skills_promoted",
               "skills_candidate", "tools_known", "kept", "what_went_wrong"]


@dataclass
class RunResult:
    run_id: str
    family: str
    servers: list[str]
    seed: int
    quality: float
    success: bool
    feedback: str
    trace: dict
    reflection: dict
    metrics: dict = field(default_factory=dict)


class Engine:
    def __init__(self, memory_root: str | Path = "memory", runs_root: str | Path = "runs", llm=None, models=None,
                 log=print):
        self.memory = Memory(memory_root)
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        if llm is None:
            llm, models = make_llm()
        self.llm, self.models = llm, models
        self.gateway = ToolGateway(self.memory)
        self.controller = Controller(self.memory, self.models)
        self.actor = Actor(self.llm, self.gateway, self.memory)
        self.reflector = Reflector(self.llm, self.gateway, self.memory)
        self.log = log

    # --- one run ------------------------------------------------------------------------------------------------
    def run_once(self, family_name: str, task: str, answer_schema: dict, servers: list[ToolServer], seed: int,
                 grader=None, run_id: str | None = None, deny: tuple[str, ...] = ()) -> RunResult:
        run_id = run_id or f"{family_name}-{int(time.time())}-{seed}"
        names = []
        for s in servers:
            names += self.gateway.attach(s, deny=deny)
        server_names = self.gateway.attached()
        budget = self.controller.plan_run(task, family_name, server_names, names)
        self.log(f"\n== {run_id}  mode={budget.mode} model={budget.model}  ({budget.reason})")
        t0 = time.perf_counter()
        trace = self.actor.run(run_id, task, family_name, answer_schema, budget,
                               on_step=lambda st: self.log(f"   [{st.idx}] {st.tool or 'FINAL'} "
                                                           f"{json.dumps(st.args, default=str)[:90]} -> {'ok' if st.ok else 'ERR'}"))
        wall = time.perf_counter() - t0

        if grader is not None:
            quality, feedback, success = grader(servers[0], trace.final)
        else:
            from astra.judge import llm_grade
            quality, feedback, success = llm_grade(self.llm, self.models["fast"], task, trace.final, trace.compact())
        self.log(f"   quality={quality:.2f} success={success} calls={self.gateway.run_stats()['tool_calls']} "
                 f"cost=${trace.llm_cost_usd:.4f} wall={wall:.1f}s\n   feedback: {feedback}")

        # keep-or-revert candidates proposed after the previous run
        kept = self._resolve_candidates(family_name, server_names, quality, run_id)
        self.memory.skills.record_use(family_name, server_names, success)

        rmodel = self.controller.reflection_model(quality)
        reflection = self.reflector.reflect(trace, family_name, quality, feedback, success, rmodel, names)
        self.log(f"   reflect[{rmodel}]: +{reflection['facts_new']} facts, +{reflection['conventions_new']} tool notes, "
                 f"skill={reflection['skill']}, prompt_patch={reflection['prompt_patch']}"
                 + (f"\n   went wrong: {reflection['what_went_wrong']}" if reflection.get("what_went_wrong") else ""))

        snap = self.memory.snapshot(server_names)
        stats = self.gateway.run_stats()
        metrics = {
            "run_id": run_id, "family": family_name, "servers": "+".join(server_names), "seed": seed, "mode": budget.mode,
            "model": budget.model, "quality": quality, "success": success, "tool_calls": stats["tool_calls"],
            "tool_errors": stats["tool_errors"], "cached_calls": stats["cached_calls"], "llm_tokens": trace.tokens,
            "cost_usd": round(trace.llm_cost_usd, 6), "reflect_cost_usd": round(reflection["cost_usd"], 6),
            "latency_s": round(wall, 3), "facts": snap["facts"], "skills_promoted": snap["skills_promoted"],
            "skills_candidate": snap["skills_candidate"], "tools_known": snap["tools_known"], "kept": kept,
            "what_went_wrong": reflection.get("what_went_wrong", "").replace("\t", " ").replace("\n", " "),
        }
        self._persist(run_id, trace.to_dict() | {"quality": quality, "feedback": feedback}, reflection, metrics)
        for s in servers:
            self.gateway.detach(s.name)
        return RunResult(run_id, family_name, server_names, seed, quality, success, feedback, trace.to_dict(),
                         reflection, metrics)

    def _resolve_candidates(self, family: str, servers: list[str], quality: float, run_id: str) -> str:
        eps = self.memory.episodic.recent(family, servers, n=1)
        if not eps:
            return "n/a"
        prev = eps[-1].get("quality", 0.0)
        keep = quality >= prev - 0.05  # tolerate noise from the fresh data snapshot
        has_cand = self.memory.skills.count("candidate") > 0 or bool(
            self.memory.prompt_patches.get(f"{family}@{'+'.join(sorted(servers))}", {}).get("candidate"))
        if keep:
            self.memory.skills.promote(family, servers, run_id)
        else:
            self.memory.skills.reject_candidate(family, servers, run_id, f"quality fell {prev:.2f} -> {quality:.2f}")
        self.memory.resolve_prompt_patch(family, servers, keep, run_id)
        if not has_cand:
            return "none"
        return "kept" if keep else "reverted"

    def _persist(self, run_id: str, trace: dict, reflection: dict, metrics: dict) -> None:
        d = self.runs_root / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "trace.json").write_text(json.dumps(trace, indent=1, default=str), encoding="utf-8")
        (d / "reflection.json").write_text(json.dumps(reflection, indent=1, default=str), encoding="utf-8")
        tsv = self.runs_root / "results.tsv"
        if not tsv.exists():
            tsv.write_text("\t".join(RESULT_COLS) + "\n", encoding="utf-8")
        with tsv.open("a", encoding="utf-8") as f:
            f.write("\t".join(str(metrics.get(c, "")) for c in RESULT_COLS) + "\n")

    # --- families on fixtures -----------------------------------------------------------------------------------------
    def run_family(self, family_name: str, runs: int = 5, start_seed: int = 1, transport: str = "inprocess") -> list[RunResult]:
        fam: TaskFamily = FAMILIES[family_name]
        out = []
        for i in range(runs):
            seed = start_seed + i
            if transport == "mcp":
                server = MCPStdioServer(fam.fixture, [sys.executable, "-m", "astra.tools.mcp_serve", fam.fixture,
                                                      "--seed", str(seed)])
                # the MCP process holds the truth; graders need it too, so mirror the same seed in-process
                truth = fixture_for(fam, seed)
                grader = _mcp_grader(fam, truth, server)
            else:
                server = fixture_for(fam, seed)
                grader = fam.grade
            out.append(self.run_once(family_name, fam.task, fam.answer_schema, [server], seed, grader,
                                     run_id=f"{family_name}-r{seed:02d}"))
        return out


def _mcp_grader(fam: TaskFamily, truth, mcp: MCPStdioServer):
    """Grade an MCP-transport run. Read-only families grade from the mirror; write families replay state via MCP."""

    def grade(_server, final):
        if fam.fixture == "tracker":
            # pull actual state back through the MCP server so mutations count
            issues = {}
            cursor = None
            while True:
                page = mcp.call("list_issues", {"status": "open", **({"cursor": cursor} if cursor else {})})
                for it in page.get("items", []):
                    issues[it["id"]] = it
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            for iid, it in issues.items():
                if iid in truth.app.issues:
                    truth.app.issues[iid]["labels"] = it["labels"]
                    truth.app.issues[iid]["assignee"] = it["assignee"]
        return fam.grade(truth, final)

    return grade
