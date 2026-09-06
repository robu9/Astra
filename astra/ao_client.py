"""Thin client for Agent Orchestrator (AO). AO is the operating plane Astra runs inside.

AO exposes a loopback daemon (127.0.0.1:3001) and a thin `ao` CLI. We never import AO code; we drive it the way
AO's own orchestrator does: `ao spawn`, `ao send`, `ao session ls`. HTTP is used when the CLI is not on PATH.

One AO worker receives learning iterations sequentially; each next turn starts only after the previous turn is idle.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
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
    def spawn(self, prompt: str, name: str | None = None, branch: str | None = None) -> str:
        """Start one AO worker with an inline prompt. Returns the session id (or raw output when parsing fails)."""
        if self.cli:
            cmd = [self.cli, "spawn", "--prompt", prompt[:4000].replace("\n", " ")]
            if self.project:
                cmd += ["--project", self.project]
            if self.agent:
                cmd += ["--agent", self.agent]
            if name:
                cmd += ["--name", name]
            if branch:
                cmd += ["--branch", branch]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                raise RuntimeError(f"ao spawn failed: {out.stderr.strip() or out.stdout.strip()}")
            text = out.stdout.strip()
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    found = payload.get("id") or payload.get("sessionId") or payload.get("session", {}).get("id")
                    if found:
                        return str(found)
            except json.JSONDecodeError:
                pass
            ids = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]*-\d+\b", text)
            if ids:
                return ids[-1]
            return text
        body = {"prompt": prompt, **({"projectId": self.project} if self.project else {}),
                **({"agent": self.agent} if self.agent else {}), **({"name": name} if name else {}),
                **({"branch": branch} if branch else {})}
        res = self._http("POST", "/sessions", body)
        return str(res.get("id") or res.get("session", {}).get("id") or res)

    def send(self, session_id: str, message: str) -> None:
        if self.cli:
            out = subprocess.run([self.cli, "send", "--session", session_id, "--message", message], check=False,
                                 capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise RuntimeError(f"ao send failed: {out.stderr.strip() or out.stdout.strip()}")
            return
        self._http("POST", f"/sessions/{session_id}/send", {"message": message})

    def get_session(self, session_id: str) -> dict:
        if self.cli:
            cmd = [self.cli, "session", "get", session_id, "--json"]
            if self.project:
                cmd += ["--project", self.project]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise RuntimeError(f"ao session get failed: {out.stderr.strip() or out.stdout.strip()}")
            return json.loads(out.stdout)
        res = self._http("GET", f"/sessions/{session_id}")
        return res.get("session", res) if isinstance(res, dict) else {}

    @staticmethod
    def _states(value) -> set[str]:
        states: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"status", "state", "agentstatus", "agent_status"} and isinstance(item, str):
                    states.add(item.lower().replace("-", "_").replace(" ", "_"))
                states.update(AOClient._states(item))
        elif isinstance(value, list):
            for item in value:
                states.update(AOClient._states(item))
        return states

    def wait_until_idle(self, session_id: str, timeout: float = 1800, poll: float = 5) -> dict:
        """Wait until a worker finishes its turn before sending the next learning iteration."""
        deadline = time.monotonic() + timeout
        idle_polls = 0
        failed = {"failed", "error", "terminated", "killed", "crashed"}
        idle = {"idle", "waiting", "awaiting_input", "completed", "complete", "done", "stopped"}
        while time.monotonic() < deadline:
            session = self.get_session(session_id)
            states = self._states(session)
            if states & failed:
                raise RuntimeError(f"AO session {session_id} failed with state(s): {sorted(states)}")
            if states & idle:
                idle_polls += 1
                if idle_polls >= 2:
                    return session
            else:
                idle_polls = 0
            time.sleep(max(0.2, poll))
        raise TimeoutError(f"AO session {session_id} did not become idle within {timeout:.0f}s")

    @staticmethod
    def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
        if check and out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
        return out

    def branch_head(self, branch: str) -> str | None:
        out = self._git(["rev-parse", "--verify", f"refs/heads/{branch}"], check=False)
        return out.stdout.strip() if out.returncode == 0 else None

    def verify_iteration(self, branch: str, family: str, seed: int, previous_head: str | None) -> str:
        """Prove the AO turn committed complete artifacts and pushed that exact commit."""
        head = self.branch_head(branch)
        if not head or head == previous_head:
            raise RuntimeError(f"AO worker did not create a new commit on {branch}")
        results = self._git(["show", f"{head}:runs/results.tsv"]).stdout.splitlines()
        if len(results) < 2:
            raise RuntimeError("AO worker commit has no result row")
        columns = results[0].split("\t")
        row = dict(zip(columns, results[-1].split("\t")))
        if row.get("family") != family or row.get("seed") != str(seed):
            raise RuntimeError(f"AO worker result mismatch: expected {family} seed {seed}, got {row}")
        run_id = row.get("run_id", "")
        required = [f"runs/{run_id}/trace.json", f"runs/{run_id}/reflection.json",
                    "runs/results.tsv", "scoreboard/data.js", "memory/episodic.jsonl"]
        for path in required:
            if self._git(["cat-file", "-e", f"{head}:{path}"], check=False).returncode != 0:
                raise RuntimeError(f"AO worker commit is missing {path}")
        remote = self._git(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).stdout.split()
        if not remote or remote[0] != head:
            raise RuntimeError(f"AO worker commit {head[:10]} was not pushed to origin/{branch}")
        return head

    def sessions(self) -> list[dict]:
        if self.cli:
            out = subprocess.run([self.cli, "session", "ls", "--json"], capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise RuntimeError(f"ao session ls failed: {out.stderr.strip() or out.stdout.strip()}")
            try:
                data = json.loads(out.stdout)
                return data if isinstance(data, list) else data.get("sessions", data.get("data", []))
            except json.JSONDecodeError:
                return []
        res = self._http("GET", "/sessions")
        return res if isinstance(res, list) else res.get("sessions", [])


def worker_prompt(family: str, seed: int, transport: str = "inprocess", followup: bool = False) -> str:
    """The instruction an AO worker receives to execute exactly one Astra learning run and commit its artefacts."""
    return (
        ("Continue the same Astra learning series. " if followup else "You are an Astra learning-run worker. ")
        + f"Run exactly one learning iteration for {family} seed {seed} and commit its artefacts.\n"
        f"1. `python3 -m astra run {family} --runs 1 --seed {seed} --transport {transport}`\n"
        f"2. Read the last row of runs/results.tsv to get the collision-safe run_id. Read that run's reflection.json "
        f"and memory/*.json; summarise in one paragraph what Astra learned "
        f"(new facts, tool conventions, skill status) and how quality/cost/latency moved vs the previous row in runs/results.tsv.\n"
        f"3. `python3 -m astra scoreboard` then commit memory/, runs/results.tsv, the new run directory, and scoreboard/data.js "
        f"with message 'learn({family}): run r{seed:02d}'. Run `git push -u origin HEAD`. If the run, commit, or push "
        f"fails, stop and report the error. Do not edit agent code."
    )
