"""DB provisioning decisions use source/configuration evidence, never declarations alone."""

import json

import pytest
from test_analysis_gate import gate, units, write


def app(tmp_path, source, *, packages=None, example="", filename="server.js"):
    return write(
        tmp_path / "repo",
        {
            "package.json": {"scripts": {"start": f"node {filename}"}, "dependencies": packages or {}},
            filename: source,
            ".env.example": example,
        },
    )


def test_library_declaration_and_comment_do_not_provision(tmp_path):
    result = gate(
        app(tmp_path, "// new Pool({connectionString: process.env.DATABASE_URL})\n", packages={"pg": "8"})
    )
    assert result["dependencies"] == []
    assert any(q["code"] == "database_usage_unconfirmed" for q in result["questions"])


@pytest.mark.parametrize(
    ("source", "engine", "key"),
    [
        (
            "import { Pool as DB } from 'pg'; new DB({connectionString: process.env.DATABASE_URL});",
            "postgres",
            "DATABASE_URL",
        ),
        (
            "const {MongoClient} = require('mongodb'); new MongoClient(process.env.MONGO_URI);",
            "mongodb",
            "MONGO_URI",
        ),
        (
            "import mongoose from 'mongoose'; mongoose.connect(process.env.MONGODB_URI);",
            "mongodb",
            "MONGODB_URI",
        ),
        (
            "import {createClient} from 'redis'; createClient({url:process.env.REDIS_URL});",
            "redis",
            "REDIS_URL",
        ),
        (
            "import mysql from 'mysql2/promise'; mysql.createPool(process.env.MYSQL_URL);",
            "mysql",
            "MYSQL_URL",
        ),
    ],
)
def test_connection_configuration_creates_candidate_and_binding(tmp_path, source, engine, key):
    result = gate(app(tmp_path, source))
    assert result["decision"] == "analyze"
    assert [d["engine"] for d in result["dependencies"]] == [engine]
    env = {e["key"]: e for e in result["units"][0]["env"]}
    assert env[key]["binding"]["targetId"] == result["dependencies"][0]["id"]
    assert result["executionAuthorized"] is False


@pytest.mark.parametrize(
    "source",
    [
        "import {Pool} from 'pg'; function test(Pool) {new Pool({connectionString:process.env.DATABASE_URL});}",
        "import {Pool} from 'pg'; Pool = fake; new Pool({connectionString:process.env.DATABASE_URL});",
        'const text = "new Pool({connectionString:process.env.DATABASE_URL})";',
    ],
)
def test_shadowed_reassigned_and_string_calls_do_not_create_db(tmp_path, source):
    assert gate(app(tmp_path, source), mode="force")["dependencies"] == []


@pytest.mark.parametrize("compose", [False, True])
def test_external_url_never_creates_or_binds_local_database(tmp_path, compose):
    repo = app(
        tmp_path,
        "import {Pool} from 'pg'; new Pool({connectionString:process.env.DATABASE_URL});",
        packages={"pg": "8"},
        example="DATABASE_URL=postgres://u:secret-marker@db.example.net/prod\n",
    )
    if compose:
        write(
            repo,
            {
                "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
                "compose.yaml": "services:\n  app:\n    build: .\n    environment:\n      DATABASE_URL: postgres://u:secret-marker@db.example.net/prod\n",
            },
        )
    result = gate(repo)
    assert result["dependencies"] == []
    env = {row["key"]: row for row in result["units"][0]["env"]}
    assert env["DATABASE_URL"]["binding"] is None
    assert "secret-marker" not in json.dumps(result)
    assert any(q["code"] == "database_external_connection" for q in result["questions"])


def test_two_identical_postgres_images_remain_separate_and_bind_exact_host(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: .\n    environment:\n      DATABASE_URL: postgres://u:p@orders:5432/shop\n  orders:\n    image: postgres:16\n  audit:\n    image: postgres:16\n",
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "server.js": "import {Pool} from 'pg'; new Pool({connectionString:process.env.DATABASE_URL});",
        },
    )
    result = gate(repo)
    assert {d["id"] for d in result["dependencies"]} == {"orders", "audit"}
    assert units(result)["api"]["dependsOn"] == ["orders"]
    assert units(result)["api"]["env"][0]["binding"]["targetId"] == "orders"
    assert not any(q["code"] == "database_external_connection" for q in result["questions"])


def test_same_engine_without_target_stays_unbound(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: .\n    environment:\n      DATABASE_URL: ${DATABASE_URL}\n  orders:\n    image: postgres:16\n  audit:\n    image: postgres:16\n",
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "server.js": "import {Pool} from 'pg'; new Pool({connectionString:process.env.DATABASE_URL});",
        },
    )
    result = gate(repo)
    assert units(result)["api"]["dependsOn"] == []
    assert units(result)["api"]["env"][0]["binding"] is None
    assert any(q["code"] == "database_target_ambiguous" for q in result["questions"])


