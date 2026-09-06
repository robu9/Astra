"""Durable memory. Four stores, all plain files under memory/ so growth is visible and diffable.

- episodic.jsonl   one line per run: task, tools, outcome, what went wrong
- semantic.json    facts extracted from third-party tool data, namespaced by server
- skills.json      procedural "how to do task X with these tools" recipes, promoted only after success
- tool_model.json  per-tool stats + learned calling conventions (enums, hidden required args, quirks)

Retrieval is scoped by attached servers + task keywords. Server-specific memory never leaks across
servers; server-agnostic lessons (server == null) transfer to new tools.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

STOP = set("the a an and or of to for in on with by from as is are be this that it at into over per all any "
           "each every their its our your list get find show which what who how when where".split())


def keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_\-]+", text.lower()) if len(w) > 2 and w not in STOP}


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")


class EpisodicStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, episode: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(episode, default=str) + "\n")

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def recent(self, family: str, servers: list[str], n: int = 3) -> list[dict]:
        eps = [e for e in self.all() if e.get("family") == family and set(e.get("servers", [])) & set(servers)]
        return eps[-n:]


class SemanticStore:
    def __init__(self, path: Path):
        self.path = path
        self.facts: list[dict] = _load(path, [])

    def add(self, fact: str, server: str | None, source_tool: str | None, evidence: str, run_id: str,
            confidence: float = 0.6, tags: list[str] | None = None) -> bool:
        norm = fact.strip().lower()
        for f in self.facts:
            if f["fact"].strip().lower() == norm and f.get("server") == server:
                f["confidence"] = min(1.0, f["confidence"] + 0.15)
                f["last_seen_run"] = run_id
                f["seen"] = f.get("seen", 1) + 1
                _save(self.path, self.facts)
                return False
        self.facts.append({
            "fact": fact.strip(), "server": server, "source_tool": source_tool, "evidence": evidence[:300],
            "confidence": round(confidence, 2), "first_run": run_id, "last_seen_run": run_id, "seen": 1,
            "tags": tags or sorted(keywords(fact))[:8],
        })
        _save(self.path, self.facts)
        return True

    def demote(self, fact: str, server: str | None, amount: float = 0.3) -> None:
        for f in self.facts:
            if f["fact"].strip().lower() == fact.strip().lower() and f.get("server") == server:
                f["confidence"] = round(max(0.0, f["confidence"] - amount), 2)
        self.facts = [f for f in self.facts if f["confidence"] > 0.05]
        _save(self.path, self.facts)

    def retrieve(self, task: str, servers: list[str], k: int = 12) -> list[dict]:
        kw = keywords(task)
        scored = []
        for f in self.facts:
            if f.get("server") is not None and f["server"] not in servers:
                continue
            overlap = len(kw & set(f.get("tags", []))) + len(kw & keywords(f["fact"]))
            score = f["confidence"] * (1 + 0.4 * overlap) + (0.3 if f.get("server") in servers else 0)
            scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:k]]

    def count(self, servers: list[str] | None = None) -> int:
        if servers is None:
            return len(self.facts)
        return sum(1 for f in self.facts if f.get("server") in servers or f.get("server") is None)


class SkillStore:
    """Procedural memory. A skill is a recipe: when task looks like X, do steps Y with tools Z."""

    def __init__(self, path: Path):
        self.path = path
        self.skills: list[dict] = _load(path, [])

    def propose(self, name: str, when: str, steps: list[str], tools: list[str], servers: list[str],
                run_id: str, family: str) -> dict:
        for s in self.skills:
            if s["family"] == family and set(s["servers"]) == set(servers) and s["status"] != "rejected":
                s.update({"name": name, "when": when, "steps": steps, "tools": tools, "status": "candidate",
                          "proposed_run": run_id, "version": s.get("version", 1) + 1})
                _save(self.path, self.skills)
                return s
        s = {"name": name, "when": when, "steps": steps, "tools": tools, "servers": servers, "family": family,
             "status": "candidate", "proposed_run": run_id, "version": 1, "wins": 0, "uses": 0}
        self.skills.append(s)
        _save(self.path, self.skills)
        return s

    def promote(self, family: str, servers: list[str], run_id: str) -> None:
        for s in self.skills:
            if s["family"] == family and set(s["servers"]) == set(servers) and s["status"] == "candidate":
                s["status"] = "promoted"
                s["promoted_run"] = run_id
        _save(self.path, self.skills)

    def reject_candidate(self, family: str, servers: list[str], run_id: str, reason: str) -> None:
        for s in self.skills:
            if s["family"] == family and set(s["servers"]) == set(servers) and s["status"] == "candidate":
                s["status"] = "rejected"
                s["rejected_run"] = run_id
                s["reason"] = reason
        _save(self.path, self.skills)

    def record_use(self, family: str, servers: list[str], won: bool) -> None:
        for s in self.skills:
            if s["family"] == family and set(s["servers"]) == set(servers) and s["status"] != "rejected":
                s["uses"] = s.get("uses", 0) + 1
                if won:
                    s["wins"] = s.get("wins", 0) + 1
        _save(self.path, self.skills)

    def retrieve(self, task: str, servers: list[str], family: str | None = None) -> list[dict]:
        out = []
        for s in self.skills:
            if s["status"] == "rejected":
                continue
            if not set(s["servers"]) <= set(servers):
                continue
            if family and s["family"] == family:
                out.append(s)
            elif keywords(task) & keywords(s["when"]):
                out.append(s)
        return out

    def count(self, status: str | None = None) -> int:
        return sum(1 for s in self.skills if status is None or s["status"] == status)


class ToolModel:
    """What Astra has learned about each tool: reliability, latency, error signatures, conventions."""

    def __init__(self, path: Path):
        self.path = path
        self.tools: dict[str, dict] = _load(path, {})

    def ensure(self, q: str, description: str) -> None:
        if q not in self.tools:
            self.tools[q] = {"description": description, "calls": 0, "ok": 0, "errors": 0, "latency_total": 0.0,
                             "error_signatures": {}, "conventions": [], "probed": False, "shape": None}
            _save(self.path, self.tools)

    def get(self, q: str) -> dict:
        return self.tools.setdefault(q, {"description": "", "calls": 0, "ok": 0, "errors": 0, "latency_total": 0.0,
                                         "error_signatures": {}, "conventions": [], "probed": False, "shape": None})

    def note(self, q: str, key: str, value: Any) -> None:
        self.get(q)[key] = value
        _save(self.path, self.tools)

    def observe(self, q: str, ok: bool, latency: float, error: str | None, args: dict) -> None:
        t = self.get(q)
        t["calls"] += 1
        t["ok" if ok else "errors"] += 1
        t["latency_total"] = round(t["latency_total"] + latency, 4)
        if error:
            sig = re.sub(r"'[^']*'", "'*'", error)[:120]
            t["error_signatures"][sig] = t["error_signatures"].get(sig, 0) + 1
        _save(self.path, self.tools)

    def add_convention(self, q: str, text: str) -> bool:
        t = self.get(q)
        if text.strip().lower() in (c.lower() for c in t["conventions"]):
            return False
        t["conventions"].append(text.strip())
        t["conventions"] = t["conventions"][-6:]
        _save(self.path, self.tools)
        return True

    def success_rate(self, q: str) -> float:
        t = self.get(q)
        return (t["ok"] / t["calls"]) if t["calls"] else 1.0

    def hint(self, q: str) -> str:
        t = self.get(q)
        bits = []
        if t.get("shape"):
            bits.append(f"returns {t['shape']}")
        for c in t["conventions"]:
            bits.append(c)
        if t["calls"] >= 3 and self.success_rate(q) < 0.6:
            top = max(t["error_signatures"], key=t["error_signatures"].get, default=None)
            bits.append(f"unreliable ({self.success_rate(q):.0%} ok); common error: {top}")
        return "; ".join(bits)

    def known(self, q: str) -> bool:
        t = self.get(q)
        return t["calls"] >= 2 or bool(t["conventions"])


class Memory:
    def __init__(self, root: Path | str = "memory"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.episodic = EpisodicStore(self.root / "episodic.jsonl")
        self.semantic = SemanticStore(self.root / "semantic.json")
        self.skills = SkillStore(self.root / "skills.json")
        self.tool_model = ToolModel(self.root / "tool_model.json")
        self._prompts_path = self.root / "prompt_patches.json"
        self.prompt_patches: dict = _load(self._prompts_path, {})

    # --- prompt patches (actor self-instructions per family+servers) ---------------
    def _pkey(self, family: str, servers: list[str]) -> str:
        return f"{family}@{'+'.join(sorted(servers))}"

    def get_prompt_patch(self, family: str, servers: list[str]) -> str:
        p = self.prompt_patches.get(self._pkey(family, servers), {})
        return p.get("active") or ""

    def propose_prompt_patch(self, family: str, servers: list[str], text: str, run_id: str) -> None:
        k = self._pkey(family, servers)
        p = self.prompt_patches.setdefault(k, {"active": "", "candidate": None, "history": []})
        p["candidate"] = {"text": text.strip(), "run": run_id}
        p["active_before_candidate"] = p["active"]
        p["active"] = text.strip()
        _save(self._prompts_path, self.prompt_patches)

    def resolve_prompt_patch(self, family: str, servers: list[str], keep: bool, run_id: str) -> None:
        k = self._pkey(family, servers)
        p = self.prompt_patches.get(k)
        if not p or not p.get("candidate"):
            return
        p["history"].append({**p["candidate"], "kept": keep, "resolved_run": run_id})
        if not keep:
            p["active"] = p.get("active_before_candidate", "")
        p["candidate"] = None
        _save(self._prompts_path, self.prompt_patches)

    # --- retrieval bundle for the actor -------------------------------------------
    def retrieve(self, task: str, family: str, servers: list[str]) -> dict:
        return {
            "facts": self.semantic.retrieve(task, servers),
            "skills": self.skills.retrieve(task, servers, family),
            "episodes": self.episodic.recent(family, servers),
            "prompt_patch": self.get_prompt_patch(family, servers),
        }

    def snapshot(self, servers: list[str]) -> dict:
        return {
            "facts": self.semantic.count(servers),
            "skills_promoted": self.skills.count("promoted"),
            "skills_candidate": self.skills.count("candidate"),
            "tools_known": sum(1 for q in self.tool_model.tools if self.tool_model.known(q)),
            "episodes": len(self.episodic.all()),
        }

    def wipe(self) -> None:
        for p in self.root.glob("*"):
            if p.is_file():
                p.unlink()
        self.__init__(self.root)

    def created_at(self) -> float:
        return time.time()
