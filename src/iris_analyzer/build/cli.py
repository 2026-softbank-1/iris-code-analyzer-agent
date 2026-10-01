"""JSON-only subprocess bridge for a trusted build worker."""

from __future__ import annotations

import argparse
import json
import sys

from ..contracts import AnalyzerError
from .prepare import prepare_source_build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-stdin", action="store_true", required=True)
    parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise AnalyzerError("BUILD_REQUEST_INVALID", "Preparation request exceeds 64 KiB")
        result = prepare_source_build(json.loads(raw))
    except AnalyzerError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}))
        raise SystemExit(2) from None
    except (OSError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": "BUILD_PREPARATION_FAILED",
                        "message": "Invalid source or preparation input",
                    }
                }
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
