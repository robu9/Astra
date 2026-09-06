"""Thin client for Agent Orchestrator (AO). AO is the operating plane Astra runs inside.

AO exposes a loopback daemon (127.0.0.1:3001) and a thin `ao` CLI. We never import AO code; we drive it the way
AO's own orchestrator does: `ao spawn`, `ao send`, `ao session ls`. HTTP is used when the CLI is not on PATH.

Each Astra learning run becomes one AO worker session; reflections are follow-up messages to the same session.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

AO_PORT = int(os.environ.get("AO_PORT", "3001"))
BASE = f"http://127.0.0.1:{AO_PORT}/api/v1"


class AOClient:
    def __init__(self, project: str | None = None, agent: str | None = None):
        self.project = project or os.environ.get("AO_PROJECT_ID")
        self.agent = agent or os.environ.get("ASTRA_AO_AGENT")  # e.g. claude-code, codex, cursor
        self.cli = shutil.which("ao")

    # --- health -------------------------------------------------------------------------------------------------
    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{AO_PORT}/readyz", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    # --- http -------------------------------------------------------------------------------------------------------
    def _http(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"{BASE}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    # --- operations -----------------------------------------------------------------------------------------------
    def spawn(self, prompt: str, name: str | None = None) -> str:
        """Start one AO worker with an inline prompt. Returns the session id (or raw output when parsing fails)."""
        if self.cli:
            cmd = [self.cli, "spawn", "--prompt", prompt[:4000].replace("\n", " ")]
            if self.project:
                cmd += ["--project", self.project]
            if self.agent:
                cmd += ["--agent", self.agent]
            if name:
                cmd += ["--name", name]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                raise RuntimeError(f"ao spawn failed: {out.stderr.strip() or out.stdout.strip()}")
            text = out.stdout.strip()
            for tok in text.replace("\n", " ").split():
                if tok.startswith("ao-"):
                    return tok.strip(".,:")
            return text
        body = {"prompt": prompt, **({"projectId": self.project} if self.project else {}),
                **({"agent": self.agent} if self.agent else {}), **({"name": name} if name else {})}
        res = self._http("POST", "/sessions", body)
        return str(res.get("id") or res.get("session", {}).get("id") or res)

    def send(self, session_id: str, message: str) -> None:
        if self.cli:
            subprocess.run([self.cli, "send", "--session", session_id, "--message", message], check=False,
                           capture_output=True, text=True, timeout=60)
            return
        self._http("POST", f"/sessions/{session_id}/send", {"message": message})

    def sessions(self) -> list[dict]:
        if self.cli:
            out = subprocess.run([self.cli, "session", "ls", "--json"], capture_output=True, text=True, timeout=30)
            try:
                data = json.loads(out.stdout)
                return data if isinstance(data, list) else data.get("sessions", [])
            except json.JSONDecodeError:
                return []
        res = self._http("GET", "/sessions")
        return res if isinstance(res, list) else res.get("sessions", [])


def worker_prompt(family: str, seed: int, transport: str = "inprocess") -> str:
    """The instruction an AO worker receives to execute exactly one Astra learning run and commit its artefacts."""
    return (
        f"You are an Astra learning-run worker. In this repo run exactly one learning iteration and commit its artefacts.\n"
        f"1. `python -m astra run {family} --runs 1 --seed {seed} --transport {transport}`\n"
        f"2. Read runs/{family}-r{seed:02d}/reflection.json and memory/*.json; summarise in one paragraph what Astra learned "
        f"(new facts, tool conventions, skill status) and how quality/cost/latency moved vs the previous row in runs/results.tsv.\n"
        f"3. `python -m astra scoreboard` then commit memory/, runs/results.tsv, runs/{family}-r{seed:02d}/ and scoreboard/data.js "
        f"with message 'learn({family}): run r{seed:02d}'. Push. Do not edit agent code."
    )
