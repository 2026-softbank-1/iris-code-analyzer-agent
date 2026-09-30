#!/usr/bin/env python3
import sys

from iris_analyzer.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["evaluate", *sys.argv[1:]]))
