"""Toy CRM with quirks a real SaaS API would have.

Hidden contextual logic (never stated in schemas; must be learned from data):
  R1  `health` is a cryptic code: "G" fine, "A" amber, "R" red. Red = at risk.
  R2  Notes containing "PO pending" or "budget freeze" mean the deal is at risk even if health is green.
  R3  Owners with active=false left the company; their deals are at risk ("unowned") and owner should be "UNASSIGNED".
  R4  Deals in closed_won / closed_lost are not open and never appear in the report.
API quirks:
  Q1  list_deals is paginated (page size 4) via next_cursor; stage filter must be exact lowercase enum.
  Q2  get_notes requires deal_id; notes are not embedded in the deal object.
  Q3  owner is an id (u_XX); names come from list_owners.
"""
from __future__ import annotations

import random

from astra.tools.base import ToolError, tool

STAGES = ["qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
OPEN = {"qualified", "proposal", "negotiation"}
COMPANIES = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Stark", "Wayne", "Wonka", "Tyrell",
             "Cyberdyne", "Soylent", "Dunder", "Pied Piper", "Massive", "Oscorp", "Gringotts", "Sirius", "Nakatomi", "Zorg"]
NAMES = ["Priya Rao", "Dan Alvarez", "Mei Chen", "Tom Okafor", "Sara Klein", "Ravi Nair", "Ana Souza", "Liam Byrne"]
NEUTRAL_NOTES = ["Intro call done", "Sent pricing deck", "Demo scheduled next week", "Legal reviewing MSA",
                 "Champion is VP Ops", "Follow-up on security questionnaire", "Renewal discussion started"]
RISK_NOTES = ["PO pending with finance, no ETA", "Budget freeze announced until next quarter",
              "Waiting on PO pending approval", "Customer mentioned a budget freeze"]


class CRM:
    def __init__(self, seed: int = 0):
        rng = random.Random(1000 + seed)
        self.owners = {}
        for i, n in enumerate(NAMES):
            self.owners[f"u_{i + 1:02d}"] = {"id": f"u_{i + 1:02d}", "name": n, "active": True}
        inactive = rng.sample(list(self.owners), 2)
        for u in inactive:
            self.owners[u]["active"] = False
        self.deals, self.notes = {}, {}
        n_deals = 14 + rng.randint(0, 4)
        comps = rng.sample(COMPANIES, n_deals)
        for i, comp in enumerate(comps):
            did = f"d_{seed:02d}{i + 1:02d}"
            stage = rng.choice(STAGES) if rng.random() < 0.8 else rng.choice(["closed_won", "closed_lost"])
            health = rng.choices(["G", "A", "R"], weights=[6, 2, 2])[0]
            owner = rng.choice(list(self.owners))
            self.deals[did] = {"id": did, "company": comp, "stage": stage, "health": health, "owner": owner,
                               "amount_cents": rng.randint(5, 400) * 1000_00, "currency": rng.choice(["USD", "USD", "EUR"])}
            notes = [rng.choice(NEUTRAL_NOTES) for _ in range(rng.randint(1, 3))]
            if rng.random() < 0.3:
                notes.insert(rng.randint(0, len(notes)), rng.choice(RISK_NOTES))
            self.notes[did] = [{"id": f"n_{did}_{k}", "text": t, "author": rng.choice(list(self.owners))}
                               for k, t in enumerate(notes)]

    # ---- ground truth for graders ---------------------------------------------------------------
    def expected_at_risk(self) -> dict[str, dict]:
        out = {}
        for d in self.deals.values():
            if d["stage"] not in OPEN:
                continue
            reasons = []
            if d["health"] == "R":
                reasons.append("health_red")
            if any(("po pending" in n["text"].lower() or "budget freeze" in n["text"].lower()) for n in self.notes[d["id"]]):
                reasons.append("blocking_note")
            if not self.owners[d["owner"]]["active"]:
                reasons.append("owner_inactive")
            if reasons:
                owner = "UNASSIGNED" if "owner_inactive" in reasons else self.owners[d["owner"]]["name"]
                out[d["id"]] = {"owner": owner, "reasons": reasons}
        return out

    # ---- tools ------------------------------------------------------------------------------------------
    @tool("List deals. Paginated: pass the returned next_cursor to get the next page. Optional stage filter.",
          {"type": "object", "properties": {"stage": {"type": "string"}, "cursor": {"type": "string"},
                                            "limit": {"type": "integer"}}, "required": []})
    def list_deals(self, stage: str | None = None, cursor: str | None = None, limit: int = 4):
        if stage is not None and stage not in STAGES:
            raise ToolError(f"invalid stage '{stage}'")
        limit = max(1, min(int(limit or 4), 4))
        items = [d for d in self.deals.values() if stage is None or d["stage"] == stage]
        start = 0
        if cursor:
            try:
                start = int(cursor.split(":")[1])
            except (IndexError, ValueError):
                raise ToolError(f"bad cursor '{cursor}'")
        page = items[start:start + limit]
        nxt = f"c:{start + limit}" if start + limit < len(items) else None
        return {"items": [{k: v for k, v in d.items() if k != "amount_cents"} | {"amount": d["amount_cents"] / 100}
                          for d in page], "next_cursor": nxt, "total": len(items)}

    @tool("Get one deal by id.")
    def get_deal(self, deal_id: str):
        if deal_id not in self.deals:
            raise ToolError(f"deal '{deal_id}' not found")
        return self.deals[deal_id]

    @tool("Get notes for a deal. Notes are free text written by sales reps.")
    def get_notes(self, deal_id: str):
        if deal_id not in self.notes:
            raise ToolError(f"deal '{deal_id}' not found")
        return {"deal_id": deal_id, "notes": self.notes[deal_id]}

    @tool("List deal owners (sales reps).")
    def list_owners(self):
        return {"owners": list(self.owners.values())}

    @tool("Get one owner by id.")
    def get_owner(self, owner_id: str):
        if owner_id not in self.owners:
            raise ToolError(f"owner '{owner_id}' not found")
        return self.owners[owner_id]