def test_local_url_does_not_select_first_of_two_databases(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: .\n    environment:\n      DATABASE_URL: postgres://localhost/app\n  orders:\n    image: postgres:16\n  audit:\n    image: postgres:16\n",
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
        },
    )
    result = gate(repo)
    assert {d["id"] for d in result["dependencies"]} == {"orders", "audit"}
    assert units(result)["api"]["dependsOn"] == []
    assert units(result)["api"]["env"][0]["binding"] is None
    assert any(q["code"] == "database_target_ambiguous" for q in result["questions"])


def test_external_host_key_is_not_bound_to_existing_local_db(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: .\n    environment:\n      DB_HOST: db.example.net\n  orders:\n    image: postgres:16\n",
            "Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "server.js": "import {Pool} from 'pg'; new Pool({host:process.env.DB_HOST});",
        },
    )
    result = gate(repo)
    assert units(result)["api"]["dependsOn"] == []
    assert units(result)["api"]["env"][0]["binding"] is None


def test_client_engine_and_url_conflict_is_reported(tmp_path):
    result = gate(
        app(
            tmp_path,
            "import {Pool} from 'pg'; new Pool({connectionString:process.env.DATABASE_URL});",
            example="DATABASE_URL=mongodb://remote.example.net/app",
        )
    )
    assert result["dependencies"] == []
    assert any(q["code"] == "database_engine_conflict" for q in result["questions"])


def test_python_alias_connection_and_sqlite_storage_obligation(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "requirements.txt": "psycopg[binary]\n",
            "app.py": "import os\nimport psycopg as pg\nimport sqlite3\npg.connect(os.environ['DATABASE_URL'])\nsqlite3.connect('/data/app.db')\n",
        },
    )
    result = gate(repo)
    assert [d["engine"] for d in result["dependencies"]] == ["postgres"]
    assert any(q["code"] == "sqlite_persistence_required" for q in result["questions"])


def test_compose_dependency_cycle_is_reported(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "compose.yaml": "services:\n  api:\n    build: ./api\n    depends_on: [web]\n  web:\n    build: ./web\n    depends_on: [api]\n",
            "api/Dockerfile": "FROM node:22\nEXPOSE 3000\n",
            "web/Dockerfile": "FROM nginx\nEXPOSE 80\n",
        },
    )
    assert any(q["code"] == "dependency_cycle" for q in gate(repo)["questions"])


def test_in_memory_sqlite_does_not_require_persistent_storage(tmp_path):
    repo = write(
        tmp_path / "repo",
        {
            "Dockerfile": "FROM python:3.13\nEXPOSE 3000\n",
            "app.py": "import sqlite3\nsqlite3.connect(':memory:')\n",
        },
    )
    result = gate(repo, mode="force")
    assert result["dependencies"] == []
    assert not any(q["code"].startswith("sqlite_") for q in result["questions"])


def test_local_url_preserves_database_and_options_without_password(tmp_path):
    result = gate(
        app(
            tmp_path,
            "import {Pool} from 'pg'; new Pool({connectionString:process.env.DATABASE_URL});",
            example="DATABASE_URL=postgres://app:secret-marker@localhost:5432/shop?sslmode=disable",
        )
    )
    assert result["dependencies"][0]["database"] == "shop"
    row = result["units"][0]["env"][0]
    assert row["binding"]["urlSuffix"] == "/shop?sslmode=disable"
    assert row["binding"]["scheme"] == "postgres"
    assert "secret-marker" not in json.dumps(result)


def test_hardcoded_internal_connection_requires_environment_refactor(tmp_path):
    result = gate(
        app(tmp_path, "import {Pool} from 'pg'; new Pool({connectionString:'postgres://localhost/shop'});")
    )
    assert any(q["code"] == "database_connection_literal" for q in result["questions"])


@pytest.mark.parametrize(
    "source",
    [
        "import {Pool} from 'pg'; new Pool({connectionString:'process.env.DATABASE_URL'});",
        "import {Pool} from 'pg'; new Pool({/* process.env.DATABASE_URL */});",
        "import {Pool} from 'pg'; function f(process) {new Pool({connectionString:process.env.DATABASE_URL});}",
        "import {Pool} from 'pg'; function f() {const Pool = fake; new Pool({connectionString:process.env.DATABASE_URL});}",
    ],
)
def test_env_strings_comments_and_shadowed_bindings_do_not_provision(tmp_path, source):
    assert gate(app(tmp_path, source), mode="force")["dependencies"] == []


def test_external_host_with_password_key_does_not_infer_another_local_db(tmp_path):
    repo = app(
        tmp_path,
        "import {Pool} from 'pg'; new Pool({host:process.env.DB_HOST, password:process.env.DB_PASSWORD});",
        example="DB_HOST=remote.example.net\nDB_PASSWORD=\n",
    )
    result = gate(repo)
    assert result["dependencies"] == []
    assert all(row["binding"] is None for row in result["units"][0]["env"])
