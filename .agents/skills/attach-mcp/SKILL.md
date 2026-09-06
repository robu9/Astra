---
name: attach-mcp
description: Attach a new third-party MCP server to Astra and run a general task against it several times so the learning loop starts on an unseen tool surface. Use when asked to "point Astra at <MCP>".
---

# Attach a new MCP server

```bash
python -m astra attach \
  --mcp "gh=npx -y @modelcontextprotocol/server-github" \
  --task "Summarise open PRs older than 7 days with their reviewers and blockers" \
  --schema '{"stale_prs":[{"number":"int","title":"string","reviewers":["string"],"blocker":"string"}]}' \
  --family gh_stale_prs --runs 3
```

- `--mcp name=command` is repeatable; any stdio MCP server works. Secrets go in env, never in the repo.
- No grader exists for arbitrary tasks, so the LLM judge scores each run. Improvement still shows in tool_errors, tool_calls, cost, latency and in `memory/` growth.
- After the runs: `python -m astra scoreboard`, then commit `memory/`, `runs/`, `scoreboard/data.js` with `learn(<family>): attach <name>`.
- Report: what the gateway discovered (tool count, probed shapes), what facts/conventions were learned, and the run-1 vs run-N metrics.
