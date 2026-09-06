"""Offline scripted model. Exists so the loop can be exercised with no API key.

It is deliberately *knowledge-gated*: it only applies a rule if that rule is present in the MEMORY block of the
prompt the actor built. Without memory it behaves like a naive agent (wrong enum casing, no pagination, ignores
notes). With memory it follows the learned facts. Reflection likewise only emits facts about things that actually
appear in the trace. So the mock demonstrates the mechanics of the learning loop, not model intelligence.
Use a real model (ASTRA_LLM_API_KEY) for the demo.
"""
from __future__ import annotations

import json
import re


def _section(text: str, start: str, end: str | None = None) -> str:
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start)) if end else -1
    return text[i + len(start): j if j != -1 else None]


def _observations(messages: list[dict]) -> list[tuple[str, str]]:
    out = []
    for m in messages:
        if m["role"] == "user" and m["content"].startswith("OBSERVATION from "):
            head, _, body = m["content"].partition(":\n")
            out.append((head.replace("OBSERVATION from ", ""), body))
    return out


def scripted_policy(model: str, messages: list[dict]) -> str:
    system = messages[0]["content"]
    if "reflection module" in system:
        return _reflect(messages[-1]["content"])
    if "You grade one run" in system:
        return json.dumps({"quality": 0.7, "feedback": "mock judge", "success": False})
    first = messages[1]["content"]
    tools = _section(first, "TOOLS:\n", "\n\nMEMORY:")
    memory = _section(first, "MEMORY:\n") + "\n" + tools  # learned tool hints live in the TOOLS block
    obs = _observations(messages)
    if "crm.list_deals" in tools:
        return _crm(memory, obs)
    if "tracker.list_issues" in tools:
        return _tracker(memory, obs)
    return json.dumps({"thought": "no idea", "final": {}})


# ---------------------------------------------------------------- CRM ------------------------------------------------
def _crm(memory: str, obs: list[tuple[str, str]]) -> str:
    mem = memory.lower()
    knows_enum = "lowercase" in mem or "exact" in mem or "invalid stage" in mem
    knows_pages = "next_cursor" in mem or "paginat" in mem
    knows_notes = "po pending" in mem or "budget freeze" in mem
    knows_owner = "active" in mem and ("false" in mem or "inactive" in mem or "left" in mem)
    knows_health = '"r"' in mem or "health" in mem and "red" in mem

    deals, owners, notes = {}, {}, {}
    cursor_seen, last_cursor, errors = set(), None, 0
    for tool, body in obs:
        if body.startswith("ERROR"):
            errors += 1
            continue
        try:
            data = json.loads(body.split("... (+")[0]) if body.startswith("{") and not body.endswith("chars)") else json.loads(body)
        except json.JSONDecodeError:
            continue
        if tool == "crm.list_deals":
            for d in data.get("items", []):
                deals[d["id"]] = d
            last_cursor = data.get("next_cursor")
            if last_cursor:
                cursor_seen.add(last_cursor)
        elif tool == "crm.list_owners":
            owners = {o["id"]: o for o in data.get("owners", [])}
        elif tool == "crm.get_notes":
            notes[data["deal_id"]] = data.get("notes", [])

    step = len(obs)
    if step == 0:
        if knows_enum:
            return json.dumps({"thought": "list open deals page 1", "tool": "crm.list_deals", "args": {}})
        return json.dumps({"thought": "filter to open deals", "tool": "crm.list_deals", "args": {"stage": "Open"}})
    if not deals and errors:
        return json.dumps({"thought": "stage filter failed, list all", "tool": "crm.list_deals", "args": {}})
    if knows_pages and last_cursor:
        return json.dumps({"thought": "next page", "tool": "crm.list_deals", "args": {"cursor": last_cursor}})
    if not owners and (knows_owner or True):
        return json.dumps({"thought": "resolve owner names", "tool": "crm.list_owners", "args": {}})
    open_deals = [d for d in deals.values() if d["stage"] in ("qualified", "proposal", "negotiation")]
    if knows_notes:
        for d in open_deals:
            if d["id"] not in notes:
                return json.dumps({"thought": "check notes for blockers", "tool": "crm.get_notes", "args": {"deal_id": d["id"]}})
    rows = []
    for d in open_deals:
        reasons = []
        if d["health"] == "R" and (knows_health or True):
            reasons.append("health red")
        if knows_notes and any(("po pending" in n["text"].lower() or "budget freeze" in n["text"].lower())
                               for n in notes.get(d["id"], [])):
            reasons.append("blocking note")
        o = owners.get(d["owner"], {})
        inactive = knows_owner and o and not o.get("active", True)
        if inactive:
            reasons.append("owner inactive")
        if reasons:
            owner = "UNASSIGNED" if inactive else (o.get("name") or d["owner"])
            rows.append({"deal_id": d["id"], "owner": owner, "reason": ", ".join(reasons)})
    return json.dumps({"thought": "compile report", "final": {"at_risk": rows}})


