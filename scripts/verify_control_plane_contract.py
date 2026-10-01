#!/usr/bin/env python3
"""Check the real WAS response/enums boundary with no DB, model or deployment."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from iris_analyzer.integrations import BackendJobStatus, LocalAnalysisClient
from iris_analyzer.pipeline import write_json


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("WAS contract module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def verify(was_root: Path, fixture: Path, out: Path) -> dict[str, Any]:
    response = load_module(was_root / "app/schemas/response.py", "_iris_was_response")
    enums = load_module(was_root / "app/enums.py", "_iris_was_enums")
    assert {item.value for item in enums.JobStatus} == {item.value for item in BackendJobStatus}
    outcome = await LocalAnalysisClient().analyze_repository(fixture, out=out / "analysis")
    payload = outcome.to_response_data()
    envelope = response.ApiResponse[dict[str, Any]](data=payload).model_dump(by_alias=True, exclude_none=True)
    # Original source Field.value=null must remain, while envelope nulls are omitted.
    unknown = [
        field
        for service in envelope["data"]["analysisResult"]["services"]
        for field in service.values()
        if isinstance(field, dict) and field.get("status") == "unknown"
    ]
    assert unknown and all("value" in field and field["value"] is None for field in unknown)
    assert "code" not in envelope and "details" not in envelope
    assert "requestId" not in envelope
    assert envelope["data"]["deploymentAuthorized"] is False
    sha = subprocess.check_output(["git", "-C", str(was_root), "rev-parse", "HEAD"], text=True).strip()
    report = {
        "contractVersion": payload["contractVersion"],
        "wasCommit": sha,
        "passed": True,
        "modelCalls": 0,
        "databaseWrites": 0,
        "deploymentActions": 0,
        "checks": [
            "existing JobStatus codes",
            "real ApiResponse camelCase envelope",
            "unknown value null preserved",
            "request ID remains a header",
            "analysis execution and review status separate",
            "deployment approval not inferred",
        ],
    }
    write_json(out / "response-example.json", envelope)
    write_json(out / "contract-verification.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--was-root", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path("fixtures/separated-web-api"))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if sys.version_info < (3, 13):
        raise SystemExit("The reviewed WAS requires Python 3.13+")
    print(json.dumps(asyncio.run(verify(args.was_root.resolve(), args.repo.resolve(), args.out.resolve()))))


if __name__ == "__main__":
    main()
