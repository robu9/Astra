---
name: learning-run
description: Execute exactly one Astra learning iteration (act, grade, reflect) for a task family and commit the artefacts. Use when an AO orchestrator delegates "run Astra on <family> seed <n>".
---

# Astra learning run

One delegated AO turn = one learning iteration on a fresh data snapshot. Multiple turns may run sequentially in the same worker so memory carries forward.

1. `python3 -m astra run <family> --runs 1 --seed <seed> [--transport mcp]`
   families: `crm_at_risk`, `tracker_triage`
2. Read `runs/<family>-r<seed>/reflection.json` and `memory/semantic.json`, `memory/skills.json`, `memory/tool_model.json`.
3. Compare the new last row of `runs/results.tsv` with the previous row of the same family: quality, tool_calls, tool_errors, cost_usd, latency_s, facts, skills_promoted, kept.
4. `python3 -m astra scoreboard`
5. Commit only: `memory/`, `runs/results.tsv`, `runs/<family>-r<seed>/`, `scoreboard/data.js`. Message: `learn(<family>): run r<seed>`. Push.
6. Report in one paragraph: what Astra learned, what it kept or reverted, how the four metrics moved.

Do not edit code under `astra/`. If a run crashes, report the traceback; do not patch memory by hand.