# ------------------------------------------------------------- tracker ------------------------------------------------
def _tracker(memory: str, obs: list[tuple[str, str]]) -> str:
    mem = memory.lower()
    knows_status = "lowercase" in mem or "invalid status" in mem or "'open'" in mem
    knows_pages = "next_cursor" in mem or "paginat" in mem
    knows_dup = "duplicate of" in mem or "list_comments on each issue" in mem
    knows_labels = "sev-1" in mem
    knows_members = "member_id" in mem or "m_0" in mem
    knows_component = "bracket" in mem or "prefix" in mem or "[component]" in mem or "owns" in mem

    issues, labels, members, comments = {}, [], {}, {}
    last_cursor, errors, writes = None, 0, set()
    for tool, body in obs:
        if body.startswith("ERROR"):
            errors += 1
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if tool == "tracker.list_issues":
            for i in data.get("items", []):
                issues[i["id"]] = i
            last_cursor = data.get("next_cursor")
        elif tool == "tracker.list_labels":
            labels = [l["name"] for l in data.get("labels", [])]
        elif tool == "tracker.list_members":
            members = {m["id"]: m for m in data.get("members", [])}
        elif tool == "tracker.list_comments":
            comments[data["issue_id"]] = data.get("comments", [])
        elif tool in ("tracker.add_label", "tracker.assign_issue"):
            writes.add((tool, data.get("issue_id"), data.get("labels", [None])[-1] if tool == "tracker.add_label" else data.get("assignee")))

    if not obs:
        return json.dumps({"thought": "list open issues", "tool": "tracker.list_issues",
                           "args": {"status": "open" if knows_status else "Open"}})
    if not issues and errors:
        return json.dumps({"thought": "retry lowercase", "tool": "tracker.list_issues", "args": {"status": "open"}})
    if knows_pages and last_cursor:
        return json.dumps({"thought": "next page", "tool": "tracker.list_issues", "args": {"cursor": last_cursor}})
    if not labels:
        return json.dumps({"thought": "learn label meanings", "tool": "tracker.list_labels", "args": {}})
    if not members:
        return json.dumps({"thought": "who owns what", "tool": "tracker.list_members", "args": {}})
    open_issues = [i for i in issues.values() if i["status"] == "open"]
    if knows_dup:
        for i in open_issues:
            if i["id"] not in comments:
                return json.dumps({"thought": "check for duplicate markers", "tool": "tracker.list_comments",
                                   "args": {"issue_id": i["id"]}})

    def sev(title: str) -> str:
        t = title.lower()
        if knows_labels and any(k in t for k in ("outage", "data loss", "all users cannot log in")):
            return "sev-1"
        if knows_labels and any(k in t for k in ("500", "fails", "broken")):
            return "sev-2"
        return "sev-3" if knows_labels else "P2"

    def owner(title: str):
        m = re.match(r"\[(\w+)\]", title)
        comp = m.group(1) if m else None
        if knows_component and comp:
            for mid, mm in members.items():
                if comp in mm.get("owns", []):
                    return mid
        return "m_01" if knows_members else next(iter(members.values()))["name"]

    report = []
    for i in open_issues:
        dup = knows_dup and any("duplicate of #" in c["text"].lower() for c in comments.get(i["id"], []))
        want_label = "duplicate" if dup else sev(i["title"])
        if ("tracker.add_label", i["id"], want_label) not in writes and want_label not in i["labels"]:
            return json.dumps({"thought": f"label {i['id']}", "tool": "tracker.add_label",
                               "args": {"issue_id": i["id"], "label": want_label}})
        if not dup:
            who = owner(i["title"])
            if ("tracker.assign_issue", i["id"], who) not in writes:
                return json.dumps({"thought": f"assign {i['id']}", "tool": "tracker.assign_issue",
                                   "args": {"issue_id": i["id"], "member_id": who}})
        report.append({"issue_id": i["id"], "labels": [want_label], "assignee": None if dup else owner(i["title"])})
    return json.dumps({"thought": "done", "final": {"triaged": report}})


