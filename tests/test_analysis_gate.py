import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.gate import run_gate
from iris_analyzer.gate.analysis import REQUEST_SCHEMA, RESULT_SCHEMA

NODE_APP = {
    "name": "svc",
    "scripts": {"start": "node server.js"},
    "dependencies": {"express": "^5.0.0"},
}
SHA = "0123456789abcdef0123456789abcdef01234567"


def write(root: Path, files: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else json.dumps(content))
    return root


def gate(root: Path, **kwargs) -> dict:
    request = {"schemaVersion": "iris.analysis-gate-request.v1", "sourceRoot": str(root), **kwargs}
    result = run_gate(request)
    Draft202012Validator(RESULT_SCHEMA).validate(result)
    return result


def codes(result: dict) -> list[str]:
    return [reason["code"] for reason in result["reasons"]]


def units(result: dict) -> dict[str, dict]:
    return {unit["id"]: unit for unit in result["units"]}


def test_schemas_are_valid_draft_2020_12():
    Draft202012Validator.check_schema(REQUEST_SCHEMA)
    Draft202012Validator.check_schema(RESULT_SCHEMA)


def test_single_dockerfile_skips(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "package.json": NODE_APP,
            "Dockerfile.dev": "FROM node",
        },
    )
    result = gate(repo, sourceSha=SHA)
    assert result["decision"] == "skip"
    assert result["complexity"] == "simple"
    assert codes(result) == ["single_dockerfile"]
    assert result["simpleBuild"] == {"builder": "dockerfile", "dockerfilePath": "Dockerfile"}
    assert result["units"] == [] and result["dependencies"] == []
    assert result["sourceSha"] == SHA
    assert result["signals"]["dockerfiles"] == ["Dockerfile", "Dockerfile.dev"]
    assert result["analysis"]["modelCalls"] == 0 and result["executionAuthorized"] is False


def test_single_node_app_without_dockerfile_uses_railpack(tmp_path):
    repo = write(tmp_path / "repo", {"package.json": NODE_APP, "server.js": "app.listen(8080)\n"})
    result = gate(repo)
    assert (result["decision"], result["complexity"]) == ("skip", "simple")
    assert codes(result) == ["single_railpack_app"]
    assert result["simpleBuild"] == {"builder": "railpack", "dockerfilePath": None}
    assert result["signals"]["runtimeManifests"] == [{"path": "package.json", "runtime": "node"}]


