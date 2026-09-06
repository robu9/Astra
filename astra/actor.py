"""Actor: does the task using tools + retrieved memory. No app-specific logic lives here."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from astra.controller import Budget
from astra.gateway import ToolGateway
from astra.llm import parse_json
from astra.memory import Memory

SYSTEM = """You are Astra, a careful tool-using agent. You complete a task by calling tools, then return a final answer.

Reply with ONE JSON object per turn, nothing else. Two forms:
  {"thought": "<short reasoning>", "tool": "<server.tool>", "args": {...}}
  {"thought": "<short reasoning>", "final": <answer matching the ANSWER SCHEMA>}

Rules
- Use only the tools listed. Argument names must match the schema exactly.
- Prefer the LEARNED notes, FACTS and SKILL below over guessing; they came from your own earlier runs on these tools.
- Do not re-probe tools whose behaviour you already know. Do not repeat identical calls.
- When a tool errors, read the error and fix the call; do not loop.
- Finish as soon as you have enough evidence. Fewer calls is better.
"""


@dataclass
class Step:
    idx: int
    thought: str
    tool: str | None
    args: dict | None
    observation: str | None
    ok: bool
    latency_s: float
    model: str
    cost_usd: float
    tokens: int
    executed: bool = True


@dataclass
class RunTrace:
    run_id: str
    task: str
    steps: list[Step] = field(default_factory=list)
    final: object = None
    finished: bool = False
    stop_reason: str = ""
    llm_cost_usd: float = 0.0
    llm_latency_s: float = 0.0
    tokens: int = 0
    memory_used: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "task": self.task, "finished": self.finished, "stop_reason": self.stop_reason,
            "final": self.final, "llm_cost_usd": round(self.llm_cost_usd, 6), "llm_latency_s": round(self.llm_latency_s, 3),
            "tokens": self.tokens, "budget": self.budget, "memory_used": self.memory_used,
            "steps": [s.__dict__ for s in self.steps],
        }

    def compact(self, limit_obs: int = 300) -> str:
        lines = []
        for s in self.steps:
            if s.tool:
                obs = (s.observation or "")[:limit_obs]
                status = "BLOCKED" if not s.executed else ("OK" if s.ok else "ERROR")
                lines.append(f"[{s.idx}] {s.tool}({json.dumps(s.args, default=str)}) -> {status}: {obs}")
            else:
                lines.append(f"[{s.idx}] FINAL: {json.dumps(s.args or self.final, default=str)[:limit_obs]}")
        return "\n".join(lines)


FINAL_KEYS = ("final", "final_answer", "answer", "result", "output", "response")
TOOL_KEYS = ("tool", "tool_name", "name", "action", "function", "call")
ARG_KEYS = ("args", "arguments", "parameters", "params", "input", "inputs")


def normalize_reply(reply: dict) -> dict:
    """Coerce the many JSON shapes models emit into {"thought", "tool", "args"} or {"thought", "final"}."""
    if not isinstance(reply, dict):
        return {"thought": str(reply)[:200], "final": reply}
    out = {"thought": str(reply.get("thought") or reply.get("reasoning") or "")[:400]}
    if reply.get("parse_error"):
        out["parse_error"] = True
    # nested tool-call containers: {"tool_call": {...}} / {"tool_calls": [{...}]} / {"action": {"tool":..,"args":..}}
    inner = reply.get("tool_call") or reply.get("action") if isinstance(reply.get("tool_call") or reply.get("action"), dict) else None
    if inner is None and isinstance(reply.get("tool_calls"), list) and reply["tool_calls"]:
        inner = reply["tool_calls"][0]
        if isinstance(inner, dict) and isinstance(inner.get("function"), dict):
            inner = {"tool": inner["function"].get("name"), "args": inner["function"].get("arguments")}
    src = inner if isinstance(inner, dict) else reply
    tool = next((src[k] for k in TOOL_KEYS if isinstance(src.get(k), str) and src[k]), None)
    args = next((src[k] for k in ARG_KEYS if k in src), None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    for k in FINAL_KEYS:
        if k in reply and reply[k] is not None and not tool:
            out["final"] = reply[k]
            return out
    if tool:
        out["tool"] = tool
        out["args"] = args if isinstance(args, dict) else {}
    return out


def _memory_block(mem: dict) -> str:
    out = []
    if mem.get("prompt_patch"):
        out.append("SELF-INSTRUCTIONS (from your reflection on earlier runs):\n" + mem["prompt_patch"])
    if mem.get("skills"):
        s = mem["skills"][0]
        out.append(f"SKILL '{s['name']}' (status: {s['status']}, v{s.get('version', 1)}) - when: {s['when']}\n  steps:\n  - "
                   + "\n  - ".join(s["steps"]) + f"\n  tools: {', '.join(s['tools'])}")
    if mem.get("facts"):
        out.append("FACTS learned from these tools' data:\n" + "\n".join(
            f"- ({f['confidence']:.1f}) {f['fact']}" for f in mem["facts"]))
    if mem.get("episodes"):
        eps = mem["episodes"]
        out.append("RECENT RUNS on this task:\n" + "\n".join(
            f"- run {e['run_id']}: quality={e.get('quality', 0):.2f}, calls={e.get('tool_calls')}, issue: {e.get('what_went_wrong') or 'none'}"
            for e in eps))
    return "\n\n".join(out) if out else "(no memory yet - this is the first time you see this task on these tools)"


class Actor:
    def __init__(self, llm, gateway: ToolGateway, memory: Memory):
        self.llm = llm
        self.gateway = gateway
        self.memory = memory

    def run(self, run_id: str, task: str, family: str, answer_schema: dict, budget: Budget,
            on_step=None) -> RunTrace:
        servers = self.gateway.attached()
        mem = self.memory.retrieve(task, family, servers)
        trace = RunTrace(run_id=run_id, task=task, budget=budget.__dict__.copy())
        trace.memory_used = {"facts": len(mem["facts"]), "skills": len(mem["skills"]),
                             "episodes": len(mem["episodes"]), "prompt_patch": bool(mem["prompt_patch"])}
        self.gateway.new_run()

        system = SYSTEM + f"\nMODE: {budget.mode} ({budget.reason}). Step budget: {budget.max_steps}.\n"
        explore = ""
        never_used = [q for q in self.gateway.specs if self.memory.tool_model.get(q)["calls"] == 0]
        failed_before = any(e.get("quality", 1.0) < 0.9 for e in mem["episodes"])
        if never_used and failed_before:
            explore = ("\n\nEXPLORE: earlier runs on this task were marked partly wrong and these tools have NEVER been called: "
                       + ", ".join(never_used) + ". Information that explains the wrong records is probably in one of them. "
                       "Call at least one on the records you are unsure about before deciding.")
        user = (f"TASK:\n{task}\n\nANSWER SCHEMA (return exactly this shape in 'final'):\n"
                f"{json.dumps(answer_schema, indent=1)}\n\nTOOLS:\n{self.gateway.tool_prompt()}\n\n"
                f"MEMORY:\n{_memory_block(mem)}{explore}")
        messages = [{"system_hidden": True, "role": "system", "content": system},
                    {"role": "user", "content": user}]
        messages = [{k: v for k, v in m.items() if k != "system_hidden"} for m in messages]

        malformed = 0
        attempted_calls: dict[str, tuple[int, bool, bool]] = {}
        repeated_calls = 0
        for idx in range(1, budget.max_steps + 1):
            res = self.llm.chat(budget.model, messages, max_tokens=budget.max_tokens_reply)
            trace.llm_cost_usd += res.cost_usd
            trace.llm_latency_s += res.latency_s
            trace.tokens += res.prompt_tokens + res.completion_tokens
            reply = normalize_reply(parse_json(res.text))
            thought = str(reply.get("thought", ""))[:400]
            messages.append({"role": "assistant", "content": res.text if len(res.text) < 4000 else json.dumps(reply)})

            # A provider can overshoot the ceiling in a single response, but once
            # crossed Astra must not spend on another call or touch another tool.
            if trace.llm_cost_usd >= budget.max_cost_usd and "final" not in reply:
                trace.stop_reason = "cost_budget_exhausted"
                break

            if "final" in reply and reply["final"] is not None:
                trace.final = reply["final"]
                trace.finished = True
                trace.stop_reason = "final"
                trace.steps.append(Step(idx, thought, None, reply["final"] if isinstance(reply["final"], dict) else {"value": reply["final"]},
                                        None, True, res.latency_s, res.model, res.cost_usd,
                                        res.prompt_tokens + res.completion_tokens))
                if on_step:
                    on_step(trace.steps[-1])
                break

            tool = reply.get("tool")
            args = reply.get("args") or {}
            if not tool:
                malformed += 1
                obs = ('Malformed reply. Use exactly one of: {"thought": "...", "tool": "server.tool", "args": {...}} '
                       'or {"thought": "...", "final": <answer>}. Reply now.')
                trace.steps.append(Step(idx, thought, None, None, f"{obs}\n(raw reply head: {res.text[:300]!r})", False,
                                        res.latency_s, res.model, res.cost_usd, res.prompt_tokens + res.completion_tokens))
                messages.append({"role": "user", "content": obs})
                if malformed >= 2:
                    trace.stop_reason = "malformed_replies"
                    break
                continue
            malformed = 0
            if not isinstance(args, dict):
                args = {}
            signature = hashlib.sha1(
                f"{tool}|{json.dumps(args, sort_keys=True, default=str)}".encode()
            ).hexdigest()
            previous = attempted_calls.get(signature)
            # Successful reads may repeat safely through the gateway's in-run cache.
            # Failed calls and writes are never executed twice with identical args.
            if previous and (not previous[1] or not previous[2]):
                repeated_calls += 1
                obs = (f"ERROR: blocked repeated identical call to {tool}; it was already attempted at step "
                       f"{previous[0]}. Change the arguments or return a final answer.")
                trace.steps.append(Step(idx, thought, tool, args, obs, False, res.latency_s, res.model,
                                        res.cost_usd, res.prompt_tokens + res.completion_tokens, executed=False))
                if on_step:
                    on_step(trace.steps[-1])
                messages.append({"role": "user", "content": obs})
                if repeated_calls >= 2:
                    trace.stop_reason = "repeated_tool_calls"
                    break
                continue
            rec = self.gateway.call(tool, args)
            tool_name = tool.split(".", 1)[-1].lower()
            is_read = tool_name.startswith(("list", "get", "search", "find", "read", "fetch", "describe", "query", "show"))
            attempted_calls[signature] = (idx, rec.ok, is_read)
            obs = rec.result_preview if rec.ok else f"ERROR: {rec.error}"
            if trace.llm_cost_usd > budget.max_cost_usd:
                obs += "\n(cost budget nearly exhausted: give your best 'final' answer now)"
            trace.steps.append(Step(idx, thought, tool, args, obs, rec.ok, res.latency_s + rec.latency_s, res.model,
                                    res.cost_usd, res.prompt_tokens + res.completion_tokens))
            if on_step:
                on_step(trace.steps[-1])
            messages.append({"role": "user", "content": f"OBSERVATION from {tool}:\n{obs}"})
        else:
            trace.stop_reason = "step_budget_exhausted"
        if not trace.finished and trace.stop_reason != "cost_budget_exhausted":
            # One last chance to answer from what it has, with a generous token cap.
            messages.append({"role": "user", "content": "Stop calling tools. Return your best 'final' now as one JSON object."})
            res = self.llm.chat(budget.model, messages, max_tokens=max(budget.max_tokens_reply, 8000))
            trace.llm_cost_usd += res.cost_usd
            trace.llm_latency_s += res.latency_s
            trace.tokens += res.prompt_tokens + res.completion_tokens
            reply = normalize_reply(parse_json(res.text))
            if reply.get("final") is not None:
                trace.final = reply["final"]
                trace.finished = True
            elif not reply.get("parse_error"):
                trace.final = reply  # last resort: whatever JSON it produced is the answer
                trace.finished = True
        return trace
