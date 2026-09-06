"""Tool abstractions. Astra never knows what app is behind a tool; it only sees this surface."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """Raised by a tool server when a call fails. Message is what the agent sees."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def required(self) -> list[str]:
        return list(self.input_schema.get("required", []))

    def to_prompt(self) -> str:
        props = self.input_schema.get("properties", {})
        args = ", ".join(
            f"{k}{'' if k in self.required() else '?'}: {v.get('type', 'any')}" for k, v in props.items()
        )
        return f"- {self.name}({args}) - {self.description}"


class ToolServer:
    """Minimal interface every adapter implements (in-process fixture, MCP stdio, HTTP)."""

    name: str

    def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError

    def call(self, name: str, args: dict) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


class InProcessServer(ToolServer):
    """Wraps Python callables decorated with @tool into a ToolServer."""

    def __init__(self, name: str, tools: dict[str, tuple[Callable, ToolSpec]]):
        self.name = name
        self._tools = tools

    def list_tools(self) -> list[ToolSpec]:
        return [spec for _, spec in self._tools.values()]

    def call(self, name: str, args: dict) -> Any:
        if name not in self._tools:
            raise ToolError(f"unknown tool '{name}'")
        fn, spec = self._tools[name]
        missing = [r for r in spec.required() if r not in args]
        if missing:
            raise ToolError(f"missing required argument(s): {', '.join(missing)}")
        allowed = set(spec.input_schema.get("properties", {}).keys())
        extra = [k for k in args if k not in allowed]
        if extra:
            raise ToolError(f"unexpected argument(s): {', '.join(extra)}")
        return fn(**args)


def tool(description: str, schema: dict | None = None):
    """Decorator: attach a ToolSpec to a function. Schema inferred from signature if omitted."""

    def deco(fn: Callable):
        if schema is None:
            props, req = {}, []
            for p in inspect.signature(fn).parameters.values():
                t = {int: "integer", float: "number", bool: "boolean", str: "string", list: "array", dict: "object"}
                props[p.name] = {"type": t.get(p.annotation, "string")}
                if p.default is inspect._empty:
                    req.append(p.name)
            s = {"type": "object", "properties": props, "required": req}
        else:
            s = schema
        fn.__tool_spec__ = ToolSpec(name=fn.__name__, description=description, input_schema=s)
        return fn

    return deco


def collect_tools(obj) -> dict[str, tuple[Callable, ToolSpec]]:
    out = {}
    for attr in dir(obj):
        fn = getattr(obj, attr)
        spec = getattr(fn, "__tool_spec__", None)
        if spec is not None:
            out[spec.name] = (fn, spec)
    return out
