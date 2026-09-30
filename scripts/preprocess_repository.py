#!/usr/bin/env python3
"""Use after `uv sync --extra dev` or `pip install -e .`."""

import sys

from iris_analyzer.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["preprocess", *sys.argv[1:]]))
