"""Real MCP client over stdio (JSON-RPC 2.0). Stdlib only.

Speaks the Model Context Protocol handshake: initialize -> notifications/initialized -> tools/list -> tools/call.
Any MCP server that runs as a subprocess can be attached to Astra with:

    gateway.attach(MCPStdioServer("github", ["npx", "-y", "@modelcontextprotocol/server-github"], env={...}))
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections import deque
from typing import Any

from astra.tools.base import ToolError, ToolServer, ToolSpec

PROTOCOL_VERSION = "2025-06-18"


class MCPStdioServer(ToolServer):
    def __init__(self, name: str, command: list[str], env: dict | None = None, cwd: str | None = None,
                 timeout: float = 60.0):
        if not name.strip():
            raise ValueError("MCP server name must not be empty")
        if not command:
            raise ValueError("MCP command must not be empty")
        self.name = name
        self.timeout = timeout
        merged = dict(os.environ)
        merged.update(env or {})
        resolved = shutil.which(command[0]) or command[0]  # Windows: npx -> npx.cmd, node -> node.exe
        command = [resolved] + list(command[1:])
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=merged, cwd=cwd, text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self._lock = threading.Lock()
        self._tools: list[ToolSpec] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _drain_stderr(self) -> None:
        """Prevent verbose MCP servers from blocking on a full stderr pipe."""
        if not self.proc.stderr:
            return
        for line in self.proc.stderr:
            self._stderr_tail.append(line.rstrip())

    # --- JSON-RPC plumbing -------------------------------------------------
    def _send(self, msg: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            result: dict[str, Any] = {}

            def reader():
                assert self.proc.stdout
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        result["error"] = {"message": "MCP server closed stdout"}
                        return
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # server log noise on stdout
                    if msg.get("id") == rid:
                        result.update(msg)
                        return
                    # notifications / other ids are ignored for this simple client

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(self.timeout)
            if t.is_alive():
                self.close()  # stop the orphan reader before another request can steal its response
                raise ToolError(f"MCP '{method}' timed out after {self.timeout}s")
            if "error" in result:
                err = result["error"]
                raise ToolError(f"MCP error: {err.get('message')} {err.get('data', '')}".strip())
            return result.get("result")

    def _initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "astra", "version": "0.1.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # --- ToolServer ------------------------------------------------------------
    def list_tools(self) -> list[ToolSpec]:
        if self._tools is None:
            res = self._request("tools/list") or {}
            self._tools = [
                ToolSpec(name=t["name"], description=t.get("description", ""),
                         input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                         annotations=t.get("annotations") or {})
                for t in res.get("tools", [])
            ]
        return self._tools

    def call(self, name: str, args: dict) -> Any:
        res = self._request("tools/call", {"name": name, "arguments": args}) or {}
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            raise ToolError(text or "tool returned isError")
        if "structuredContent" in res:
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
