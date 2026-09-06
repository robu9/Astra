"""Reflector: turns a finished run into durable memory.

Input: trace + grade (quality, feedback). Output written to memory:
- semantic facts about the world inside the third-party tool (contextual logic)
- tool conventions (hidden required args, enum casing, pagination)
- a procedural skill (candidate until the next run confirms it)
- an actor prompt patch (candidate until confirmed)
- an episodic record

Keep-or-revert is enforced by the engine on the following run.
"""
from __future__ import annotations

import json

from astra.actor import RunTrace
from astra.gateway import ToolGateway
from astra.llm import parse_json
from astra.memory import Memory

REFLECT_SYSTEM = """You are Astra's reflection module. You read one run of a tool-using agent and extract what should be
remembered so the NEXT run on the same tools is more accurate, cheaper and faster.

Return ONE JSON object:
{
  "what_went_wrong": "<one sentence, or empty if nothing>",
  "facts": [ {"fact": "<durable fact about the data/rules inside these tools, generalisable beyond this run>",
              "server": "<server name or null if it is a general lesson>", "source_tool": "<server.tool or null>",
              "evidence": "<short quote from an observation>", "confidence": 0.0-1.0} ],
  "tool_conventions": [ {"tool": "<server.tool>", "note": "<calling convention learned: arg values, casing, pagination, hidden requirements>"} ],
  "skill": {"name": "<short>", "when": "<task pattern>", "steps": ["<step 1>", "..."], "tools": ["server.tool", "..."]} or null,
  "prompt_patch": "<2-5 lines of self-instruction for the actor on this task, or empty>",
  "bad_facts": ["<exact text of a previously used fact that turned out wrong>"]
}

Rules
- Facts must be about the third-party data or its business rules (e.g. which field encodes what, how statuses map,
  which records are exceptions), NOT about this specific run's answer. Never store the grader's expected answer verbatim.
  Never store facts about one specific record (a single id, one person's name); store the rule that produced it.
- "server" must be the server name whenever the fact is about that tool's data. Use null ONLY for lessons that would
  hold for any tool anywhere (e.g. "read every page before concluding").
- tool_conventions: always look for pagination (next_cursor / next_page / has_more fields and how to pass them back),
  enum values that were rejected vs accepted, id-vs-name arguments, and hidden required fields. Record each one.
- skill: ALWAYS propose the current best procedure when quality >= 0.5, even if imperfect. It is versioned; the next
  reflection refines it. Steps must be concrete tool calls in order, including "follow next_cursor until null" when the
  tool paginates. Return null only when the run was mostly wrong.
- Be concrete. Mention exact tool names, arg names and values.
- When the grader says specific records were wrong but nothing in the observed data distinguishes them, the explanation
  is almost certainly in a tool the actor did NOT call (details, comments, notes, history). Do NOT invent a rule from
  titles or ids; instead write a fact of the form "check <server.tool> for each record before deciding" and say so in
  prompt_patch. Only state a business rule when the trace contains the evidence for it.
- bad_facts: list every fact the actor was given that this run's evidence contradicts or that led to wrong actions.
  Removing a wrong fact is as valuable as adding a right one.
- If quality < 0.9 and TOOLS NEVER CALLED THIS RUN is non-empty, prompt_patch MUST name one of those tools and tell the
  actor to call it on every record next run. Never write hedges like "X is not always Y"; either state the rule with
  evidence or direct the actor to the tool that will reveal it.
"""


