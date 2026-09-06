# Astra

**A self-improving, tool-agnostic agent.** Give Astra any third-party tool surface (MCP server / API) and a general task. It does the task, reflects on its own trace, grows durable memory and skills, and is measurably better — more accurate, fewer tool calls, cheaper, faster — on the next run. Plug in a tool it has never seen and the same loop starts again with zero code changes.

Built in 30 hours for **Syndicate by Maximor, Track 1: Automated Agent Engineering**, entirely inside **Agent Orchestrator (AO)**.

- Repo: https://github.com/robu9/Astra
- Design spec: [`docs/spec.md`](docs/spec.md)
- Track: [Devpost](https://syndicate-by-maximor.devpost.com/) · [AO](https://aoagents.dev/)

---

## Table of contents

1. [What Track 1 asks, and Astra's answers](#1-what-track-1-asks-and-astras-answers)
2. [Architecture](#2-architecture)
3. [The learning loop, step by step](#3-the-learning-loop-step-by-step)
4. [Memory: what is stored and how it is retrieved](#4-memory-what-is-stored-and-how-it-is-retrieved)
5. [Cost and speed control](#5-cost-and-speed-control)
6. [Tool-agnostic by construction](#6-tool-agnostic-by-construction)
7. [Evaluation: fixtures, graders, metrics](#7-evaluation-fixtures-graders-metrics)
8. [Results](#8-results)
9. [How to run Astra](#9-how-to-run-astra)
10. [How we built it with AO](#10-how-we-built-it-with-ao)
11. [Repo layout](#11-repo-layout)
12. [Honest limits](#12-honest-limits)

---

## 1. What Track 1 asks, and Astra's answers

The track brief (Devpost + organiser clarification on Discord) asks for **a well-engineered agent whose learning loop lets it get genuinely better over time at using third-party tools**, domain-agnostic, with a good cost/speed balance. Four explicit questions:

### Q1. How does it get better over time?

Every run is the same five-stage cycle: **act → trace → reflect → write memory → retrieve next time.** The policy code never changes between runs; the *context Astra has earned* does.

| After run N Astra writes | Which changes run N+1 |
| --- | --- |
| **Semantic facts** extracted from tool payloads (e.g. "`health: R` means at risk", "`[billing]` prefix is the component") | The actor is told these facts and stops re-deriving them |
| **Tool conventions** (exact enum casing, pagination cursor, hidden id formats) | The tool listing itself carries the learned notes; calling errors disappear |
| **A procedural skill** (ordered steps + tools) — *candidate* until the next run confirms it | Controller switches to `skilled` mode: fast model, tight step budget |
| **A prompt patch** (2–5 lines of self-instruction) — *candidate* until confirmed | Actor system prompt changes |
| **An episode** (quality, calls, cost, what went wrong) | Controller uses it to pick model tier and step budget |

Candidates are kept only if the next run's quality does not regress (**keep-or-revert**, `astra/engine.py:_resolve_candidates`). Skills accumulate wins/uses; facts gain confidence when re-observed and are demoted when reflection flags them wrong.

### Q2. Can you show outputs getting better through self-reflection and memory growing?

Yes, and everything is a plain file you can diff:

- `runs/results.tsv` — one row per run: quality, success, tool calls, tool errors, tokens, cost, latency, facts, skills, kept/reverted, what went wrong.
- `runs/<run>/trace.json` — every step, tool call, observation, model, cost.
- `runs/<run>/reflection.json` — exactly which facts, conventions, skill and prompt patch were written after that run.
- `memory/semantic.json`, `memory/skills.json`, `memory/tool_model.json`, `memory/episodic.jsonl`, `memory/prompt_patches.json` — the growing memory itself.
- `scoreboard/index.html` — quality / cost / tool calls / latency curves plus the live memory contents.

Run 1 on a new tool: wrong enum casing, one page of results, ignores notes, raw owner ids. Run 3: paginates, reads notes, resolves owners, flags inactive reps, half the cost. See [Results](#8-results).

### Q3. Can it learn complex contextual logic from third-party data and apply it later?

That is the specific thing the fixtures test. The hidden rules live **in the data, not the schema**, and the data is regenerated with a new seed each run so ids can never be memorised — only understanding transfers:

- CRM: `health` is a colour code `G/A/R`; free-text notes with "PO pending" / "budget freeze" mean at-risk even when health is green; owners with `active:false` have left and their deals must be reported `UNASSIGNED`; closed stages are not open.
- Tracker: priority meaning is in `list_labels` descriptions (sev-1 outage / data loss, sev-2 broken / 500 / fails, sev-3 minor); component is the `[bracket]` prefix in the title and each member `owns` components; a comment "duplicate of #N" means label `duplicate` and do not assign.

Graders never reveal these rules. They say *how many* were missed and *which ids*; reflection has to inspect the trace and infer the rule. The rules then appear verbatim in `memory/semantic.json` and are applied on the next, different snapshot.

### Q4. Does it balance cost-effectiveness and speed?

The **controller** (`astra/controller.py`) routes every run:

| Situation | Model | Step budget | Mode |
| --- | --- | --- | --- |
| No episodes for this task on these tools | strong | 30 | `novel` |
| Last run scored < 0.4 | strong | last calls × 1.5 + 16 | `recovering` |
| Promoted skill + ≥50 % tools known + last run succeeded | **fast** | last calls × 1.5 + 6 | `skilled` |
| Otherwise | fast if tools known, else strong | in between | — |

Plus: reflection uses the fast model unless the run failed badly; the gateway caches identical read-only calls within a run; probing of a tool's return shape happens once per tool ever; the tool model warns the actor away from tools with a bad success rate. Measured effect in the smoke run below: tracker cost **$0.129 → $0.0067 per run (19×)**, latency **1.0 s → 0.4 s**, tool errors **25 → 0**, quality **0.04 → 1.00**.

---

## 2. Architecture

Same diagram as the design spec. Nothing inside `astra` knows what app is behind a tool.

```mermaid
flowchart TB
    subgraph inputs [Anything new]
        Task[General task]
        MCP[MCP or API tools]
        Signal[Optional grader]
    end

    subgraph astra [Astra agent]
        Gateway[Tool gateway discover probe call]
        Actor[Actor do the task]
        Memory[Memory store]
        Reflector[Reflector after each run]
        Skills[Skill compiler]
        Controller[Cost and speed controller]
    end

    subgraph memoryKinds [What memory actually stores]
        Episodic[Episodic traces]
        Semantic[Semantic facts from tool data]
        Procedural[Procedural skills how to use a tool]
        ToolModel[Tool model success cost latency]
    end

    Task --> Actor
    MCP --> Gateway
    Gateway --> Actor
    Actor --> Reflector
    Signal --> Reflector
    Reflector --> Memory
    Memory --> Episodic
    Memory --> Semantic
    Memory --> Procedural
    Memory --> ToolModel
    Skills --> Procedural
    Memory --> Actor
    Controller --> Actor
    Controller --> Gateway
```

| Component | File | One-line job |
| --- | --- | --- |
| Tool gateway | `astra/gateway.py` | Attach any `ToolServer`, namespace tools `server.tool`, probe arg-less read tools once, call with timing / error signatures / per-run cache, feed the tool model |
| Actor | `astra/actor.py` | JSON-protocol ReAct loop: one tool call or one final answer per turn, memory injected into the prompt, no app logic |
| Memory | `astra/memory.py` | Episodic, semantic, skills, tool model, prompt patches. File-backed. Scoped retrieval |
| Reflector | `astra/reflector.py` | Reads trace + grade, writes facts / conventions / skill / prompt patch / episode |
| Controller | `astra/controller.py` | Picks model tier, step budget and mode from memory |
| Engine | `astra/engine.py` | run → grade → keep-or-revert → reflect → record |
| MCP client | `astra/tools/mcp_stdio.py` | Real MCP over stdio (`initialize`, `tools/list`, `tools/call`) |
| MCP server shim | `astra/tools/mcp_serve.py` | Exposes any in-process server as a real MCP process (used to run fixtures over MCP) |
| Fixtures | `astra/fixtures/` | Two unseen "third-party apps" with hidden rules and API quirks |
| Tasks + graders | `astra/tasks.py` | General tasks repeated over fresh snapshots; graders never leak rules |
| LLM judge | `astra/judge.py` | Scores arbitrary user tasks when there is no grader |
| AO client | `astra/ao_client.py` | `ao spawn` / `ao send` / HTTP fallback; sequential learning turns in one AO worker |

How AO wraps the runtime:

```mermaid
flowchart LR
    Human[Task plus MCP]
    Orch[AO orchestrator]
    Run[AO worker sequential turns]
    Engine[Astra act grade reflect]
    Files[memory and metrics in repo]
    Preview[AO browser scoreboard]

    Human --> Orch
    Orch -->|"ao spawn then ao send"| Run
    Run --> Engine
    Engine --> Files
    Files --> Preview
```

---

## 3. The learning loop, step by step

One iteration (`Engine.run_once`):

1. **Attach** the tool servers. Gateway lists tools, registers them in the tool model, probes arg-less read tools once (learns return shape, e.g. `object keys=['items','next_cursor','total']; items[0] keys=[...]`).
2. **Plan budget.** Controller looks at episodes, skills and tool-model coverage → model tier, step budget, mode.
3. **Retrieve.** Memory returns facts (scoped to attached servers + task keywords), the skill for this task family, the last 3 episodes, and the active prompt patch.
4. **Act.** Actor runs the JSON tool-calling loop. Each observation is the full tool payload (up to 3.5 KB) so pagination cursors and nested fields are visible. Errors are shown verbatim and the actor is told to fix, not loop.
5. **Grade.** Fixture grader (ground truth) or LLM judge (arbitrary tasks) → `quality ∈ [0,1]`, feedback, success.
6. **Keep or revert.** Candidates proposed after the previous run are promoted if `quality ≥ prev − 0.05`, else rejected / rolled back.
7. **Reflect.** Reflector (fast model, strong if the run failed badly) emits facts, tool conventions, a skill, a prompt patch, bad-facts to demote, and one sentence on what went wrong. All of it is written to `memory/`.
8. **Record.** Trace, reflection and a metrics row are persisted. Servers are detached; the tool model persists.

Nothing in this loop references CRM, tracker, GitHub, or any other app.

---

## 4. Memory: what is stored and how it is retrieved

| Store | File | Entry | Retrieval |
| --- | --- | --- | --- |
| Episodic | `memory/episodic.jsonl` | run id, family, servers, quality, success, calls, errors, cost, mode, what went wrong | last 3 for the exact same family and server set |
| Semantic | `memory/semantic.json` | fact, server (or `null` = general lesson), source tool, evidence quote, confidence, first/last run, seen count, tags | score = confidence × (1 + 0.4 × keyword overlap) + bonus for matching server; top 12; facts from other servers are excluded |
| Procedural | `memory/skills.json` | name, when, steps[], tools[], servers, family, status `candidate/promoted/rejected`, version, wins/uses | skills whose servers ⊆ attached servers and whose family or `when` matches |
| Tool model | `memory/tool_model.json` | per tool: calls, ok, errors, latency total, error signatures, conventions[], probed, shape | injected as `learned:` notes under each tool in the prompt; drives the `known tools` ratio in the controller |
| Prompt patches | `memory/prompt_patches.json` | per family@servers: active text, candidate, history with kept/reverted | injected as SELF-INSTRUCTIONS |

Namespacing matters: facts learned on `crm` are never shown when only `tracker` is attached, but `server: null` lessons ("read all pages before deciding") transfer to every new tool. That is the mechanism behind "expose it to a new MCP and it keeps improving".

---

## 5. Cost and speed control

Levers, all measurable in `results.tsv`:

- **Model routing** (`skilled` → fast model). Reflection also on the fast model unless quality < 0.3.
- **Step budgets** derived from the last run's real call count, not a constant.
- **Per-run read cache** in the gateway (`cached_calls` column).
- **Probe once, ever** — return shapes live in the tool model.
- **Tool-model hints** stop the actor re-discovering conventions (this is where most wasted calls on run 1 come from).
- **Actor cost ceiling** (`Budget.max_cost_usd`); once crossed, the action loop makes no additional model or tool call.
- **Skill first** — with a promoted skill the actor is told the exact step order and stops exploring.

---

## 6. Tool-agnostic by construction

Astra sees only `ToolSpec {name, description, input_schema, annotations}` and JSON results. Two adapters implement `ToolServer`:

- `InProcessServer` — Python functions decorated with `@tool` (fixtures use this).
- `MCPStdioServer` — any MCP server launched as a subprocess. Verified end to end by running the fixtures themselves as MCP processes (`--transport mcp`).
An HTTP/OpenAPI adapter can be added behind the same interface, but is not included today.

To attach a real third-party MCP:

```bash
python3 -m astra attach \
  --mcp "gh=npx -y @modelcontextprotocol/server-github" \
  --task "List open PRs older than 7 days with reviewers and the blocker" \
  --schema '{"stale_prs":[{"number":"int","title":"string","reviewers":["string"],"blocker":"string"}]}' \
  --family gh_stale_prs --runs 3
```

No grader exists for arbitrary tasks, so the LLM judge scores them. Learning still shows in tool errors, calls, cost, latency and in `memory/` growth.

---

## 7. Evaluation: fixtures, graders, metrics

**Why fixtures instead of a public benchmark.** The brief is about learning *inside* a third-party tool's data over repeated runs. That needs (a) hidden business rules, (b) fresh data each run so ids cannot be memorised, (c) ground truth for honest scoring. Public benchmarks give none of these. The fixtures are deliberately small and quirky in the ways real SaaS APIs are quirky (pagination, cryptic enums, ids instead of names, information hidden in free text, records that should be skipped).

| Family | Tool surface | General task | Grader |
| --- | --- | --- | --- |
| `crm_at_risk` | 5 tools | Weekly at-risk pipeline report with owner and reason | 0.75 × F1 on deal set + 0.25 × owner accuracy |
| `tracker_triage` | 7 tools, 2 of them writes | Triage every open issue per the team's conventions | 0.6 × label accuracy + 0.4 × assignee accuracy, read from actual server state |

Metrics per run: `quality`, `success`, `tool_calls`, `tool_errors`, `cached_calls`, `llm_tokens`, `cost_usd`, `reflect_cost_usd`, `latency_s`, `facts`, `skills_promoted`, `skills_candidate`, `tools_known`, `kept`, `what_went_wrong`.

Each run uses seed `N`, so run 3 sees deals, owners, issues and comments run 1 never saw.

---

## 8. Results

The active results below were produced with **Gemini 2.5 Flash, real MCP stdio transport, fresh memory, and one continuous local learning session** across two previously unseen tool surfaces. Each run used a different seeded snapshot, so ids, names and records changed and memorising an answer could not help. The complete traces, reflections and learned memory are committed with these results.

```
run                   mode         quality  ok calls errs   cost$  lat s facts skills  kept
crm_at_risk-r01       novel           0.50   F     6    1  0.0098   22.1     3      1  n/a
crm_at_risk-r02       novel           0.58   F     8    0  0.0123   24.8     6      1  kept
crm_at_risk-r03       skilled         0.81   F    16    0  0.0364   56.0     8      1  kept
crm_at_risk-r04       skilled         1.00   T    14    0  0.0326   52.1    10      1  kept
crm_at_risk-r05       skilled         0.80   F    18    0  0.0454   66.2    12      1  reverted
tracker_triage-r01    novel           0.49   F    16    0  0.0217   43.9     5      0  n/a
tracker_triage-r02    skilled         0.73   F    17    0  0.0301   52.0     7      1  kept
tracker_triage-r03    skilled         0.64   F    27    0  0.0456   74.3    11      1  reverted
tracker_triage-r04    skilled         0.67   F    21    0  0.0397   66.5    12      1  kept
tracker_triage-r05    skilled         1.00   T    38    0  0.0709   87.5    15      1  kept
```

Both families are graded against hidden ground truth that is never exposed to the actor or reflector.

**Accuracy.** CRM improved from 0.50 to a perfect 1.00 on run 4. Tracker improved from 0.49 to a perfect 1.00 on run 5. The non-monotonic points are fresh snapshots exposing incomplete rules, rather than repeated evaluation on memorised records.

**Reliability.** The CRM first-contact invalid enum produced the only tool error in all 10 runs. Astra learned the convention after reflection and did not repeat that error. Tracker completed all five runs without a tool error, including its state-changing label and assignment calls.

**Contextual logic learned from tool data, not from the schema** (`memory/semantic.json`, confidence in brackets):

- CRM: inactive owners must be reported as `UNASSIGNED` [1.0]; green deals can still be at risk based on notes [1.0]; amber alone does not prove risk [1.0]; `list_deals(stage="OPEN")` is invalid [0.6].
- Tracker: the bracketed module maps to a member's `owns` field [1.0]; matching titles are duplicates only when comments explicitly confirm it [0.9]; production outage and data-loss reports are `sev-1` [0.9].

**Cost and speed.** The run records the tradeoff rather than hiding it. Tracker cost rises as Astra verifies more comments and reaches perfect accuracy; CRM run 4 reaches 1.00 with fewer calls and lower cost than run 5. Across the complete demo Astra made 181 tool calls for $0.3445 of actor cost plus $0.0543 of reflection cost.

**The keep-or-revert gate.** CRM run 5 and tracker run 3 regressed on new snapshots, so their proposed learning was reverted. Nothing is accepted merely because the reflector proposed it.

### Verified AO learning series

The active local results above are the strongest two-domain product demo. AO use is independently auditable on the public [`learn/astra-ao-demo`](https://github.com/robu9/Astra/tree/learn/astra-ao-demo) branch. AO worker **`astra-25`** received four sequential turns against fresh CRM snapshots; each turn committed and pushed its trace, reflection, memory, metrics and scoreboard before Astra allowed the series to continue:

```text
quality:     0.000 -> 0.167 -> 0.725 -> 0.750
facts:       5     -> 8     -> 15    -> 16
tool errors: 1     -> 0     -> 2     -> 0
```

The exact verified head is [`b06919d`](https://github.com/robu9/Astra/commit/b06919de29d8f137eeaae7f38465b4b3af27249e). The AO transcript shows the sequential instructions and learning summaries; the branch proves that the corresponding artefacts reached GitHub.

`scoreboard/index.html` renders these curves and the live memory view. Offline smoke runs (`ASTRA_LLM=mock`) exercise the same loop deterministically without a key.

---

## 9. How to run Astra

Requirements: Python 3.11+ (stdlib only). Node only if you attach npm-distributed MCP servers.

```bash
git clone https://github.com/robu9/Astra.git && cd Astra
cp .env.example .env       # set ASTRA_LLM_API_KEY (+ base URL / model names). Any OpenAI-compatible endpoint.

# Full demo: unseen tool surface #1 x5, then unseen tool surface #2 x5, same agent
python3 -m astra demo --runs 5

# One family, through a real MCP stdio process
python3 -m astra run tracker_triage --runs 5 --transport mcp

# What Astra knows now
python3 -m astra memory

# Regenerate the scoreboard, then open scoreboard/index.html
python3 -m astra scoreboard

# Attach any external MCP + any task (LLM-judged). Write-style tools are hidden unless --allow-writes.
export GITHUB_PERSONAL_ACCESS_TOKEN=$(gh auth token)
python3 -m astra attach --mcp "github=npx -y @modelcontextprotocol/server-github" \
  --task "Digest the 10 most recently updated open 'bug' issues in owner/repo: number, title, age_days, comments, assigned" \
  --schema '{"issues":[{"number":"int","title":"string","age_days":"int","comments":"int","assigned":"bool"}]}' \
  --family gh_bug_digest --runs 4

# Start over
python3 -m astra reset
```

Offline smoke test without a key: `ASTRA_LLM=mock python3 -m astra --memory /tmp/m --runs /tmp/r demo --runs 3`.

Inside an AO worker session, preview the scoreboard beside the agent: `ao preview scoreboard/index.html`.

---

## 10. How we built it with AO

AO usage was mandatory and is 25 % of the score. AO played two roles.

**Role 1 — build plane.** The repo is an AO project. The project orchestrator held the design conversation and split the work into focused workers, each in its own worktree:

| AO worker | Scope |
| --- | --- |
| runtime core | gateway, memory stores, controller, actor, reflector |
| MCP transport | stdio client + server shim, verified fixtures over MCP |
| fixtures + graders | CRM and tracker apps with hidden rules, seeded snapshots |
| engine + CLI | run/grade/keep-or-revert/reflect loop, results.tsv, CLI |
| scoreboard | static scoreboard + data builder |
| docs | README, AGENTS.md, AO skills |

Reviews and fixes (e.g. the `@tool` schema leaking `self`, observation truncation hiding `next_cursor`, skills being demoted back to candidate on re-proposal) were routed back to the owning session.

**Role 2 — run plane.** `python3 -m astra ao-run crm_at_risk --runs 5` creates one AO worker and sends it five sequential learning turns. Waiting between turns guarantees that run N+1 sees run N's committed memory. Before continuing, the controller verifies that the expected result, trace, reflection, memory and scoreboard were committed and that the exact commit reached the AO branch on `origin`. The worker follows the `learning-run` skill (`.agents/skills/learning-run/SKILL.md`) and reports what Astra learned. The AO transcript shows the learning history, and `ao preview scoreboard/index.html` shows the curves beside it.

`AGENTS.md` tells any AO worker the rules (keep Astra tool-agnostic, never leak rules through graders, never hand-edit memory). The demo video shows the AO dashboard with the session count, a run-1 vs run-N comparison, the memory files growing, and a second MCP attached with no code change.

---

## 11. Repo layout

```
astra/
  actor.py          the agent policy (tool-agnostic)
  reflector.py      post-run learning
  controller.py     cost/speed routing
  gateway.py        tool discovery, probing, calling, tool model
  memory.py         episodic / semantic / skills / tool model / prompt patches
  engine.py         run -> grade -> keep/revert -> reflect -> record
  judge.py          LLM judge for arbitrary tasks
  llm.py            OpenAI-compatible client + cost accounting + offline mock
  ao_client.py      AO spawn/send/status wait, sequential learning in one worker
  scoreboard.py     builds scoreboard/data.js
  cli.py            demo | run | attach | ao-run | memory | scoreboard | reset
  tools/            base ToolServer, MCP stdio client, MCP server shim
  fixtures/         crm.py, tracker.py (unseen third-party stand-ins)
  tasks.py          task families + graders
memory/             the growing memory (committed by learning runs)
runs/               per-run traces, reflections, results.tsv
scoreboard/         index.html + data.js
.agents/skills/     AO skills: learning-run, attach-mcp
docs/spec.md        design spec
AGENTS.md           rules for AO / coding-agent workers
```

---

## 12. Honest limits

- Two fixtures plus one real MCP, small data, 17 committed runs. Enough to show the loop; not a benchmark.
- Curves are noisy run-to-run because every run is a new snapshot and the model is Gemini 2.5 Flash at low reasoning effort; the trend and the memory contents are the evidence, not any single row.
- The offline mock is knowledge-gated and fixture-aware. It proves the memory mechanics deterministically; it is not model intelligence.
- Keep-or-revert compares against the previous run on a *different* data snapshot, so a 0.05 tolerance is used; a noisy snapshot can still promote a mediocre patch (visible as `kept` followed by a dip).
- Skills are one per family per server set; there is no skill library across families yet.
- The LLM judge for arbitrary tasks can be gamed by a confident answer; ground-truth graders are preferred whenever the task allows one.
