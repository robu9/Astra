"""Write scoreboard/friend-runs-data.js from docs/friend-branch-runs.json.

  python -m astra.friend_board
Then open scoreboard/friend-runs.html (file:// or `ao preview scoreboard/friend-runs.html`).
"""
from __future__ import annotations

import json
from pathlib import Path


def build(json_path: str = "docs/friend-branch-runs.json",
          out_path: str = "scoreboard/friend-runs-data.js") -> Path:
    src = Path(json_path)
    data = json.loads(src.read_text(encoding="utf-8"))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.FRIEND_RUNS = " + json.dumps(data, indent=2) + ";\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}. Open scoreboard/friend-runs.html")
