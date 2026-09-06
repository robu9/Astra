"""Tool gateway: the only way the actor touches the outside world.

Responsibilities
- attach any ToolServer, namespace tools as `server.tool`
- discover schemas; cheaply probe read-only tools once to learn real return shapes
- call with timing, error-signature capture, in-run caching
- feed every observation into the durable tool model (memory)
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astra.memory import Memory
from astra.tools.base import ToolError, ToolServer, ToolSpec

READ_PREFIXES = ("list", "get", "search", "find", "read", "fetch", "describe", "query", "show")


def _name_words(name: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return set(re.findall(r"[a-z]+", expanded.lower()))


def looks_write_capable(spec: ToolSpec, write_words: tuple[str, ...]) -> bool:
    """Conservatively identify external write tools using MCP hints, name, and description."""
    if spec.annotations.get("readOnlyHint") is True:
        return False
    if spec.annotations.get("destructiveHint") is True:
        return True
    words = _name_words(spec.name)
    if any(word.rstrip("_") in words for word in write_words):
        return True
    description = spec.description.lower()
    mutation_phrases = ("create ", "update ", "delete ", "remove ", "modify ", "send ", "post ",
                        "assign ", "add ", "write ", "upload ", "merge ", "close ", "archive ")
    return any(phrase in description for phrase in mutation_phrases)


@dataclass
class CallRecord:
    tool: str
    args: dict
    ok: bool
    latency_s: float
    result_preview: str
    error: str | None = None
    cached: bool = False
    result: Any = field(default=None, repr=False)


NOISE_KEY_SUFFIXES = ("_url", "url", "node_id", "gravatar_id", "_at_iso", "etag")


def _compact(value: Any, depth: int = 0) -> Any:
    """Drop the boilerplate real APIs return (nulls, hyperlink fields, opaque ids, huge strings) so the model sees data."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if v is None or v == [] or v == {}:
                continue
            if isinstance(k, str) and k.lower().endswith(NOISE_KEY_SUFFIXES) and k.lower() not in ("next_cursor", "cursor"):
                continue
            out[k] = _compact(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + f"...(+{len(value) - 600} chars)"
    return value


def _preview(value: Any, limit: int = 4500) -> str:
    try:
        s = json.dumps(_compact(value), default=str, ensure_ascii=False)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + f"... (+{len(s) - limit} chars; narrow the query or page size)"


def _shape(value: Any) -> str:
    """Compact description of a return shape, for the tool model."""
    if isinstance(value, dict):
        keys = sorted(value.keys())[:12]
        inner = ""
        for k in ("items", "results", "data"):
            if isinstance(value.get(k), list) and value[k] and isinstance(value[k][0], dict):
                inner = f"; {k}[0] keys={sorted(value[k][0].keys())[:12]}"
                break
        return f"object keys={keys}{inner}"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return f"array[{len(value)}] of object keys={sorted(value[0].keys())[:12]}"
        return f"array[{len(value)}]"
    return type(value).__name__


class ToolGateway:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.servers: dict[str, ToolServer] = {}
        self.specs: dict[str, ToolSpec] = {}  # qualified name -> spec
        self._cache: dict[str, Any] = {}
        self.records: list[CallRecord] = []

    # --- attach / discover --------------------------------------------------
    def attach(self, server: ToolServer, probe: bool = True, deny: tuple[str, ...] = ()) -> list[str]:
        """Attach a server. `deny` holds tool-name prefixes to hide (e.g. write tools on a read-only task)."""
        self.servers[server.name] = server
        names = []
        for spec in server.list_tools():
            if deny and looks_write_capable(spec, deny):
                continue
            q = f"{server.name}.{spec.name}"
            self.specs[q] = spec
            names.append(q)
            self.memory.tool_model.ensure(q, spec.description)
        if probe:
            self._probe(server.name, names)
        return names

    def _probe(self, server: str, names: list[str]) -> None:
        """Learn return shapes of arg-less read-only tools once per server (cheap, durable)."""
        for q in names:
            entry = self.memory.tool_model.get(q)
            if entry.get("probed"):
                continue
            spec = self.specs[q]
            if not spec.name.lower().startswith(READ_PREFIXES) or spec.required():
                continue
            rec = self.call(q, {}, count=False)
            self.memory.tool_model.note(q, "probed", True)
            if rec.ok:
                self.memory.tool_model.note(q, "shape", _shape(rec.result))

    def detach(self, server_name: str) -> None:
        srv = self.servers.pop(server_name, None)
        if srv:
            srv.close()
        for q in [q for q in self.specs if q.startswith(server_name + ".")]:
            self.specs.pop(q)

    def attached(self) -> list[str]:
        return list(self.servers.keys())

    def tool_prompt(self) -> str:
        lines = []
        for q, spec in self.specs.items():
            line = spec.to_prompt().replace(f"- {spec.name}(", f"- {q}(", 1)
            hint = self.memory.tool_model.hint(q)
            if hint:
                line += f"\n    learned: {hint}"
            lines.append(line)
        return "\n".join(lines)

    # --- call -------------------------------------------------------------
    def new_run(self) -> None:
        self._cache.clear()
        self.records = []

    def call(self, qualified: str, args: dict, count: bool = True) -> CallRecord:
        if qualified not in self.specs:
            rec = CallRecord(qualified, args, False, 0.0, "", error=f"unknown tool '{qualified}'. Available: "
                             + ", ".join(sorted(self.specs)))
            if count:
                self.records.append(rec)
            return rec
        server_name, tool_name = qualified.split(".", 1)
        key = hashlib.sha1(f"{qualified}|{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()
        if key in self._cache and tool_name.lower().startswith(READ_PREFIXES):
            value = self._cache[key]
            rec = CallRecord(qualified, args, True, 0.0, _preview(value), cached=True, result=value)
            if count:
                self.records.append(rec)
            return rec
        t0 = time.perf_counter()
        try:
            value = self.servers[server_name].call(tool_name, args)
            lat = time.perf_counter() - t0
            rec = CallRecord(qualified, args, True, lat, _preview(value), result=value)
            if tool_name.lower().startswith(READ_PREFIXES):
                self._cache[key] = value
        except ToolError as e:
            lat = time.perf_counter() - t0
            rec = CallRecord(qualified, args, False, lat, "", error=str(e))
        except Exception as e:  # adapter bug or transport failure; still an observation
            lat = time.perf_counter() - t0
            rec = CallRecord(qualified, args, False, lat, "", error=f"{type(e).__name__}: {e}")
        self.memory.tool_model.observe(qualified, rec.ok, rec.latency_s, rec.error, args)
        if count:
            self.records.append(rec)
        return rec

    def run_stats(self) -> dict:
        calls = [r for r in self.records]
        return {
            "tool_calls": len(calls),
            "tool_errors": sum(1 for r in calls if not r.ok),
            "cached_calls": sum(1 for r in calls if r.cached),
            "tool_latency_s": round(sum(r.latency_s for r in calls), 4),
        }
