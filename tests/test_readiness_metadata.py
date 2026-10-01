"""Build ownership and scoped configuration from bounded immutable source."""

import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import Limits, canonical_bytes
from iris_analyzer.deployment.dossier import build_deployment_dossier
from iris_analyzer.preprocess import expand_context, prepare_context, release_snapshot
from iris_analyzer.readiness import build_readiness
from iris_analyzer.result import static_analysis


def capture(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return prepare_context(tmp_path)


def package(**extra):
    return json.dumps(
        {"name": "fixture", "scripts": {"build": "vite build", "start": "node index.js"}, **extra}
    )


def test_build_container_directory_is_not_runtime_directory(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "package-lock.json": "{}",
            "Dockerfile": 'FROM node:22 AS build\nWORKDIR /app\nCOPY package.json .\nRUN npm ci --ignore-scripts\nRUN npm run build\nFROM node:22\nWORKDIR /app/server\nEXPOSE 3000\nCMD ["node", "index.js"]\n',
            "compose.yaml": "services:\n  app:\n    build: .\n",
        },
    )
    try:
        result = build_deployment_dossier(static_analysis(bundle), bundle)
        target = next(t for t in result["sourceReadiness"]["buildTargets"] if t["serviceName"] == "app")
        assert target["buildWorkingDirectory"] == "/app"
        assert target["runtimeWorkingDirectory"] == "/app/server"
        assert target["contextPath"] == "." and target["contextBasis"] == "compose"
        assert target["dockerfilePath"] == "Dockerfile"
        assert target["lockfiles"] == ["package-lock.json"]
        build = next(s for s in result["benchmarkPlan"]["scenarios"] if s["kind"] == "build")
        assert build["inputs"]["workingDirectory"] == "/app"
        assert build["inputs"]["workingDirectoryScope"] == "container_build_stage"
        assert build["inputs"]["runtimeWorkingDirectory"]["value"] == "/app/server"
        assert result["benchmarkPlan"]["executed"] is False
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_package_script_runs_through_manager_and_install_is_policy(tmp_path):
    bundle = capture(tmp_path, {"package.json": package(), "package-lock.json": "{}"})
    try:
        target = build_readiness(bundle)["buildTargets"][0]
        assert target["buildCommand"] == "npm run build"
        assert target["buildCommandBasis"] == "policy"
        assert target["buildWorkingDirectory"] == "."
        assert target["installCommand"] == "npm ci --ignore-scripts"
        assert target["installCommandBasis"] == "policy"
        assert target["packageManager"] == "npm"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_compose_db_environment_ownership_survives_secret_masking(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "Dockerfile": "FROM node:22\n",
            "compose.yaml": "services:\n  app:\n    build: .\n    environment:\n      SESSION_SECRET: ${SESSION_SECRET:?required}\n      MONGO_URI: mongodb://user:private-value@mongo:27017/db\n      PUBLIC_URL: ${PUBLIC_URL:-http://localhost}\n  mongo:\n    image: mongo:8\n    environment:\n      MONGO_INITDB_ROOT_PASSWORD: super-private-value\n",
        },
    )
    try:
        report = build_readiness(bundle)
        env = {v["key"]: v for v in report["environmentVariables"]}
        assert env["SESSION_SECRET"]["required"] is True
        assert env["SESSION_SECRET"]["serviceName"] == "app"
        assert env["MONGO_INITDB_ROOT_PASSWORD"]["component"] == "compose:mongo"
        assert env["MONGO_INITDB_ROOT_PASSWORD"]["serviceName"] == "mongo"
        assert env["PUBLIC_URL"]["required"] is False
        connection = report["serviceConnections"][0]
        assert (
            connection["fromService"],
            connection["toService"],
            connection["port"],
            connection["environmentKey"],
        ) == ("app", "mongo", 27017, "MONGO_URI")
        assert "private-value" not in canonical_bytes(bundle).decode()
        assert "private-value" not in canonical_bytes(report).decode()
        static_analysis(bundle)  # Supplemental fact components remain valid v1 paths.
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_source_env_phases_defaults_and_example_unknowns(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "index.js": "const a = process.env.API_TOKEN; const b = process.env.PORT || 3000; const c = import.meta.env.VITE_API_URL; // process.env.COMMENT_SECRET\n",
            ".env.example": "EXAMPLE_SECRET=do-not-copy\n",
        },
    )
    try:
        env = {v["key"]: v for v in build_readiness(bundle)["environmentVariables"]}
        assert env["API_TOKEN"]["required"] is None and env["API_TOKEN"]["phase"] == "runtime"
        assert env["PORT"]["required"] is False
        assert env["VITE_API_URL"]["phase"] == "build"
        assert env["EXAMPLE_SECRET"]["phase"] == "unknown"
        assert "COMMENT_SECRET" not in env
        assert "do-not-copy" not in canonical_bytes(bundle).decode()
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


