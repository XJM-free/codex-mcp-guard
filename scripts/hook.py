#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from codex_mcp_guard.cli import main

    raise SystemExit(main(["hook", *sys.argv[1:]]))
