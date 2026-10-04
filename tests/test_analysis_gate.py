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
    binding = api_env["DATABASE_URL"].pop("binding")
    assert api_env["DATABASE_URL"] == {"key": "DATABASE_URL", "stage": "runtime", "required": True}
    assert binding["kind"] == "dependency" and binding["targetId"] == "postgres"
    assert binding["property"] == "url" and binding["hasCredentials"] is True
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
    assert "custom_database_image" in [item["code"] for item in result["questions"]]


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
    assert api["DATABASE_URL"] == {
        "kind": "dependency", "targetId": "postgres", "property": "url",
        "scheme": "postgres", "urlSuffix": "/shop", "hasCredentials": True,
        "user": "app", "passwordSecretId": None,
    }
    # Compose service name `cache` differs from the engine; the host decides the target.
    assert api["REDIS_URL"] == {
        "kind": "dependency", "targetId": "cache", "property": "url",
        "scheme": "redis", "urlSuffix": "", "hasCredentials": False,
    }
    assert api["MONGODB_URI"] == {
        "kind": "dependency", "targetId": "mongo", "property": "url",
        "scheme": "mongodb", "urlSuffix": "/audit", "hasCredentials": False,
    }
    assert api["PORT"] is None
    worker = {row["key"]: row["binding"] for row in found["worker"]["env"]}
    assert worker["DB_HOST"] == {"kind": "dependency", "targetId": "postgres", "property": "host"}
    assert worker["DB_PORT"] == {"kind": "dependency", "targetId": "postgres", "property": "port"}
    assert worker["DB_USER"] == {"kind": "dependency", "targetId": "postgres", "property": "user"}
    assert worker["API_URL"] == {
        "kind": "unit", "targetId": "api", "property": "url",
        "scheme": "http", "urlSuffix": "", "hasCredentials": False,
    }
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


# -- Contract F: database init scripts -------------------------------------------

def init_repo(tmp_path: Path, compose: str, files: dict[str, object]) -> Path:
    return write(
        tmp_path / "repo",
        {"compose.yaml": compose, "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n", **files},
    )


def init_scripts(result: dict, dependency: str = "db") -> list[dict]:
    return {row["id"]: row for row in result["dependencies"]}[dependency].get("initScripts", [])


def sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def test_init_scripts_from_directory_mount_sorted_and_non_recursive(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - pgdata:/var/lib/postgresql/data\n      - ./db:/docker-entrypoint-initdb.d:ro\n",
        {
            "db/02-seed.sql": "INSERT INTO t VALUES (1);\n",
            "db/01-schema.sql": "CREATE TABLE t (id int);\n",
            "db/notes.md": "ignored",
            "db/.hidden.sql": "x",
            "db/nested/03.sql": "SELECT 1;",
        },
    )
    result = gate(repo, mode="force")
    scripts = init_scripts(result)
    assert [(s["path"], s["kind"], s["order"], s["supported"]) for s in scripts] == [
        ("db/01-schema.sql", "sql", 0, True),
        ("db/02-seed.sql", "sql", 1, True),
    ]
    assert scripts[0]["sha256"] == sha("CREATE TABLE t (id int);\n")
    assert scripts[0]["size"] == len("CREATE TABLE t (id int);\n")
    assert not [q for q in result["questions"] if q["code"].startswith("init_script")]
    assert "CREATE TABLE" not in json.dumps(result)


def test_init_scripts_from_file_mounts_use_container_names(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - ./db/seed.sql:/docker-entrypoint-initdb.d/002-seed.sql:ro\n"
        "      - ./db/schema.sql:/docker-entrypoint-initdb.d/001-schema.sql:ro\n"
        "      - ./db/other.sql:/elsewhere/other.sql\n",
        {"db/seed.sql": "SELECT 2;", "db/schema.sql": "SELECT 1;", "db/other.sql": "SELECT 3;"},
    )
    scripts = init_scripts(gate(repo, mode="force"))
    assert [(s["path"], s["order"]) for s in scripts] == [("db/schema.sql", 0), ("db/seed.sql", 1)]