class Reflector:
    def __init__(self, llm, gateway: ToolGateway, memory: Memory):
        self.llm = llm
        self.gateway = gateway
        self.memory = memory

    def reflect(self, trace: RunTrace, family: str, quality: float, feedback: str, success: bool, model: str,
                allowed_tools: list[str]) -> dict:
        servers = self.gateway.attached()
        mem_used = self.memory.retrieve(trace.task, family, servers)
        used_tools = {s.tool for s in trace.steps if s.tool}
        unused = [t for t in allowed_tools if t not in used_tools]
        user = (
            f"TASK: {trace.task}\n\nGRADE: quality={quality:.2f} success={success}\nGRADER FEEDBACK: {feedback}\n\n"
            f"MEMORY THE ACTOR HAD:\nfacts={json.dumps([f['fact'] for f in mem_used['facts']])}\n"
            f"skill={json.dumps(mem_used['skills'][0] if mem_used['skills'] else None)}\n"
            f"prompt_patch={json.dumps(mem_used['prompt_patch'])}\n\n"
            f"TOOLS AVAILABLE: {', '.join(allowed_tools)}\n"
            f"TOOLS NEVER CALLED THIS RUN: {', '.join(unused) or 'none'}\n\nTRACE:\n{trace.compact(limit_obs=700)}\n\n"
            f"FINAL ANSWER: {json.dumps(trace.final, default=str)[:1500]}"
        )
        res = self.llm.chat(model, [{"role": "system", "content": REFLECT_SYSTEM}, {"role": "user", "content": user}],
                            max_tokens=6000)
        out = parse_json(res.text)
        if out.get("parse_error"):
            out["_raw"] = res.text[:2000]
        written = {"facts_new": 0, "conventions_new": 0, "skill": None, "prompt_patch": False, "facts_demoted": 0,
                   "cost_usd": res.cost_usd, "latency_s": res.latency_s, "model": model,
                   "tokens": res.prompt_tokens + res.completion_tokens}

        for f in out.get("facts") or []:
            if not isinstance(f, dict) or not f.get("fact"):
                continue
            server = f.get("server")
            if server not in servers:
                server = None
            if server is None:
                # a "general" fact that names an attached server's tool is really about that server
                hit = [s for s in servers if f"{s}." in f["fact"] or f" {s} " in f" {f['fact'].lower()} "]
                if hit:
                    server = hit[0]
            if self.memory.semantic.add(f["fact"], server, f.get("source_tool"), str(f.get("evidence", "")),
                                        trace.run_id, float(f.get("confidence", 0.6) or 0.6)):
                written["facts_new"] += 1
        for c in out.get("tool_conventions") or []:
            if isinstance(c, dict) and c.get("tool") in allowed_tools and c.get("note"):
                if self.memory.tool_model.add_convention(c["tool"], c["note"]):
                    written["conventions_new"] += 1
        for bad in out.get("bad_facts") or []:
            if not isinstance(bad, str) or not bad.strip():
                continue
            b = bad.strip().lower()
            for f in list(self.memory.semantic.facts):
                ft = f["fact"].strip().lower()
                # exact match, or the model quoted a substantial prefix/substring of the stored fact
                if ft == b or (len(b) > 30 and (b in ft or ft in b)):
                    self.memory.semantic.demote(f["fact"], f.get("server"), amount=0.4)
                    written["facts_demoted"] += 1
        skill = out.get("skill")
        if isinstance(skill, dict) and skill.get("steps") and quality >= 0.5:
            tools = [t for t in (skill.get("tools") or []) if t in allowed_tools]
            s = self.memory.skills.propose(str(skill.get("name", family))[:60], str(skill.get("when", trace.task))[:200],
                                           [str(x) for x in skill["steps"]][:10], tools, servers, trace.run_id, family)
            written["skill"] = {"name": s["name"], "status": s["status"], "version": s["version"]}
        patch = out.get("prompt_patch")
        if isinstance(patch, str) and patch.strip():
            self.memory.propose_prompt_patch(family, servers, patch, trace.run_id)
            written["prompt_patch"] = True

        self.memory.episodic.append({
            "run_id": trace.run_id, "family": family, "task": trace.task[:200], "servers": servers,
            "quality": round(quality, 3), "success": success, "tool_calls": sum(1 for s in trace.steps if s.tool),
            "tool_errors": sum(1 for s in trace.steps if s.tool and not s.ok), "cost_usd": round(trace.llm_cost_usd, 5),
            "what_went_wrong": str(out.get("what_went_wrong", ""))[:300], "mode": trace.budget.get("mode"),
        })
        written["what_went_wrong"] = str(out.get("what_went_wrong", ""))[:300]
        if out.get("parse_error"):
            written["parse_error"] = True
            written["raw"] = out.get("_raw", "")
        return written
