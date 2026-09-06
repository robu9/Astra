"""Expose any InProcessServer as a real MCP stdio server.

Used to run Astra's fixtures as genuine MCP processes so the MCP client path is exercised:

    python -m astra.tools.mcp_serve crm
    python -m astra.tools.mcp_serve tracker --seed 3
"""
from __future__ import annotations

import argparse
import json
import sys

from astra.tools.base import InProcessServer, ToolError


def serve(server: InProcessServer) -> None:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if rid is None:
            continue  # notification
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": server.name, "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                                    for t in server.list_tools()]}
            elif method == "tools/call":
                try:
                    value = server.call(params.get("name"), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
                except ToolError as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            elif method == "ping":
                result = {}
            else:
                out.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "error": {"code": -32601, "message": f"method not found: {method}"}}) + "\n")
                out.flush()
                continue
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        except Exception as e:  # never die on a bad request
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}}) + "\n")
        out.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", choices=["crm", "tracker"])
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    from astra.fixtures import make_fixture
    serve(make_fixture(a.fixture, seed=a.seed))


if __name__ == "__main__":
    main()