@pytest.mark.parametrize(
    "context", ["..", "../outside", "/tmp/outside", "${BUILD_CONTEXT}", "https://example.com/source.git"]
)
def test_invalid_compose_context_is_not_relabelled_as_repository(tmp_path, context):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "Dockerfile": "FROM node:22\n",
            "compose.yaml": f"services:\n  app:\n    build: '{context}'\n",
        },
    )
    try:
        target = next(t for t in build_readiness(bundle)["buildTargets"] if t["serviceName"] == "app")
        assert target["contextPath"] is None and target["status"] == "needs_input"
        assert any(item["key"] == "deployment.root" for item in bundle["unresolved"])
        static_analysis(bundle)
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_compose_variants_do_not_merge_build_target_selection(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "Dockerfile": "FROM node:22 AS build\nWORKDIR /app\nRUN npm run build\n",
            "compose.yaml": "services:\n  app:\n    build: .\n",
            "compose.dev.yaml": "services:\n  app:\n    build: {context: ., target: build}\n",
        },
    )
    try:
        dossier = build_deployment_dossier(static_analysis(bundle), bundle)
        targets = [t for t in dossier["sourceReadiness"]["buildTargets"] if t["serviceName"] == "app"]
        assert len(targets) == 2 and {t["condition"] for t in targets} == {
            None,
            "when Compose file compose.dev.yaml is selected",
        }
        build = next(s for s in dossier["benchmarkPlan"]["scenarios"] if s["kind"] == "build")
        assert build["inputs"]["requiresTargetSelection"] is True
        assert build["inputs"]["workingDirectory"] is None
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_monorepo_keeps_package_ownership_and_lock_conflicts(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": '{"private":true,"workspaces":["apps/*"]}',
            "apps/api/package.json": package(packageManager="pnpm@9.15.0"),
            "apps/api/pnpm-lock.yaml": "lockfileVersion: 9.0\n",
            "apps/api/package-lock.json": "{}",
            "apps/web/package.json": package(),
            "apps/api/index.js": "console.log(process.env.API_SECRET);\n",
            "apps/web/index.js": "console.log(import.meta.env.VITE_PUBLIC_URL);\n",
        },
    )
    try:
        report = build_readiness(bundle)
        targets = {t["component"]: t for t in report["buildTargets"]}
        assert targets["apps/api"]["packageManager"] is None
        assert targets["apps/api"]["status"] == "needs_input"
        assert targets["apps/api"]["installCommand"] is None
        env = {v["key"]: v for v in report["environmentVariables"]}
        assert env["API_SECRET"]["component"] == "apps/api"
        assert env["VITE_PUBLIC_URL"]["component"] == "apps/web"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_metadata_expansion_uses_immutable_captured_source(tmp_path):
    bundle = capture(
        tmp_path,
        {"package.json": package(), "Dockerfile": "FROM node:22\nWORKDIR /build\nRUN npm run build\n"},
    )
    try:
        before = build_readiness(bundle)["buildTargets"]
        (tmp_path / "Dockerfile").write_text("FROM hostile\nWORKDIR /changed\n")
        expanded = expand_context(bundle, ["Dockerfile"], Limits(max_bundle_bytes=400000))
        after = build_readiness(expanded)["buildTargets"]
        assert before == after
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


@pytest.mark.parametrize("name", ["Temp_log", "portpolio-production"])
def test_real_samples_output_build_contract(name):
    root = Path(__file__).resolve().parents[2] / "tested_code" / name
    if not root.is_dir():
        pytest.skip("User sample is absent")
    bundle = prepare_context(root)
    try:
        dossier = build_deployment_dossier(static_analysis(bundle), bundle)
        assert dossier["sourceReadiness"]["buildTargets"]
        if name == "Temp_log":
            target = next(t for t in dossier["sourceReadiness"]["buildTargets"] if t["serviceName"] == "app")
            assert target["buildWorkingDirectory"] == "/app"
            assert target["runtimeWorkingDirectory"] == "/app/server"
            assert dossier["sourceReadiness"]["serviceConnections"][0]["toService"] == "mongo"
        else:
            target = dossier["sourceReadiness"]["buildTargets"][0]
            assert target["buildCommand"] == "npm run build"
            assert target["dockerfilePath"] is None
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_compose_default_context_and_nested_dockerfile(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "ops/Dockerfile": "FROM node:22 AS build\nWORKDIR /builder\nRUN npm run build\n",
            "compose.yaml": "services:\n  app:\n    build: {dockerfile: ops/Dockerfile}\n",
        },
    )
    try:
        target = next(t for t in build_readiness(bundle)["buildTargets"] if t["serviceName"] == "app")
        assert target["contextPath"] == "." and target["dockerfilePath"] == "ops/Dockerfile"
        assert target["status"] == "detected"
        assert not any(item["key"] == "deployment.root" for item in bundle["unresolved"])
        static_analysis(bundle)
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_env_unknown_fallback_and_echo_are_not_runtime_defaults_or_builds(tmp_path):
    bundle = capture(
        tmp_path,
        {
            "package.json": package(),
            "index.js": 'const a = unavailable || process.env.API_TOKEN; const b = process.env.SECRET + "??"; const c = process.env.PORT || process.env.OTHER_PORT;\n',
            "Dockerfile": "FROM node:22\nWORKDIR /app\nRUN echo npm run build\n",
        },
    )
    try:
        report = build_readiness(bundle)
        assert all(v["required"] is None for v in report["environmentVariables"])
        target = next(t for t in report["buildTargets"] if t["dockerfilePath"])
        assert target["buildCommand"] is None
        assert target["steps"][0]["phase"] == "other"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
