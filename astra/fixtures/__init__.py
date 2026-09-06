"""Fixture third-party apps. Astra has zero knowledge of them; they exist so the demo has something to learn.

Each fixture hides business rules inside its data (not its schema). Data is regenerated per seed, so the only
thing that transfers between runs is understanding, never memorised ids.
"""
from astra.fixtures.crm import CRM
from astra.fixtures.tracker import Tracker
from astra.tools.base import InProcessServer, collect_tools


def make_fixture(name: str, seed: int = 0) -> InProcessServer:
    if name == "crm":
        app = CRM(seed)
    elif name == "tracker":
        app = Tracker(seed)
    else:
        raise ValueError(f"unknown fixture {name}")
    srv = InProcessServer(name, collect_tools(app))
    srv.app = app  # graders read ground truth from here
    return srv