# ------------------------------------------------------------- reflection ------------------------------------------------
def _reflect(user: str) -> str:
    trace = _section(user, "TRACE:\n", "\n\nFINAL ANSWER")
    grade = _section(user, "GRADE:", "\n")
    quality = float(re.search(r"quality=([0-9.]+)", grade).group(1)) if "quality=" in grade else 0.0
    facts, conv, wrong = [], [], ""
    low = trace.lower()
    crm = "crm." in trace
    server = "crm" if crm else "tracker"
    if "invalid stage" in low:
        conv.append({"tool": "crm.list_deals", "note": "stage must be exact lowercase enum (qualified|proposal|negotiation|closed_won|closed_lost); omit to get all"})
        wrong = "wasted a call on a wrongly-cased stage filter"
    if "invalid status" in low:
        conv.append({"tool": "tracker.list_issues", "note": "status must be lowercase 'open' or 'closed'"})
        wrong = "wasted a call on a wrongly-cased status filter"
    if "next_cursor\": \"c:" in trace or "next_cursor\": \"c:" in low:
        conv.append({"tool": f"{server}.list_{'deals' if crm else 'issues'}", "note": "paginated: keep calling with cursor=next_cursor until it is null; page size is small"})
        if quality < 0.9 and "cursor" not in low.split("next_cursor")[0]:
            wrong = wrong or "only read the first page of results"
    if crm:
        if '"health": "r"' in low:
            facts.append({"fact": "Deal field health is a colour code: G green, A amber, R red; R means the deal is at risk", "server": "crm",
                          "source_tool": "crm.list_deals", "evidence": '"health": "R"', "confidence": 0.8})
        if "po pending" in low or "budget freeze" in low:
            facts.append({"fact": "Notes mentioning 'PO pending' or 'budget freeze' mean the deal is at risk even when health is green; fetch notes with crm.get_notes for every open deal",
                          "server": "crm", "source_tool": "crm.get_notes", "evidence": "PO pending", "confidence": 0.8})
        elif quality < 0.9 and "missed deal ids" in user:
            facts.append({"fact": "At-risk signals are not all in the deal object; some live in free-text notes, so read crm.get_notes for each open deal",
                          "server": "crm", "source_tool": "crm.get_notes", "evidence": "missed deals had green health", "confidence": 0.6})
        if '"active": false' in low:
            facts.append({"fact": "Owners with active=false have left; their open deals are at risk and owner must be reported as UNASSIGNED",
                          "server": "crm", "source_tool": "crm.list_owners", "evidence": '"active": false', "confidence": 0.8})
        conv.append({"tool": "crm.list_owners", "note": "owner on a deal is an id like u_03; map to name via list_owners"})
    else:
        if "sev-1" in low:
            facts.append({"fact": "Priority labels: sev-1 = outage or data loss, sev-2 = major feature broken (500/fails/broken), sev-3 = minor/cosmetic",
                          "server": "tracker", "source_tool": "tracker.list_labels", "evidence": "sev-1: production outage", "confidence": 0.85})
        if '"owns"' in low:
            facts.append({"fact": "Component is the [bracket] prefix of the issue title; assign to the member whose owns list contains that component",
                          "server": "tracker", "source_tool": "tracker.list_members", "evidence": '"owns": ["billing"', "confidence": 0.85})
        if "duplicate of #" in low:
            facts.append({"fact": "If a comment says 'duplicate of #N', label the issue 'duplicate' and do not assign or prioritise it",
                          "server": "tracker", "source_tool": "tracker.list_comments", "evidence": "duplicate of #", "confidence": 0.85})
        elif quality < 0.9 and "incorrect" in user:
            facts.append({"fact": "Some open issues should not be triaged normally; check tracker.list_comments on each issue for special markers",
                          "server": "tracker", "source_tool": "tracker.list_comments", "evidence": "labels wrong on issues with comments", "confidence": 0.6})
        if "unknown member_id" in low:
            conv.append({"tool": "tracker.assign_issue", "note": "member_id must be an id like m_01 from list_members, not a name"})
        if "unknown label" in low:
            conv.append({"tool": "tracker.add_label", "note": "label must exactly match a name from list_labels"})
    skill = None
    if quality >= 0.5:
        if crm:
            skill = {"name": "crm at-risk report", "when": "weekly at-risk pipeline report from CRM",
                     "steps": ["list_deals with no filter and follow next_cursor until null", "list_owners once",
                               "get_notes for each open deal", "flag health R, blocking notes, inactive owners", "report"],
                     "tools": ["crm.list_deals", "crm.list_owners", "crm.get_notes"]}
        else:
            skill = {"name": "tracker triage", "when": "triage open issues per team conventions",
                     "steps": ["list_issues status=open, follow next_cursor", "list_labels and list_members once",
                               "list_comments per issue; duplicates get 'duplicate' only", "add_label by severity keywords",
                               "assign_issue by [component] owner"],
                     "tools": ["tracker.list_issues", "tracker.list_labels", "tracker.list_members", "tracker.list_comments",
                               "tracker.add_label", "tracker.assign_issue"]}
    patch = "" if quality >= 0.95 else ("Read all pages. Resolve ids to names. Check notes/comments before deciding. "
                                        "Do not re-probe tools you already understand.")
    return json.dumps({"what_went_wrong": wrong, "facts": facts, "tool_conventions": conv, "skill": skill,
                       "prompt_patch": patch, "bad_facts": []})