def test_single_python_app_uses_railpack(tmp_path):
    repo = write(tmp_path / "repo", {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = 1\n"})
    result = gate(repo)
    assert result["decision"] == "skip"
    assert result["simpleBuild"]["builder"] == "railpack"
    assert "python" in result["reasons"][0]["message"]


COMPOSE_SHOP = """\
services:
  web:
    build:
      context: ./web
      args:
        VITE_API_BASE_URL: /api
    ports:
      - "127.0.0.1:${WEB_PORT:-8088}:80"
    depends_on: [api]
  api:
    build:
      context: .
      dockerfile: api/Dockerfile
    environment:
      DATABASE_URL: postgresql://u:p@postgres:5432/shop
      REDIS_URL: redis://redis:6379
      NODE_ENV: production
    ports: ["3000:3000"]
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
  worker:
    build: ./worker
    command: ["npm", "run", "worker"]
    environment:
      - REDIS_URL=redis://redis:6379
      - SESSION_SECRET
    depends_on: [redis]
  postgres:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
  tunnel:
    image: cloudflare/cloudflared:latest
"""


def test_compose_with_three_build_services_and_two_dependencies(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": COMPOSE_SHOP,
            "web/Dockerfile": "FROM node:22 AS build\nRUN npm run build\nFROM nginx:1.27\nEXPOSE 80\n",
            "web/package.json": {
                "scripts": {"build": "vite build", "dev": "vite"},
                "devDependencies": {"vite": "6"},
            },
            "web/index.html": "<html></html>",
            "api/Dockerfile": 'FROM node:22\nWORKDIR /app\nCOPY api/ .\nEXPOSE 3000\nCMD ["npm", "start"]\n',
            "api/package.json": NODE_APP,
            "worker/Dockerfile": 'FROM node:22\nCOPY . .\nCMD ["node", "worker.js"]\n',
            "worker/package.json": {"scripts": {"start": "node worker.js"}, "dependencies": {"bullmq": "5"}},
        },
    )
    result = gate(repo)
    assert (result["decision"], result["complexity"]) == ("analyze", "complex")
    assert {"multiple_dockerfiles", "compose_multi_build", "has_image_dependencies"} <= set(codes(result))
    assert result["simpleBuild"] is None
    found = units(result)
    assert set(found) == {"web", "api", "worker"}
    assert found["api"]["rootDirectory"] == "."
    assert found["api"]["dockerfilePath"] == "api/Dockerfile"
    assert found["web"]["rootDirectory"] == "web" and found["web"]["dockerfilePath"] == "Dockerfile"
    assert found["worker"]["rootDirectory"] == "worker" and found["worker"]["dockerfilePath"] == "Dockerfile"
    assert found["web"]["port"] == 80 and found["web"]["role"] == "web"
    assert found["api"]["port"] == 3000 and found["api"]["role"] == "api"
    assert found["worker"]["role"] == "worker" and found["worker"]["public"] is False
    assert found["worker"]["startCommand"] == "npm run worker"
    assert found["web"]["dependsOn"] == ["api"]
    assert found["api"]["dependsOn"] == ["postgres", "redis"]
    assert found["worker"]["dependsOn"] == ["redis"]
    api_env = {row["key"]: row for row in found["api"]["env"]}
    assert api_env["DATABASE_URL"] == {
        "key": "DATABASE_URL",
        "stage": "runtime",
        "required": True,
        "binding": {"kind": "dependency", "targetId": "postgres", "property": "url"},
    }
    assert api_env["REDIS_URL"]["required"] is True and api_env["NODE_ENV"]["required"] is False
    assert {
        "key": "VITE_API_BASE_URL",
        "stage": "build",
        "required": False,
        "binding": None,
    } in found["web"]["env"]
    worker_env = {row["key"]: row["required"] for row in found["worker"]["env"]}
    assert worker_env == {"REDIS_URL": True, "SESSION_SECRET": True}
    dependencies = {row["id"]: row for row in result["dependencies"]}
    assert set(dependencies) == {"postgres", "redis"}
    assert dependencies["postgres"]["engine"] == "postgres"
    assert dependencies["postgres"]["image"] == "postgres:16-alpine"
    assert dependencies["redis"]["evidence"][0]["path"] == "compose.yaml"
    assert result["signals"]["composeBuildServices"] == ["api", "web", "worker"]
    assert result["signals"]["composeImageServices"] == ["postgres", "redis", "tunnel"]
    # Environment values (credentials) never leave the gate.
    assert "postgresql://" not in json.dumps(result) and "u:p@" not in json.dumps(result)


def test_npm_workspaces_with_two_apps_analyze(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "package.json": {"private": True, "workspaces": ["apps/*", "packages/*"]},
            "apps/web/package.json": {
                "name": "@acme/web",
                "scripts": {"dev": "next dev", "build": "next build", "start": "next start -p 3001"},
                "dependencies": {"next": "15"},
            },
            "apps/api/package.json": {
                "name": "@acme/api",
                "scripts": {"start": "node index.js"},
                "dependencies": {"fastify": "5", "pg": "8"},
            },
            "apps/api/index.js": "app.listen({ port: process.env.PORT || 4000 })\n",
            "packages/ui/package.json": {"name": "@acme/ui", "main": "index.js", "exports": "./index.js"},
        },
    )
    result = gate(repo)
    assert result["decision"] == "analyze"
    assert codes(result) == ["workspace_multi_app"]
    assert result["signals"]["workspaceManifests"] == ["package.json"]
    found = units(result)
    assert set(found) == {"web", "api"}
    assert found["web"]["rootDirectory"] == "apps/web" and found["web"]["builder"] == "railpack"
    assert found["web"]["dockerfilePath"] is None and found["web"]["port"] == 3001
    assert found["api"]["port"] == 4000 and found["api"]["dependsOn"] == ["postgres"]
    assert result["dependencies"] == [
        {
            "id": "postgres",
            "engine": "postgres",
            "image": None,
            "port": 5432,
            "database": None,
            "user": "postgres",
            "passwordInSource": False,
            "evidence": [{"path": "apps/api/package.json", "line": 1}],
        }
    ]