def test_init_scripts_long_syntax_and_gzip(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: mysql:8\n    volumes:\n"
        "      - type: bind\n        source: ./sql\n        target: /docker-entrypoint-initdb.d\n"
        "      - type: volume\n        source: data\n        target: /var/lib/mysql\n",
        {"sql/a.sql.gz": "gzbytes", "sql/b.sql": "SELECT 1;"},
    )
    scripts = init_scripts(gate(repo, mode="force"))
    assert [(s["path"], s["kind"]) for s in scripts] == [("sql/a.sql.gz", "sql.gz"), ("sql/b.sql", "sql")]


def test_init_scripts_ignore_symlinks_and_outside_paths(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - ./db:/docker-entrypoint-initdb.d\n"
        "      - ../outside.sql:/docker-entrypoint-initdb.d/9-out.sql\n"
        "      - ./linked.sql:/docker-entrypoint-initdb.d/8-linked.sql\n"
        "      - /etc/passwd:/docker-entrypoint-initdb.d/7-abs.sql\n",
        {"db/real.sql": "SELECT 1;"},
    )
    (tmp_path / "outside.sql").write_text("SELECT 'out';")
    (repo / "secret.sql").write_text("SELECT 'secret';")
    (repo / "db" / "link.sql").symlink_to(repo / "secret.sql")
    (repo / "linked.sql").symlink_to(repo / "secret.sql")
    scripts = init_scripts(gate(repo, mode="force"))
    assert [s["path"] for s in scripts] == ["db/real.sql"]


def test_init_scripts_outside_requested_root_are_ignored(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "app/compose.yaml": "services:\n  api:\n    build: .\n  db:\n    image: postgres:16\n    volumes:\n"
            "      - ../db:/docker-entrypoint-initdb.d\n",
            "app/Dockerfile": "FROM node:22\n",
            "db/schema.sql": "SELECT 1;",
        },
    )
    assert init_scripts(gate(repo, rootDirectory="app", mode="force")) == []


def test_shell_init_script_is_unsupported_with_question(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - ./db:/docker-entrypoint-initdb.d\n",
        {"db/01.sql": "SELECT 1;", "db/02-setup.sh": "#!/bin/sh\necho hi\n"},
    )
    result = gate(repo, mode="force")
    scripts = init_scripts(result)
    assert [(s["path"], s["kind"], s["supported"]) for s in scripts] == [
        ("db/01.sql", "sql", True),
        ("db/02-setup.sh", "sh", False),
    ]
    questions = [q for q in result["questions"] if q["code"] == "init_script_unsupported"]
    assert len(questions) == 1 and "db/02-setup.sh" in questions[0]["message"]
    assert "echo hi" not in json.dumps(result)


def test_oversize_init_script_is_unsupported(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - ./db:/docker-entrypoint-initdb.d\n",
        {"db/big.sql": "-- " + "x" * (1024 * 1024), "db/small.sql": "SELECT 1;"},
    )
    result = gate(repo, mode="force")
    scripts = {s["path"]: s for s in init_scripts(result)}
    assert scripts["db/big.sql"]["supported"] is False and scripts["db/big.sql"]["size"] > 1024 * 1024
    assert scripts["db/small.sql"]["supported"] is True
    assert [q["code"] for q in result["questions"] if q["code"].startswith("init_script")] == [
        "init_script_too_large"
    ]


def test_total_init_script_size_over_limit_marks_all_unsupported(tmp_path):
    chunk = "-- " + "x" * (600 * 1024)
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  db:\n    image: postgres:16\n    volumes:\n"
        "      - ./db:/docker-entrypoint-initdb.d\n",
        {"db/1.sql": chunk, "db/2.sql": chunk},
    )
    result = gate(repo, mode="force")
    assert [s["supported"] for s in init_scripts(result)] == [False, False]
    assert len([q for q in result["questions"] if q["code"] == "init_script_too_large"]) == 2


def test_mongo_accepts_js_and_ignores_sql_while_redis_has_none(tmp_path):
    repo = init_repo(
        tmp_path,
        "services:\n  api:\n    build: ./api\n  mongo:\n    image: mongo:8\n    volumes:\n"
        "      - ./docker/init.js:/docker-entrypoint-initdb.d/init.js:ro\n"
        "      - ./docker/x.sql:/docker-entrypoint-initdb.d/x.sql:ro\n"
        "  cache:\n    image: redis:7\n    volumes:\n      - ./db:/docker-entrypoint-initdb.d\n",
        {"docker/init.js": "db.a.insert({})", "docker/x.sql": "SELECT 1;", "db/a.sql": "SELECT 1;"},
    )
    result = gate(repo, mode="force")
    assert [(s["path"], s["kind"]) for s in init_scripts(result, "mongo")] == [("docker/init.js", "js")]
    redis = {row["id"]: row for row in result["dependencies"]}["cache"]
    assert "initScripts" not in redis


