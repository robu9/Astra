"""Toy issue tracker with team conventions hidden in its data.

Hidden contextual logic:
  R1  Priority labels are sev-1 / sev-2 / sev-3. Their meaning is in list_labels descriptions:
      sev-1 = outage or data loss, sev-2 = major feature broken, sev-3 = everything else.
      Issue body keywords: "outage", "data loss", "cannot log in for all users" -> sev-1; "broken", "fails", "500" -> sev-2.
  R2  Component is the bracket prefix in the title, e.g. "[billing] ...". Each member `owns` components.
  R3  If any comment says "duplicate of #N", the issue gets label "duplicate" and is NOT assigned/prioritised.
API quirks:
  Q1  list_issues paginated (page size 5) via next_cursor; status filter must be lowercase ("open").
  Q2  assign_issue needs member_id (m_XX), not a name. add_label needs an existing label name, exact case.
  Q3  Comments are not embedded; call list_comments.
"""
from __future__ import annotations

import random

from astra.tools.base import ToolError, tool

COMPONENTS = ["billing", "auth", "search", "mobile", "exports"]
MEMBERS = [("m_01", "Nora Patel", ["billing", "exports"]), ("m_02", "Jae Kim", ["auth"]),
           ("m_03", "Omar Haddad", ["search"]), ("m_04", "Lena Fischer", ["mobile"])]
SEV1 = ["Production outage: {c} service down for all users", "Data loss after {c} migration",
        "All users cannot log in via {c}"]
SEV2 = ["{c} page returns 500 on save", "Export to CSV fails in {c}", "{c} sync is broken for enterprise tenants"]
SEV3 = ["Typo in {c} settings tooltip", "Improve {c} empty-state copy", "Minor alignment issue in {c} header",
        "Add dark-mode icon to {c} tab"]


def _sev(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("outage", "data loss", "all users cannot log in")):
        return "sev-1"
    if any(k in t for k in ("500", "fails", "broken")):
        return "sev-2"
    return "sev-3"


class Tracker:
    def __init__(self, seed: int = 0):
        rng = random.Random(2000 + seed)
        self.labels = {
            "sev-1": "Priority: production outage or data loss. Page on-call.",
            "sev-2": "Priority: major feature broken for many users.",
            "sev-3": "Priority: minor bug, cosmetic or copy.",
            "duplicate": "Closed as duplicate of an existing issue. Do not assign.",
            "needs-info": "Reporter must add details.",
        }
        self.members = {m[0]: {"id": m[0], "name": m[1], "owns": m[2]} for m in MEMBERS}
        self.issues, self.comments = {}, {}
        n = 9 + rng.randint(0, 3)
        for i in range(n):
            comp = rng.choice(COMPONENTS)
            bucket = rng.choices([SEV1, SEV2, SEV3], weights=[2, 3, 4])[0]
            title = f"[{comp}] " + rng.choice(bucket).format(c=comp)
            iid = f"{seed}{i + 1:02d}"
            dup = rng.random() < 0.2
            self.issues[iid] = {"id": iid, "title": title, "status": "open", "labels": [], "assignee": None,
                                "component": comp, "reporter": rng.choice(["ext", "support", "eng"])}
            self.comments[iid] = []
            if dup:
                self.comments[iid].append({"author": "eng", "text": f"duplicate of #{rng.randint(1, 80)}"})
            if rng.random() < 0.5:
                self.comments[iid].append({"author": "support", "text": rng.choice(["Customer escalated.", "Seen twice this week.",
                                                                                   "Repro steps attached."])})
        for _ in range(2):  # a couple already-triaged ones (closed) that must be ignored
            iid = f"{seed}{len(self.issues) + 1:02d}"
            comp = rng.choice(COMPONENTS)
            self.issues[iid] = {"id": iid, "title": f"[{comp}] old issue", "status": "closed", "labels": ["sev-3"],
                                "assignee": "m_01", "component": comp, "reporter": "eng"}
            self.comments[iid] = []

    def expected(self) -> dict[str, dict]:
        out = {}
        for iss in self.issues.values():
            if iss["status"] != "open":
                continue
            if any("duplicate of #" in c["text"].lower() for c in self.comments[iss["id"]]):
                out[iss["id"]] = {"labels": {"duplicate"}, "assignee": None}
                continue
            owner = next(m["id"] for m in self.members.values() if iss["component"] in m["owns"])
            out[iss["id"]] = {"labels": {_sev(iss["title"])}, "assignee": owner}
        return out

    @tool("List issues. Paginated via next_cursor. Optional status filter (open|closed).",
          {"type": "object", "properties": {"status": {"type": "string"}, "cursor": {"type": "string"}}, "required": []})
    def list_issues(self, status: str | None = None, cursor: str | None = None):
        if status is not None and status not in ("open", "closed"):
            raise ToolError(f"invalid status '{status}' (expected 'open' or 'closed')")
        items = [i for i in self.issues.values() if status is None or i["status"] == status]
        start = int(cursor.split(":")[1]) if cursor else 0
        page = items[start:start + 5]
        return {"items": [{k: v for k, v in i.items() if k != "component"} for i in page],
                "next_cursor": f"c:{start + 5}" if start + 5 < len(items) else None}

    @tool("Get one issue.")
    def get_issue(self, issue_id: str):
        if issue_id not in self.issues:
            raise ToolError(f"issue '{issue_id}' not found")
        return {k: v for k, v in self.issues[issue_id].items() if k != "component"}

    @tool("List comments on an issue.")
    def list_comments(self, issue_id: str):
        if issue_id not in self.comments:
            raise ToolError(f"issue '{issue_id}' not found")
        return {"issue_id": issue_id, "comments": self.comments[issue_id]}

    @tool("List available labels with their meaning.")
    def list_labels(self):
        return {"labels": [{"name": k, "description": v} for k, v in self.labels.items()]}

    @tool("List team members and the components they own.")
    def list_members(self):
        return {"members": list(self.members.values())}

    @tool("Add a label to an issue. Label must exist (see list_labels).")
    def add_label(self, issue_id: str, label: str):
        if issue_id not in self.issues:
            raise ToolError(f"issue '{issue_id}' not found")
        if label not in self.labels:
            raise ToolError(f"unknown label '{label}'; use list_labels")
        if label not in self.issues[issue_id]["labels"]:
            self.issues[issue_id]["labels"].append(label)
        return {"ok": True, "issue_id": issue_id, "labels": self.issues[issue_id]["labels"]}

    @tool("Assign an issue to a member by member_id (m_XX).")
    def assign_issue(self, issue_id: str, member_id: str):
        if issue_id not in self.issues:
            raise ToolError(f"issue '{issue_id}' not found")
        if member_id not in self.members:
            raise ToolError(f"unknown member_id '{member_id}'; expected an id like m_01 (see list_members)")
        self.issues[issue_id]["assignee"] = member_id
        return {"ok": True, "issue_id": issue_id, "assignee": member_id}
