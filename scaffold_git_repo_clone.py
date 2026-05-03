#!/usr/bin/env python3
"""Focused entrypoint for Git repo clone case scaffolding."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "scaffold_pipeline.py"


def main() -> int:
    completed = subprocess.run([sys.executable, str(ENTRYPOINT), *sys.argv[1:]], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