def test_init_scripts_for_database_built_from_dockerfile(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: ./api\n  mongo:\n    build:\n      context: .\n"
            "      dockerfile: Dockerfile.mongo\n    volumes:\n"
            "      - ./docker/init.js:/docker-entrypoint-initdb.d/init.js:ro\n",
            "api/Dockerfile": "FROM node:22\n",
            "Dockerfile.mongo": "FROM mongo:8\n",
            "docker/init.js": "db.a.insert({})",
        },
    )
    assert [s["path"] for s in init_scripts(gate(repo), "mongo")] == ["docker/init.js"]


# -- build.target, build.args, URL suffix -----------------------------------------

STAGED = (
    "FROM node:22 AS base\nWORKDIR /app\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n"
    "FROM base AS api\nRUN npm ci\n"
    "FROM node:22 AS builder\nRUN npm run build\n"
    "FROM nginx:1.27 AS web\nEXPOSE 80\n"
)


def staged_repo(tmp_path: Path, compose: str) -> Path:
    return write(
        tmp_path / "repo",
        {
            "compose.yaml": compose,
            "Dockerfile": STAGED,
            "other/Dockerfile": "FROM node:22\nEXPOSE 9000\n",
        },
    )


def test_build_target_selects_stage_port_role_and_inherits_expose(tmp_path):
    repo = staged_repo(
        tmp_path,
        "services:\n  api:\n    build: {context: ., target: api}\n"
        "  site:\n    build: {context: ., target: web}\n"
        "  last:\n    build: {context: other}\n",
    )
    result = gate(repo)
    found = units(result)
    assert found["api"]["buildTarget"] == "api" and found["api"]["port"] == 3000
    assert found["api"]["role"] == "api"
    assert found["site"]["buildTarget"] == "web" and found["site"]["port"] == 80
    assert found["site"]["role"] == "web"
    assert found["last"]["buildTarget"] is None and found["last"]["port"] == 9000
    assert "build_target_not_found" not in codes_of_questions(result)


def codes_of_questions(result: dict) -> list[str]:
    return [question["code"] for question in result["questions"]]


def test_derived_stage_expose_wins_over_inherited_and_default_is_last_stage(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  a:\n    build: {context: ., target: child}\n  b:\n    build: ./b\n",
            "Dockerfile": "FROM node:22 AS parent\nEXPOSE 3000\nFROM parent AS child\nEXPOSE 4000\n",
            "b/Dockerfile": "FROM node:22 AS one\nEXPOSE 1111\nFROM node:22 AS two\nEXPOSE 2222\n",
        },
    )
    found = units(gate(repo))
    assert found["a"]["port"] == 4000
    assert found["b"]["port"] == 2222 and found["b"]["buildTarget"] is None


def test_missing_build_target_asks_and_leaves_port_null(tmp_path):
    repo = staged_repo(
        tmp_path,
        "services:\n  api:\n    build: {context: ., target: nope}\n  w:\n    build: ./other\n",
    )
    result = gate(repo)
    api = units(result)["api"]
    assert api["port"] is None and api["buildTarget"] == "nope"
    questions = [q for q in result["questions"] if q["unitId"] == "api"]
    assert [q["code"] for q in questions] == ["build_target_not_found"]


def test_build_args_report_keys_only(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build:\n      context: .\n      args:\n"
            "        NPM_TOKEN: tok-secret-value\n"
            "  w:\n    build:\n      context: ./other\n      args: [REGION=eu-secret, FLAG]\n",
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "other/Dockerfile": "FROM node:22\nEXPOSE 3001\n",
        },
    )
    result = gate(repo)
    found = units(result)
    assert found["api"]["buildArgs"] == ["NPM_TOKEN"]
    assert found["w"]["buildArgs"] == ["FLAG", "REGION"]
    present = [q for q in result["questions"] if q["code"] == "build_args_present"]
    assert {q["unitId"] for q in present} == {"api", "w"}
    dump = json.dumps(result)
    assert "tok-secret-value" not in dump and "eu-secret" not in dump