def test_frontend_and_backend_roots_analyze_as_multi_language(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "frontend/package.json": {
                "scripts": {"dev": "vite", "build": "vite build"},
                "devDependencies": {"vite": "6"},
            },
            "frontend/index.html": "<html></html>",
            "backend/requirements.txt": "django\npsycopg[binary]\n",
            "backend/.env.example": "DATABASE_URL=postgres://localhost/app\nDEBUG=1\nSECRET_KEY=\n",
        },
    )
    result = gate(repo)
    assert (result["decision"], result["complexity"]) == ("analyze", "complex")
    assert codes(result) == ["multi_language_roots"]
    found = units(result)
    assert set(found) == {"frontend", "backend"}
    assert found["backend"]["builder"] == "railpack" and found["backend"]["role"] == "api"
    assert found["frontend"]["role"] == "web"
    backend_env = {row["key"]: row["required"] for row in found["backend"]["env"]}
    assert backend_env == {"DATABASE_URL": True, "DEBUG": False, "SECRET_KEY": True}
    assert found["backend"]["dependsOn"] == ["postgres"]
    port_questions = {item["unitId"] for item in result["questions"] if item["code"] == "port_unknown"}
    assert port_questions == {"frontend", "backend"}


def test_compose_single_build_with_postgres_still_skips(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "docker-compose.yml": "services:\n  app:\n    build: .\n  db:\n    image: postgres:16\n",
            "Dockerfile": "FROM python:3.12\nEXPOSE 8000\n",
            "pyproject.toml": "[project]\nname='x'\n",
        },
    )
    result = gate(repo)
    assert (result["decision"], result["complexity"]) == ("skip", "simple")
    assert codes(result) == ["single_dockerfile", "has_image_dependencies"]
    assert result["simpleBuild"] == {"builder": "dockerfile", "dockerfilePath": "Dockerfile"}
    assert result["signals"]["composeImageServices"] == ["db"]
    assert result["units"] == [] and result["dependencies"] == [] and result["questions"] == []


def test_empty_repository_is_unsupported(tmp_path):
    repo = write(tmp_path / "repo", {"README.md": "hello\n"})
    result = gate(repo)
    assert (result["decision"], result["complexity"]) == ("analyze", "unsupported")
    assert codes(result) == ["no_builder_signal"]
    assert result["units"] == [] and result["simpleBuild"] is None
    assert [item["code"] for item in result["questions"]] == ["no_builder_signal"]


def test_force_mode_analyzes_simple_repository(tmp_path):
    repo = write(tmp_path / "repo", {"Dockerfile": "FROM node:22\nEXPOSE 3000\n", "package.json": NODE_APP})
    result = gate(repo, mode="force", ai=True)
    assert (result["decision"], result["complexity"]) == ("analyze", "simple")
    assert codes(result) == ["single_dockerfile", "forced"]
    assert result["simpleBuild"] is None
    [unit] = result["units"]
    assert unit["rootDirectory"] == "." and unit["dockerfilePath"] == "Dockerfile" and unit["port"] == 3000
    assert "ai_not_configured" in [item["code"] for item in result["questions"]]
    assert result["analysis"] == {
        "engine": "static",
        "durationMs": result["analysis"]["durationMs"],
        "modelCalls": 0,
    }


