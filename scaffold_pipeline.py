#!/usr/bin/env python3
"""Backward-compatible shim for `scaffold_pipeline.py`.

Real implementation: `agentflow_pipeline.scaffold`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentflow_pipeline.scaffold import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
