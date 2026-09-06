"""Cost and speed controller. Decides how much brain and how many steps a run gets.

Policy
- novel task / unknown tools / no prior success  -> strong model, generous step budget, plan first
- promoted skill + known tools + recent success   -> fast model, tight step budget, skill first
- reflection always uses the fast model unless the run failed badly
"""
from __future__ import annotations

from dataclasses import dataclass

from astra.memory import Memory


@dataclass
class Budget:
    model: str
    tier: str            # "fast" | "strong"
    max_steps: int
    max_tokens_reply: int
    mode: str            # "novel" | "skilled" | "recovering"
    reason: str
    max_cost_usd: float = 0.25


class Controller:
    def __init__(self, memory: Memory, models: dict[str, str]):
        self.memory = memory
        self.models = models

    def plan_run(self, task: str, family: str, servers: list[str], tool_names: list[str]) -> Budget:
        skills = [s for s in self.memory.skills.retrieve(task, servers, family) if s["status"] == "promoted"]
        eps = self.memory.episodic.recent(family, servers, n=3)
        known_ratio = (sum(1 for q in tool_names if self.memory.tool_model.known(q)) / len(tool_names)) if tool_names else 0
        last_ok = bool(eps) and eps[-1].get("success") is True
        last_bad = bool(eps) and eps[-1].get("quality", 0) < 0.4

        # Step budgets scale with how many tool calls the last successful run actually needed.
        base = max(20, int(1.5 * max((e.get("tool_calls", 0) for e in eps), default=0)) + 6)
        if skills and known_ratio >= 0.5 and last_ok:
            return Budget(self.models["fast"], "fast", max_steps=base, max_tokens_reply=3000, mode="skilled",
                          reason=f"promoted skill v{skills[0].get('version', 1)}, {known_ratio:.0%} tools known, last run succeeded")
        if eps and last_bad:
            return Budget(self.models["strong"], "strong", max_steps=base + 10, max_tokens_reply=5000, mode="recovering",
                          reason="last run scored poorly; escalating model and step budget")
        if not eps:
            return Budget(self.models["strong"], "strong", max_steps=30, max_tokens_reply=5000, mode="novel",
                          reason="no prior episodes for this task on these tools")
        return Budget(self.models["fast"] if known_ratio >= 0.5 else self.models["strong"],
                      "fast" if known_ratio >= 0.5 else "strong", max_steps=base + 4, max_tokens_reply=4000,
                      mode="novel" if known_ratio < 0.5 else "skilled",
                      reason=f"{known_ratio:.0%} tools known, no promoted skill yet")

    def reflection_model(self, quality: float) -> str:
        return self.models["strong"] if quality < 0.3 else self.models["fast"]