def test_root_directory_scopes_the_scan(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": COMPOSE_SHOP,
            "web/Dockerfile": "FROM nginx\n",
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "worker/Dockerfile": "FROM node:22\n",
            "services/billing/Dockerfile": "FROM node:22\nEXPOSE 9000\n",
            "services/billing/package.json": NODE_APP,
        },
    )
    result = gate(repo, rootDirectory="services/billing")
    assert result["rootDirectory"] == "services/billing"
    assert result["decision"] == "skip"
    assert result["simpleBuild"] == {"builder": "dockerfile", "dockerfilePath": "Dockerfile"}
    assert result["signals"]["dockerfiles"] == ["services/billing/Dockerfile"]
    assert result["signals"]["composeFiles"] == []
    forced = gate(repo, rootDirectory="./services/billing/", mode="force")
    assert forced["rootDirectory"] == "services/billing"
    assert forced["units"][0]["rootDirectory"] == "services/billing" and forced["units"][0]["port"] == 9000


def test_excluded_and_hidden_directories_are_ignored(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "Dockerfile": "FROM node:22\n",
            "package.json": NODE_APP,
            "tests/fixtures/app/Dockerfile": "FROM node:22\n",
            "fixtures/other/Dockerfile": "FROM node:22\n",
            "examples/demo/Dockerfile": "FROM node:22\n",
            "node_modules/pkg/Dockerfile": "FROM node:22\n",
            "node_modules/pkg/package.json": NODE_APP,
            ".devcontainer/Dockerfile": "FROM node:22\n",
            "docs/site/package.json": NODE_APP,
        },
    )
    result = gate(repo)
    assert result["decision"] == "skip"
    assert result["signals"]["dockerfiles"] == ["Dockerfile"]
    assert result["signals"]["runtimeManifests"] == [{"path": "package.json", "runtime": "node"}]


def test_symlinks_are_not_followed(tmp_path):
    outside = write(tmp_path / "outside", {"Dockerfile": "FROM node:22\n", "app/package.json": NODE_APP})
    repo = write(tmp_path / "repo", {"Dockerfile": "FROM node:22\n"})
    os.symlink(outside / "app", repo / "linked")
    os.symlink(outside / "Dockerfile", repo / "Dockerfile.linked")
    result = gate(repo)
    assert result["decision"] == "skip"
    assert result["signals"]["dockerfiles"] == ["Dockerfile"]
    with pytest.raises(AnalyzerError) as error:
        gate(repo, rootDirectory="linked")
    assert error.value.code == "GATE_ROOT_DIRECTORY_NOT_FOUND"


def test_database_dockerfile_variant_counts_and_becomes_dependency(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "Dockerfile": "FROM node:22\nENV PORT=4000\n",
            "Dockerfile.mongo": "FROM mongo:8\n",
            "package.json": NODE_APP,
        },
    )
    result = gate(repo)
    assert result["decision"] == "analyze" and codes(result) == ["multiple_dockerfiles"]
    assert [unit["id"] for unit in result["units"]] == ["app"]
    assert result["units"][0]["port"] == 4000
    assert result["dependencies"][0]["engine"] == "mongodb"
    assert "dependency_built_from_dockerfile" in [item["code"] for item in result["questions"]]


def test_procfile_with_two_processes_analyzes(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "requirements.txt": "flask\n",
            "Procfile": "web: gunicorn app:app --bind 0.0.0.0:8000\nworker: rq worker\n",
        },
    )
    result = gate(repo)
    assert codes(result) == ["procfile_multi_process"]
    found = units(result)
    assert found["app"]["startCommand"].startswith("gunicorn") and found["app"]["port"] == 8000
    assert found["worker"]["role"] == "worker" and found["worker"]["startCommand"] == "rq worker"


