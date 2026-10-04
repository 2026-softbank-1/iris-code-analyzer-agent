"""Opt-in real Docker smoke for the authored multi-image fixture.

Run with the analyzer environment, Docker available, and --out outside source.
Deletes only its uniquely named Compose project's test containers and volumes.
This verifies local Compose runtime, not AWS/Kubernetes deployment.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

from iris_analyzer.gate import run_gate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "fixtures/db-multi-service"
    gate = run_gate({"schemaVersion": "iris.analysis-gate-request.v1", "sourceRoot": str(root)})
    units = {u["id"]: u for u in gate["units"]}
    assert set(units) == {"api", "web"}
    assert {d["engine"] for d in gate["dependencies"]} == {"postgres", "mongodb"}
    assert units["api"]["dependsOn"] == ["mongo", "postgres"]
    bindings = {row["key"]: row["binding"] for row in units["api"]["env"]}
    assert bindings["DATABASE_URL"]["targetId"] == "postgres"
    assert bindings["MONGO_URI"]["targetId"] == "mongo"
    assert bindings["MONGO_URI"]["urlSuffix"] == "/fixture?authSource=admin"
    assert any(d.get("initScripts") for d in gate["dependencies"])
    project = "iris-smoke-" + uuid4().hex[:12]
    command = ["docker", "compose", "--project-name", project, "--file", str(root / "compose.yaml")]

    def compose(*args, capture=False):
        return subprocess.run(
            command + list(args), check=True, text=True, capture_output=capture, timeout=600
        )

    def request(path):
        deadline = time.monotonic() + 60
        while True:
            try:
                with urlopen(address + path, timeout=10) as response:
                    return json.load(response)
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    try:
        compose("up", "--build", "--detach", "--wait", "--wait-timeout", "180")
        address = "http://" + compose("port", "web", "8080", capture=True).stdout.strip()
        expected = {"postgres": "durable", "mongodb": "durable"}
        assert request("/write") == expected
        compose("restart", "postgres", "mongo", "api")
        assert request("/read") == expected
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "gate-result.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2))
        (args.out / "runtime-result.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "runtime": "local Docker Compose",
                    "appImages": 2,
                    "databases": ["postgres", "mongodb"],
                    "realDatabaseWriteRead": True,
                    "survivedDatabaseRestart": True,
                    "cloudDeploymentTested": False,
                },
                indent=2,
            )
        )
        print("Two app images, PostgreSQL/MongoDB access, initialization and restart persistence: passed")
    finally:
        compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
