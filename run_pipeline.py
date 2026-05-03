#!/usr/bin/env python3
"""Backward-compatible shim for `run_pipeline.py`.

The real implementation now lives in `agentflow_pipeline.cli`. This file is kept
so existing invocations (`python3 run_pipeline.py ...`) and case yamls referring
to it continue to work after the namespace refactor.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentflow_pipeline.cli import _main_entry  # noqa: E402

if __name__ == "__main__":
    _main_entry()
