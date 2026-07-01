#!/usr/bin/env python3
"""Deprecated compatibility entrypoint for Mongo index checks.

Use ``tools/check_and_fix_indexes.py`` for the canonical multi-database
index inventory and repair workflow.
"""

from pathlib import Path
import runpy
import sys


def main() -> None:
    target = Path(__file__).resolve().parent / "tools" / "check_and_fix_indexes.py"
    print(
        "Deprecated: use tools/check_and_fix_indexes.py instead. Forwarding...",
        file=sys.stderr,
    )
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()