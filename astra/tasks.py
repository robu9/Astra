"""Task families + graders. A task family is a general task the agent repeats over fresh data snapshots.

Graders return quality in [0,1] and feedback that names *what kind* of thing was missed. They never reveal the
hidden rule; the agent has to infer it from tool data during reflection. Ids change every run (new seed), so the only
thing that can carry over is understanding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from astra.fixtures import make_fixture


@dataclass
class TaskFamily:
    name: str
    fixture: str
    task: str
    answer_schema: dict
    grade: Callable  # (server, final) -> (quality, feedback, success)


def _norm(s) -> str:
    return str(s or "").strip().lower()


def grade_at_risk(server, final) -> tuple[float, str, bool]:
    exp = server.app.expected_at_risk()
    got = {}
    if isinstance(final, dict):
        for row in final.get("at_risk") or []:
            if isinstance(row, dict) and row.get("deal_id"):
                got[str(row["deal_id"])] = row
    if not exp:
        return (1.0 if not got else 0.5), "no at-risk deals this week", not got
    hit = set(got) & set(exp)
    missed = set(exp) - set(got)
    extra = set(got) - set(exp)
    precision = len(hit) / len(got) if got else 0.0
    recall = len(hit) / len(exp)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    owner_ok = sum(1 for d in hit if _norm(got[d].get("owner")) == _norm(exp[d]["owner"]))
    owner_acc = owner_ok / len(hit) if hit else 0.0
    quality = 0.75 * f1 + 0.25 * owner_acc
    fb = [f"found {len(hit)}/{len(exp)} at-risk deals", f"{len(extra)} false positives" if extra else "no false positives",
          f"owner correct on {owner_ok}/{len(hit)}"]
    if missed:
        fb.append(f"missed deal ids: {sorted(missed)} (inspect them: why are they at risk?)")
    if extra:
        fb.append(f"not actually at risk: {sorted(extra)}")
    return round(quality, 3), "; ".join(fb), quality >= 0.9


def grade_triage(server, final) -> tuple[float, str, bool]:
    exp = server.app.expected()
    state = server.app.issues
    total, ok_labels, ok_assign, extra_labels = 0, 0, 0, 0
    wrong = []
    for iid, e in exp.items():
        total += 1
        labels = set(state[iid]["labels"])
        if e["labels"] <= labels and not (labels - e["labels"] - {"needs-info"}):
            ok_labels += 1
        else:
            wrong.append(f"#{iid} labels={sorted(labels)}")
        if state[iid]["assignee"] == e["assignee"]:
            ok_assign += 1
        else:
            wrong.append(f"#{iid} assignee={state[iid]['assignee']}")
        extra_labels += len(labels - e["labels"] - {"needs-info"})
    if total == 0:
        return 1.0, "nothing to triage", True
    quality = 0.6 * ok_labels / total + 0.4 * ok_assign / total
    fb = [f"labels right on {ok_labels}/{total}", f"assignee right on {ok_assign}/{total}"]
    if wrong:
        fb.append("incorrect: " + ", ".join(wrong[:8]))
    return round(quality, 3), "; ".join(fb), quality >= 0.9


FAMILIES: dict[str, TaskFamily] = {
    "crm_at_risk": TaskFamily(
        name="crm_at_risk", fixture="crm",
        task=("Produce this week's at-risk pipeline report from the CRM. List every OPEN deal that is at risk, "
              "with the owner's name and a short reason. Use the CRM's own signals to decide what 'at risk' means; "
              "if a deal has no valid owner, put owner 'UNASSIGNED'."),
        answer_schema={"at_risk": [{"deal_id": "string", "owner": "string", "reason": "string"}]},
        grade=grade_at_risk,
    ),
    "tracker_triage": TaskFamily(
        name="tracker_triage", fixture="tracker",
        task=("Triage every OPEN issue in the tracker according to the team's conventions: apply the correct priority "
              "label and assign the right owner. Follow whatever conventions the tracker's own data implies. "
              "Then report what you did."),
        answer_schema={"triaged": [{"issue_id": "string", "labels": ["string"], "assignee": "string|null"}]},
        grade=grade_triage,
    ),
}


def fixture_for(family: TaskFamily, seed: int):
    return make_fixture(family.fixture, seed=seed)
