"""JSON-only subprocess bridge for the Build Worker: stdin request -> stdout gate result.

Exit codes: 0 result printed (any decision), 2 invalid request, 1 internal
failure. Errors go to stderr as a short JSON object without source paths,
file contents or environment values.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..contracts import AnalyzerError
from .analysis import run_gate

MAX_REQUEST_BYTES = 64 * 1024


def _fail(code: str, message: str, status: int) -> None:
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    raise SystemExit(status)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="iris-analysis-gate", description=__doc__)
    parser.add_argument("--request-stdin", action="store_true", required=True)
    parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _fail("GATE_REQUEST_INVALID", "Request exceeds 64 KiB", 2)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        _fail("GATE_REQUEST_INVALID", "Request is not valid JSON", 2)
    try:
        result = run_gate(document)
    except AnalyzerError as error:
        status = 1 if error.code == "GATE_RESULT_INVALID" else 2
        _fail(error.code, error.message, status)
    except Exception:  # noqa: BLE001 - never leak tracebacks with source details
        _fail("GATE_FAILED", "Analysis gate failed", 1)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
