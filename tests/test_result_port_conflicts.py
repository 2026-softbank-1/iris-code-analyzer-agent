"""Source listeners remain independent facts when deployment metadata differs."""

import json
from copy import deepcopy

import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.preprocess import prepare_context, release_snapshot
from iris_analyzer.result import static_analysis, validate_analysis


def write(repo, path, text):
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def express_source(port, *, multiline=False):
    listener = f"app.listen(\n  {port}\n);" if multiline else f"app.listen({port});"
    return "const express = require('express');\nconst app = express();\n" + listener + "\n"


def test_compose_non_node_contexts_own_their_container_facts(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  api:\n    build: ./api\n  web:\n    build: ./web\n")
    write(tmp_path, "api/Dockerfile", 'FROM python:3.13\nEXPOSE 3000\nCMD ["python", "app.py"]\n')
    write(tmp_path, "web/Dockerfile", 'FROM python:3.13\nEXPOSE 8080\nCMD ["python", "app.py"]\n')
    bundle = prepare_context(tmp_path)
    try:
        result = static_analysis(bundle)
        assert len(result["services"]) == 2
        roots = {c["candidateId"]: c["root"] for c in bundle["deploymentCandidates"]}
        for fact in bundle["facts"]:
            if fact.get("candidateId") in roots:
                assert fact["component"] == roots[fact["candidateId"]]
        for service in result["services"]:
            expected = 3000 if service["root"]["value"] == "api" else 8080
            assert {p["value"] for p in service["ports"] if p["scope"] == "container"} == {expected}
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


@pytest.mark.parametrize("multiline", [False, True])
def test_source_listener_conflicts_with_docker_expose_in_same_scope(tmp_path, multiline):
    write(
        tmp_path,
        "package.json",
        json.dumps({"dependencies": {"express": "5"}, "scripts": {"start": "node index.js"}}),
    )
    write(tmp_path, "index.js", express_source(3000, multiline=multiline))
    write(
        tmp_path,
        "Dockerfile",
        'FROM node:24\nWORKDIR /app\nCOPY . .\nEXPOSE 4000\nCMD ["node", "index.js"]\n',
    )
    bundle = prepare_context(tmp_path)
    try:
        assert {
            fact["value"]
            for fact in bundle["facts"]
            if fact["key"] == "runtime.port" and fact["scope"] == "container"
        } == {3000, 4000}
        result = static_analysis(bundle)
        service = result["services"][0]
        assert {port["value"] for port in service["ports"] if port["scope"] == "container"} == {3000, 4000}
        assert result["status"] == "needs_input"
        assert any(
            "Conflicting container ports" in question["reason"]
            and "3000" in question["reason"]
            and "4000" in question["reason"]
            for question in result["questions"]
        )
        assert validate_analysis({"kind": "analysis", "result": result}, bundle) == result
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_matching_listener_and_expose_preserve_both_evidence_without_conflict(tmp_path):
    write(
        tmp_path,
        "package.json",
        json.dumps({"dependencies": {"express": "5"}, "scripts": {"start": "node index.js"}}),
    )
    write(tmp_path, "index.js", express_source(3000))
    write(tmp_path, "Dockerfile", 'FROM node:24\nWORKDIR /app\nEXPOSE 3000\nCMD ["node", "index.js"]\n')
    bundle = prepare_context(tmp_path)
    try:
        result = static_analysis(bundle)
        container_ports = [port for port in result["services"][0]["ports"] if port["scope"] == "container"]
        assert result["status"] == "complete"
        assert len(container_ports) == 1
        assert container_ports[0]["value"] == 3000
        by_id = {evidence["evidenceId"]: evidence for evidence in bundle["evidence"]}
        assert {by_id[identifier]["path"] for identifier in container_ports[0]["evidenceIds"]} == {
            "Dockerfile",
            "index.js",
        }
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_same_build_context_does_not_assign_unrelated_root_listener_to_services(tmp_path):
    write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "workspaces": ["web", "api"],
                "main": "index.js",
            }
        ),
    )
    write(tmp_path, "index.js", express_source(9000))
    write(
        tmp_path,
        "web/package.json",
        json.dumps({"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}}),
    )
    write(
        tmp_path,
        "api/package.json",
        json.dumps({"dependencies": {"express": "5"}, "scripts": {"build": "tsc", "start": "node index.js"}}),
    )
    write(tmp_path, "api/index.js", express_source(3000, multiline=True))
    write(tmp_path, "Dockerfile.web", "FROM nginx:1\nWORKDIR /app/web\nEXPOSE 80\n")
    write(
        tmp_path, "Dockerfile.api", 'FROM node:24\nWORKDIR /app/api\nEXPOSE 3000\nCMD ["node", "index.js"]\n'
    )
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  web:\n    build: {context: ., dockerfile: Dockerfile.web}\n  api:\n    build: {context: ., dockerfile: Dockerfile.api}\n",
    )
    bundle = prepare_context(tmp_path)
    try:
        assert any(
            fact["key"] == "runtime.port" and fact["value"] == 9000 and fact["component"] == "."
            for fact in bundle["facts"]
        )
        result = static_analysis(bundle)
        assert len(result["services"]) == 2
        for service in result["services"]:
            assert service["root"]["value"] == "."
            expected = {80} if service["componentRoots"] == ["web"] else {3000}
            assert {port["value"] for port in service["ports"] if port["scope"] == "container"} == expected
        assert not any("9000" in question["reason"] for question in result["questions"])
        assert result["status"] == "complete"
        model = {"kind": "analysis", "result": deepcopy(result)}
        web = next(service for service in model["result"]["services"] if service["componentRoots"] == ["web"])
        unrelated = next(
            fact for fact in bundle["facts"] if fact["key"] == "runtime.port" and fact["value"] == 9000
        )
        web["ports"][0].update(value=9000, evidenceIds=unrelated["evidenceIds"])
        with pytest.raises(AnalyzerError) as raised:
            validate_analysis(model, bundle)
        assert raised.value.code == "RESULT_OBSERVATION_INVALID"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