URL_COMPOSE = """\
services:
  api:
    build: ./api
    environment:
      API_URL: http://web:8080/api/v1?tenant=demo#top
      SELF_PG: postgresql+asyncpg://u:pw-leak-1@db:5432/app?sslmode=disable
      PLAIN: http://web:8080
      SRV: mongodb+srv://web/app
      TOKENED: http://web:8080/x?token=abc
      AUTHSRC: mongodb://u:p@db:27017/app?authSource=admin&retryWrites=true
      UNRES: http://web:8080/${PATH_PART}
  web:
    build: ./web
  db:
    image: postgres:16
"""


def test_url_binding_keeps_scheme_and_exact_suffix_without_credentials(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": URL_COMPOSE,
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "web/Dockerfile": "FROM node:22\nEXPOSE 8080\n",
        },
    )
    result = gate(repo)
    env = {row["key"]: row["binding"] for row in units(result)["api"]["env"]}
    assert env["API_URL"] == {
        "kind": "unit", "targetId": "web", "property": "url",
        "scheme": "http", "urlSuffix": "/api/v1?tenant=demo#top", "hasCredentials": False,
    }
    assert env["SELF_PG"] == {
        "kind": "dependency", "targetId": "db", "property": "url",
        "scheme": "postgresql+asyncpg", "urlSuffix": "/app?sslmode=disable", "hasCredentials": True,
        "user": "u", "passwordSecretId": None,
    }
    assert env["PLAIN"]["urlSuffix"] == ""
    assert env["AUTHSRC"]["urlSuffix"] == "/app?authSource=admin&retryWrites=true"
    assert env["AUTHSRC"]["scheme"] == "mongodb" and env["AUTHSRC"]["hasCredentials"] is True
    assert env["SRV"] is None and env["TOKENED"] is None and env["UNRES"] is None
    assert "pw-leak-1" not in json.dumps(result)


# -- 계약 G: secrets, unit-consumed env, URL user ---------------------------------

TEMP_LOG_COMPOSE = """\
services:
  app:
    build: .
    ports: ['127.0.0.1:${APP_PORT:-8080}:4000']
    environment:
      NODE_ENV: production
      PUBLIC_URL: ${PUBLIC_URL:-http://localhost:8080}
      SESSION_SECRET: ${SESSION_SECRET:?Run make init}
      MONGO_URI: mongodb://archlog:${MONGO_APP_PASSWORD:?Run make init}@mongo:27017/archlog?authSource=archlog
    depends_on:
      mongo: {condition: service_healthy}
  mongo:
    image: temp-log-mongo:local
    build: {context: ., dockerfile: Dockerfile.mongo}
    command: [mongod, --bind_ip_all, --auth]
    environment:
      MONGO_INITDB_ROOT_USERNAME: root
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_ROOT_PASSWORD:?Run make init}
      MONGO_APP_PASSWORD: ${MONGO_APP_PASSWORD:?Run make init}
      HOME: /tmp
    volumes:
      - mongo-data:/data/db
      - ./docker/mongo-init.js:/docker-entrypoint-initdb.d/init.js:ro
volumes:
  mongo-data:
"""
TEMP_LOG_EXAMPLE = (
    "APP_PORT=8080\nSESSION_SECRET=generate-a-random-secret-with-make-init\n"
    "MONGO_ROOT_PASSWORD=generate-with-make-init\nMONGO_APP_PASSWORD=generate-with-make-init\n"
)


def temp_log_repo(tmp_path: Path) -> Path:
    return write(
        tmp_path / "repo",
        {
            "compose.yaml": TEMP_LOG_COMPOSE,
            "Dockerfile": "FROM node:22\nEXPOSE 4000\n",
            "Dockerfile.mongo": "FROM mongo:8.0.32\n",
            ".env.example": TEMP_LOG_EXAMPLE,
            "docker/mongo-init.js": "db.createUser({})",
            "server/index.js": "const s = process.env.SESSION_SECRET; app.listen(4000)\n",
            "scripts/init.py": "open('.env','w').write('MONGO_ROOT_PASSWORD=' + secrets.token_hex())\n",
        },
    )


