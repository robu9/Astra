from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from astra.actor import Actor
from astra.ao_client import AOClient
from astra.cli import WRITE_PREFIXES, cmd_ao_run, cmd_reset
from astra.controller import Budget
from astra.engine import Engine
from astra.gateway import ToolGateway, looks_write_capable
from astra.llm import LLMResult
from astra.memory import Memory
from astra.tools.base import InProcessServer, ToolError, ToolServer, ToolSpec


class SequenceLLM:
    def __init__(self, replies: list[str], costs: list[float] | None = None):
        self.replies = iter(replies)
        self.costs = iter(costs or [0.001] * len(replies))

    def chat(self, model, messages, **kwargs):
        text = next(self.replies)
        return LLMResult(text, model, 10, 5, 0.0, next(self.costs))


class CloseTrackingServer(ToolServer):
    name = "test"

    def __init__(self):
        self.closed = False

    def list_tools(self):
        return []

    def call(self, name, args):
        raise AssertionError("no calls expected")

    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def test_repeated_identical_calls_are_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            def fail(value: str):
                calls.append(value)
                raise ToolError("bad value")

            spec = ToolSpec("write_value", "Write a value", {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]
            })
            server = InProcessServer("app", {"write_value": (fail, spec)})
            memory = Memory(Path(td) / "memory")
            gateway = ToolGateway(memory)
            gateway.attach(server, probe=False)
            repeated = json.dumps({"thought": "try", "tool": "app.write_value", "args": {"value": "x"}})
            llm = SequenceLLM([repeated, repeated, repeated, json.dumps({"final": {"ok": False}})])
            actor = Actor(llm, gateway, memory)
            trace = actor.run("run", "write", "family", {"ok": "bool"},
                              Budget("model", "fast", 10, 100, "novel", "test"))
            self.assertEqual(calls, ["x"])
            self.assertEqual(gateway.run_stats()["tool_calls"], 1)
            self.assertEqual(trace.final, {"ok": False})

    def test_cost_limit_stops_before_tool_execution(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            def write():
                calls.append(True)

            spec = ToolSpec("write", "Write", {"type": "object", "properties": {}})
            memory = Memory(Path(td) / "memory")
            gateway = ToolGateway(memory)
            gateway.attach(InProcessServer("app", {"write": (write, spec)}), probe=False)
            reply = json.dumps({"tool": "app.write", "args": {}})
            trace = Actor(SequenceLLM([reply], [0.3]), gateway, memory).run(
                "run", "write", "family", {}, Budget("model", "fast", 5, 100, "novel", "test", 0.25))
            self.assertEqual(calls, [])
            self.assertEqual(trace.stop_reason, "cost_budget_exhausted")

    def test_promoted_skill_revision_can_be_reverted(self):
        with tempfile.TemporaryDirectory() as td:
            skills = Memory(Path(td)).skills
            skills.propose("good", "task", ["good step"], ["app.read"], ["app"], "r1", "family")
            skills.promote("family", ["app"], "r2")
            skills.propose("bad", "task", ["bad step"], ["app.write"], ["app"], "r3", "family")
            self.assertTrue(skills.has_candidate("family", ["app"]))
            skills.reject_candidate("family", ["app"], "r4", "regression")
            restored = skills.skills[0]
            self.assertEqual(restored["status"], "promoted")
            self.assertEqual(restored["name"], "good")
            self.assertEqual(restored["steps"], ["good step"])

    def test_run_ids_are_collision_safe(self):
        with tempfile.TemporaryDirectory() as td:
            engine = Engine(Path(td) / "memory", Path(td) / "runs", llm=object(),
                            models={"fast": "fast", "strong": "strong"}, log=lambda *_: None)
            (engine.runs_root / "task-r01").mkdir()
            self.assertEqual(engine._unique_run_id("task-r01"), "task-r01-attempt02")

    def test_snapshot_metrics_are_server_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            memory = Memory(Path(td))
            memory.skills.propose("one", "task", ["step"], [], ["crm"], "r1", "crm-task")
            memory.skills.promote("crm-task", ["crm"], "r2")
            memory.tool_model.ensure("crm.read", "")
            memory.tool_model.add_convention("crm.read", "known")
            self.assertEqual(memory.snapshot(["tracker"])["skills_promoted"], 0)
            self.assertEqual(memory.snapshot(["tracker"])["tools_known"], 0)

    def test_write_detection_uses_annotations_and_names(self):
        read = ToolSpec("execute_query", "Run a read query", annotations={"readOnlyHint": True})
        write = ToolSpec("comments.publishReply", "Publish a reply")
        self.assertFalse(looks_write_capable(read, WRITE_PREFIXES))
        self.assertTrue(looks_write_capable(write, WRITE_PREFIXES))

    def test_engine_closes_server_when_actor_fails(self):
        with tempfile.TemporaryDirectory() as td:
            server = CloseTrackingServer()

            class BrokenLLM:
                def chat(self, *args, **kwargs):
                    raise RuntimeError("provider down")

            engine = Engine(Path(td) / "memory", Path(td) / "runs", BrokenLLM(),
                            {"fast": "fast", "strong": "strong"}, log=lambda *_: None)
            with self.assertRaises(RuntimeError):
                engine.run_once("family", "task", {}, [server], 1, grader=lambda *_: (0, "", False))
            self.assertTrue(server.closed)

    def test_reset_recreates_clean_store_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            memory, runs = root / "memory", root / "runs"
            Memory(memory).semantic.add("fact", "app", None, "evidence", "r1")
            runs.mkdir()
            (runs / "old").mkdir()
            previous = Path.cwd()
            try:
                os.chdir(root)
                cmd_reset(Namespace(memory=str(memory), runs=str(runs)))
            finally:
                os.chdir(previous)
            self.assertEqual(Memory(memory).semantic.count(), 0)
            self.assertEqual(len((runs / "results.tsv").read_text().splitlines()), 1)
            self.assertFalse((runs / "old").exists())

    @patch("astra.ao_client.AOClient")
    def test_ao_iterations_use_one_sequential_session(self, client_class):
        client = client_class.return_value
        client.available.return_value = True
        client.spawn.return_value = "ao-1"
        cmd_ao_run(Namespace(family=["crm_at_risk"], runs_n=3, seed=1, transport="inprocess",
                             ao_timeout=10, ao_poll=0.2))
        self.assertEqual(client.spawn.call_count, 1)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(client.wait_until_idle.call_count, 3)

    @patch("astra.ao_client.subprocess.run")
    def test_ao_cli_parses_project_session_ids_and_list_shape(self, run):
        run.side_effect = [
            Namespace(returncode=0, stdout="Spawned session astra-25\n", stderr=""),
            Namespace(returncode=0, stdout=json.dumps({"data": [{"id": "astra-25"}]}), stderr=""),
        ]
        client = AOClient()
        client.cli = "/bin/ao"
        self.assertEqual(client.spawn("task", "name"), "astra-25")
        self.assertEqual(client.sessions(), [{"id": "astra-25"}])

    def test_ao_state_parser_handles_installed_cli_shape(self):
        payload = {"session": {"status": "idle", "activity": {"state": "idle"}}}
        self.assertEqual(AOClient._states(payload), {"idle"})


if __name__ == "__main__":
    unittest.main()