def test_single_dockerfile_in_subdirectory_needs_review(tmp_path):
    repo = write(tmp_path / "repo", {"server/Dockerfile": "FROM node:22\nEXPOSE 3000\n"})
    result = gate(repo)
    assert result["decision"] == "analyze"
    assert codes(result) == ["single_unit_in_subdirectory"]
    assert result["units"][0]["rootDirectory"] == "server"


@pytest.mark.parametrize(
    "patch",
    [
        {"schemaVersion": "iris.analysis-gate-request.v0"},
        {"sourceRoot": "relative/path"},
        {"sourceRoot": "/definitely/not/here"},
        {"rootDirectory": "../outside"},
        {"rootDirectory": "/abs"},
        {"rootDirectory": "a/../../b"},
        {"rootDirectory": "missing"},
        {"sourceSha": "ABC"},
        {"sourceSha": "G" * 40},
        {"mode": "fast"},
        {"ai": "yes"},
        {"unexpected": 1},
    ],
)
def test_invalid_requests_are_rejected(tmp_path, patch):
    repo = write(tmp_path / "repo", {"Dockerfile": "FROM node:22\n"})
    request = {"schemaVersion": "iris.analysis-gate-request.v1", "sourceRoot": str(repo), **patch}
    with pytest.raises(AnalyzerError):
        run_gate(request)


def run_cli(payload: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "iris_analyzer.gate.cli", "--request-stdin"],
        input=payload,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_cli_round_trip_and_error_exit_codes(tmp_path):
    repo = write(tmp_path / "repo", {"package.json": NODE_APP})
    request = {
        "schemaVersion": "iris.analysis-gate-request.v1",
        "sourceRoot": str(repo),
        "rootDirectory": ".",
        "sourceSha": None,
        "mode": "auto",
        "ai": False,
    }
    Draft202012Validator(REQUEST_SCHEMA).validate(request)
    completed = run_cli(json.dumps(request).encode())
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    Draft202012Validator(RESULT_SCHEMA).validate(result)
    assert result["decision"] == "skip"

    for payload in (b"not json", json.dumps({**request, "rootDirectory": "../x"}).encode(), b"x" * 70000):
        failed = run_cli(payload)
        assert failed.returncode == 2
        assert failed.stdout == b""
        error = json.loads(failed.stderr)["error"]
        assert error["code"] == "GATE_REQUEST_INVALID"
        assert str(repo) not in failed.stderr.decode()
    missing = run_cli(json.dumps({**request, "sourceRoot": str(tmp_path / "nope")}).encode())
    assert missing.returncode == 2 and json.loads(missing.stderr)["error"]["code"] == "GATE_SOURCE_NOT_FOUND"


# -- Phase 2: env bindings, host aliases, dependency profiles -------------------

LINKS = Path(__file__).resolve().parent.parent / "fixtures" / "gate-links"
HARDCODED_SECRETS = ("s3cr3t-pg-pass", "hunter2-hardcoded", "redis-secret-xyz")


def test_env_bindings_from_compose_values_siblings_and_key_names():
    result = gate(LINKS)
    found = units(result)
    api = {row["key"]: row["binding"] for row in found["api"]["env"]}
    assert api["DATABASE_URL"] == {"kind": "dependency", "targetId": "postgres", "property": "url"}
    # Compose service name `cache` differs from the engine; the host decides the target.
    assert api["REDIS_URL"] == {"kind": "dependency", "targetId": "cache", "property": "url"}
    assert api["MONGODB_URI"] == {"kind": "dependency", "targetId": "mongo", "property": "url"}
    assert api["PORT"] is None
    worker = {row["key"]: row["binding"] for row in found["worker"]["env"]}
    assert worker["DB_HOST"] == {"kind": "dependency", "targetId": "postgres", "property": "host"}
    assert worker["DB_PORT"] == {"kind": "dependency", "targetId": "postgres", "property": "port"}
    assert worker["DB_USER"] == {"kind": "dependency", "targetId": "postgres", "property": "user"}
    assert worker["API_URL"] == {"kind": "unit", "targetId": "api", "property": "url"}
    # `${REDIS_URL}` carries no host, so the key name picks the only redis dependency.
    assert worker["REDIS_URL"] == {"kind": "dependency", "targetId": "cache", "property": "url"}
    assert worker["SENTRY_DSN"] is None