def secret_map(result: dict) -> dict[str, dict]:
    return {item["id"]: item for item in result["secrets"]}


def test_temp_log_secrets_generated_shared_and_platform_managed(tmp_path):
    result = gate(temp_log_repo(tmp_path))
    secrets = secret_map(result)
    assert set(secrets) == {"MONGO_APP_PASSWORD", "MONGO_ROOT_PASSWORD", "SESSION_SECRET"}
    app_pw = secrets["MONGO_APP_PASSWORD"]
    assert app_pw["generate"] == "random" and "platformManaged" not in app_pw
    assert app_pw["consumers"] == [
        {"kind": "dependency", "targetId": "mongo", "key": "MONGO_APP_PASSWORD", "via": "env"},
        {"kind": "unit", "targetId": "app", "key": "MONGO_APP_PASSWORD", "via": "url_password"},
    ]
    assert app_pw["evidence"] and all(item["path"] == "compose.yaml" for item in app_pw["evidence"])
    session = secrets["SESSION_SECRET"]
    assert session["generate"] == "random"
    assert session["consumers"] == [{"kind": "unit", "targetId": "app", "key": "SESSION_SECRET", "via": "env"}]
    root = secrets["MONGO_ROOT_PASSWORD"]
    assert root["generate"] is None
    assert root["platformManaged"] == {"dependencyId": "mongo", "property": "password"}
    assert root["consumers"] == [
        {"kind": "dependency", "targetId": "mongo", "key": "MONGO_INITDB_ROOT_PASSWORD", "via": "env"}
    ]

    app = units(result)["app"]
    keys = {row["key"] for row in app["env"]}
    assert "MONGO_ROOT_PASSWORD" not in keys  # only the mongo service consumes it
    assert {"SESSION_SECRET", "MONGO_URI", "NODE_ENV"} <= keys
    row = {row["key"]: row for row in app["env"]}
    assert row["SESSION_SECRET"]["secretId"] == "SESSION_SECRET"
    assert row["MONGO_URI"]["binding"] == {
        "kind": "dependency", "targetId": "mongo", "property": "url", "scheme": "mongodb",
        "urlSuffix": "/archlog?authSource=archlog", "hasCredentials": True,
        "user": "archlog", "passwordSecretId": "MONGO_APP_PASSWORD",
    }
    mongo = {row["id"]: row for row in result["dependencies"]}["mongo"]
    assert mongo["env"] == [{"key": "MONGO_APP_PASSWORD", "secretId": "MONGO_APP_PASSWORD"}]
    question = next(item for item in result["questions"] if item["code"] == "custom_database_image")
    assert "공식 이미지" in question["message"] and "만들지 않으므로" not in question["message"]


def test_root_env_example_alone_does_not_add_keys_but_source_reads_and_env_file_do(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: ./api\n    env_file: [./api/api.env]\n"
            "  worker:\n    build: ./worker\n",
            ".env.example": "DB_PASSWORD=\nREAD_ME=1\nUNUSED_TOKEN=\nFROM_FILE=\n",
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "api/api.env": "FROM_FILE=x\n",
            "worker/Dockerfile": "FROM node:22\n",
            "worker/index.js": "const a = process.env.READ_ME;\n",
        },
    )
    found = units(gate(repo))
    # api reads FROM_FILE through env_file; neither service gets DB_PASSWORD/UNUSED_TOKEN from the root example.
    assert {row["key"] for row in found["api"]["env"]} == {"FROM_FILE"}
    assert {row["key"] for row in found["worker"]["env"]} == set()

    write(repo, {"api/.env.example": "FROM_FILE=\nNOT_READ=\nREAD_ME=\n", "api/src/a.js": "const {READ_ME} = process.env;\n"})
    api = {row["key"] for row in units(gate(repo))["api"]["env"]}
    assert api == {"FROM_FILE", "READ_ME"}


