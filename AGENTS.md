# Astra - notes for coding agents (AO workers, Claude Code, Codex, Cursor)

Astra is a self-improving tool-using agent. Read `README.md` first. Design in `docs/spec.md`.

## Layout

- `astra/gateway.py` tool gateway (attach any ToolServer, probe, call, tool model)
- `astra/memory.py` four durable stores under `memory/` (episodic, semantic, skills, tool_model) + prompt patches
- `astra/controller.py` cost/speed policy (model tier, step budget, mode)
- `astra/actor.py` does the task; `astra/reflector.py` writes memory after each run
- `astra/engine.py` run -> grade -> keep/revert -> reflect -> record (`runs/results.tsv`)
- `astra/tools/mcp_stdio.py` real MCP client; `astra/tools/mcp_serve.py` exposes fixtures as MCP servers
- `astra/fixtures/` unseen third-party stand-ins (CRM, tracker). Astra has no code that knows about them.
- `astra/tasks.py` task families + graders. Graders never leak hidden rules.
- `scoreboard/` static scoreboard; `python -m astra scoreboard` regenerates `scoreboard/data.js`

## Rules for workers

- Never put app-specific logic in `actor.py`, `reflector.py`, `gateway.py`, `controller.py`. Astra must stay tool-agnostic.
- Never let graders reveal hidden rules; they may name what kind of thing was wrong and which ids.
- Learning runs commit `memory/`, `runs/results.tsv`, `runs/<run>/`, `scoreboard/data.js`. Do not hand-edit memory.
- Stdlib only. Python 3.11+.
- Smoke test: `ASTRA_LLM=mock python -m astra --memory /tmp/m --runs /tmp/r demo --runs 3`

## AO skills

`.agents/skills/learning-run/SKILL.md` - execute one learning iteration and commit artefacts.
`.agents/skills/attach-mcp/SKILL.md` - attach a new MCP server and run Astra against it.
