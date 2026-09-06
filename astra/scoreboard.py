"""Build scoreboard/data.js from runs/results.tsv + memory so scoreboard/index.html works from file:// or `ao preview`."""
from __future__ import annotations

import json
from pathlib import Path

from astra.memory import Memory


def build(runs_root: str = "runs", memory_root: str = "memory", out_dir: str = "scoreboard") -> Path:
    tsv = Path(runs_root) / "results.tsv"
    rows = []
    if tsv.exists():
        lines = [l for l in tsv.read_text(encoding="utf-8").splitlines() if l.strip()]
        head = lines[0].split("\t")
        for l in lines[1:]:
            vals = l.split("\t")
            row = dict(zip(head, vals))
            for k in ("quality", "cost_usd", "reflect_cost_usd", "latency_s"):
                row[k] = float(row.get(k) or 0)
            for k in ("tool_calls", "tool_errors", "cached_calls", "llm_tokens", "facts", "skills_promoted",
                      "skills_candidate", "tools_known", "seed"):
                row[k] = int(float(row.get(k) or 0))
            row["success"] = row.get("success") == "True"
            rows.append(row)
    m = Memory(memory_root)
    mem = {
        "facts": m.semantic.facts,
        "skills": m.skills.skills,
        "tool_model": {q: {k: v for k, v in t.items() if k != "description"} for q, t in m.tool_model.tools.items()
                       if t["calls"] or t["conventions"]},
        "episodes": m.episodic.all()[-40:],
        "prompt_patches": m.prompt_patches,
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.js").write_text("window.ASTRA_DATA = " + json.dumps({"runs": rows, "memory": mem}, default=str) + ";\n",
                                 encoding="utf-8")
    return out / "data.js"