def test_empty_env_example_secrets_and_generate_rules(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: ./api\n"
            "    environment:\n      STRIPE_API_KEY: ${STRIPE_API_KEY}\n      SHARED_TOKEN: ${SHARED_TOKEN}\n"
            "      LABEL: ${LABEL:-x}\n      PLAIN: ${PLAIN}\n"
            "  worker:\n    build: ./worker\n    environment:\n      SHARED_TOKEN: ${SHARED_TOKEN}\n"
            "      OTHER: ${PLAIN}\n",
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "worker/Dockerfile": "FROM node:22\nEXPOSE 3001\n",
            "worker/.env.example": "JWT_SECRET=\nDEBUG=\n",
            "worker/index.js": "process.env.JWT_SECRET; process.env.DEBUG\n",
        },
    )
    result = gate(repo)
    secrets = secret_map(result)
    assert secrets["STRIPE_API_KEY"]["generate"] is None  # external credential, user supplied
    assert secrets["SHARED_TOKEN"]["generate"] is None
    assert [c["targetId"] for c in secrets["SHARED_TOKEN"]["consumers"]] == ["api", "worker"]
    assert secrets["PLAIN"]["generate"] is None  # shared by two services, not a secret name
    assert secrets["JWT_SECRET"]["generate"] == "random"
    assert secrets["JWT_SECRET"]["evidence"] == [{"path": "worker/.env.example", "line": 1}]
    assert "LABEL" not in secrets and "DEBUG" not in secrets
    worker = {row["key"]: row for row in units(result)["worker"]["env"]}
    assert worker["JWT_SECRET"]["secretId"] == "JWT_SECRET"


def test_secrets_never_leak_values_or_defaults_on_stdout(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: ./api\n    environment:\n"
            "      API_SECRET: ${API_SECRET:-leak-default-1}\n"
            "      DB: mongodb://literal-user:leak-literal-2@db:27017/x\n"
            "      DB2: mongodb://${DB_USER:-leak-user-3}:${DB_PASS:-leak-default-4}@db:27017/y\n"
            "      API_TOKEN: ${API_TOKEN:?leak-message-5}\n"
            "  db:\n    image: mongo:7\n    environment:\n"
            "      MONGO_INITDB_ROOT_PASSWORD: leak-literal-6\n      INIT_PW: ${INIT_PW:?x}\n",
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "api/.env.example": "SESSION_SECRET=leak-example-7\nTOKEN_X=\n",
            "api/index.js": "process.env.SESSION_SECRET; process.env.TOKEN_X\n",
        },
    )
    request = {"schemaVersion": "iris.analysis-gate-request.v1", "sourceRoot": str(repo)}
    completed = run_cli(json.dumps(request).encode())
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout.decode()
    for leaked in ("leak-default-1", "leak-literal-2", "leak-user-3", "leak-default-4", "leak-message-5",
                   "leak-literal-6", "leak-example-7"):
        assert leaked not in output
    result = json.loads(output)
    Draft202012Validator(RESULT_SCHEMA).validate(result)
    assert "API_SECRET" not in secret_map(result)  # has a default, nothing to generate
    env = {row["key"]: row for row in units(result)["api"]["env"]}
    assert env["DB"]["binding"]["user"] == "literal-user" and env["DB"]["binding"]["passwordSecretId"] is None
    assert env["DB2"]["binding"]["user"] is None and env["DB2"]["binding"]["passwordSecretId"] is None
    secrets = secret_map(result)
    assert "SESSION_SECRET" not in secrets  # example value is not empty, nothing references it
    assert secrets["TOKEN_X"]["generate"] is None and "INIT_PW" not in secrets
    assert {row["id"]: row for row in result["dependencies"]}["db"]["env"] == [{"key": "INIT_PW"}]
    assert secrets["API_TOKEN"]["consumers"] == [{"kind": "unit", "targetId": "api", "key": "API_TOKEN", "via": "env"}]


def test_results_without_secrets_fields_still_validate(tmp_path):
    result = gate(temp_log_repo(tmp_path))
    legacy = json.loads(json.dumps(result))
    legacy.pop("secrets")
    for unit in legacy["units"]:
        for row in unit["env"]:
            row.pop("secretId", None)
            if row["binding"]:
                row["binding"].pop("user", None)
                row["binding"].pop("passwordSecretId", None)
    for dep in legacy["dependencies"]:
        dep.pop("env", None)
    Draft202012Validator(RESULT_SCHEMA).validate(legacy)
