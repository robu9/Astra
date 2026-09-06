"""LLM judge for tasks that ship no grader (arbitrary MCP + arbitrary task).

Returns quality in [0,1], feedback, success. Uses the fast model. The judge only sees task, final answer and a compact
trace; it does not see hidden ground truth (there is none for user tasks).
"""
from __future__ import annotations

import json

from astra.llm import parse_json

JUDGE_SYSTEM = """You grade one run of a tool-using agent. Return ONE JSON object:
{"quality": 0.0-1.0, "feedback": "<what was wrong or missing, concretely>", "success": true|false}
Criteria: did the final answer fully satisfy the task; were tool results actually used (no fabrication);
were there wasted or erroring calls; is the answer well-formed. success = quality >= 0.85."""


def llm_grade(llm, model: str, task: str, final, trace_text: str) -> tuple[float, str, bool]:
    user = f"TASK: {task}\n\nFINAL ANSWER: {json.dumps(final, default=str)[:3000]}\n\nTRACE:\n{trace_text[:6000]}"
    res = llm.chat(model, [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}], max_tokens=400)
    out = parse_json(res.text)
    try:
        q = float(out.get("quality", 0.0))
    except (TypeError, ValueError):
        q = 0.0
    q = max(0.0, min(1.0, q))
    success_raw = out.get("success", q >= 0.85)
    success = success_raw if isinstance(success_raw, bool) else q >= 0.85
    return q, str(out.get("feedback", ""))[:500], success