def test_external_database_url_is_not_bound_and_env_example_keys_use_heuristics():
    found = units(gate(LINKS))
    reporter = {row["key"]: row["binding"] for row in found["reporter"]["env"]}
    assert reporter["DATABASE_URL"] is None  # points at an external host
    assert reporter["REDIS_URL"] == {"kind": "dependency", "targetId": "cache", "property": "url"}
    assert reporter["SESSION_SECRET"] is None
    assert found["reporter"]["hostAliases"] == []


def test_host_aliases_from_compose_nginx_and_source_literals():
    found = units(gate(LINKS))
    web = found["web"]["hostAliases"]
    assert web == [
        {
            "host": "api",
            "port": 3000,
            "targetId": "api",
            "evidence": [
                {"path": "web/nginx/default.conf", "line": 2},
                {"path": "web/nginx/default.conf", "line": 7},
            ],
        }
    ]
    api = {(row["host"], row["port"]): row for row in found["api"]["hostAliases"]}
    assert set(api) == {("postgres", 5432), ("cache", 6379), ("mongo", 27017)}
    assert api[("postgres", 5432)]["targetId"] == "postgres"
    assert api[("postgres", 5432)]["evidence"] == [{"path": "compose.yaml", "line": 10}]
    worker = {row["host"]: row for row in found["worker"]["hostAliases"]}
    assert worker["api"]["port"] == 3000 and worker["api"]["targetId"] == "api"
    assert {"path": "compose.yaml", "line": 20} in worker["api"]["evidence"]
    assert {"path": "worker/src/index.js", "line": 1} in worker["api"]["evidence"]
    assert worker["postgres"]["port"] == 5432
    assert "localhost" not in worker and "unknown-host" not in {r["host"] for r in web}
    assert "api" in found["worker"]["dependsOn"]


def test_dependency_profile_and_password_flag():
    result = gate(LINKS)
    dependencies = {row["id"]: row for row in result["dependencies"]}
    assert dependencies["postgres"] | {"evidence": None} == {
        "id": "postgres",
        "engine": "postgres",
        "image": "postgres:16-alpine",
        "port": 5432,
        "database": "shop",
        "user": "app",
        "passwordInSource": True,
        "evidence": None,
    }
    assert dependencies["cache"]["port"] == 6379 and dependencies["cache"]["passwordInSource"] is True
    mongo = dependencies["mongo"]
    assert (mongo["port"], mongo["passwordInSource"]) == (27017, False)
    assert mongo["database"] == "audit" and mongo["user"] is None


def test_hardcoded_passwords_never_leave_the_gate():
    request = {"schemaVersion": "iris.analysis-gate-request.v1", "sourceRoot": str(LINKS)}
    completed = run_cli(json.dumps(request).encode())
    assert completed.returncode == 0
    for secret in HARDCODED_SECRETS:
        assert secret not in completed.stdout.decode() + completed.stderr.decode()


def test_results_without_phase2_fields_still_validate():
    result = gate(LINKS)
    for unit in result["units"]:
        unit.pop("hostAliases")
        for row in unit["env"]:
            row.pop("binding")
    for dependency in result["dependencies"]:
        for key in ("port", "database", "user", "passwordInSource"):
            dependency.pop(key)
    Draft202012Validator(RESULT_SCHEMA).validate(result)


def test_unit_binding_cannot_use_database_properties():
    result = gate(LINKS)
    row = next(row for row in result["units"][0]["env"])
    row["binding"] = {"kind": "unit", "targetId": "api", "property": "password"}
    assert list(Draft202012Validator(RESULT_SCHEMA).iter_errors(result))
