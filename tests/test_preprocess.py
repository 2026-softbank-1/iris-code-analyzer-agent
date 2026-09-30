"""Supported extraction, reproducibility, scope and deployment-unit regressions."""

import hashlib
import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest
from iris_analyzer.preprocess import prepare_context, save_bundle
from iris_analyzer.result import static_analysis


def write(repo: Path, path: str, text: str) -> None:
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def package(repo: Path, root: str, data: dict) -> None:
    write(repo, (root.rstrip("/") + "/" if root else "") + "package.json", json.dumps(data, indent=2))


@pytest.fixture
def combined(tmp_path):
    package(
        tmp_path,
        "",
        {
            "name": "workspace",
            "workspaces": ["client", "server"],
            "scripts": {"build": "npm run build -w server && npm run build -w client"},
        },
    )
    package(
        tmp_path,
        "client",
        {"name": "client", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "1"}},
    )
    package(
        tmp_path,
        "server",
        {
            "name": "server",
            "scripts": {"build": "tsc", "start": "node dist/index.js"},
            "dependencies": {"express": "5", "mongoose": "8"},
        },
    )
    write(tmp_path, "server/src/index.ts", "import {app} from './app.js';\napp.listen(4000, '0.0.0.0');\n")
    write(
        tmp_path,
        "server/src/app.ts",
        "import express from 'express';\nimport authRoutes from './routes/auth.routes.js';\nexport const app = express();\napp.get('/health/live', (_, res) => res.send('ok'));\napp.get(['/health/ready', '/api/health'], (_, res) => res.send('ok'));\napp.use('/api/auth', authRoutes);\napp.use(express.static('../client/dist'));\n",
    )
    write(
        tmp_path,
        "server/src/routes/auth.routes.ts",
        "import { Router } from 'express';\nconst router = Router();\nrouter.post('/login', login);\nrouter.get('/me', authMiddleware, me);\nexport default router;\n",
    )
    write(
        tmp_path,
        "client/src/lib/api.ts",
        "const API_BASE = '/api';\nfetch(`${API_BASE}${endpoint}`);\nfetch(`${API_BASE}/upload`);\n",
    )
    write(
        tmp_path, "client/vite.config.ts", "export default {server: {port: 5173}, build: {outDir: 'dist'}};"
    )
    write(tmp_path, "server/tsconfig.json", '{"compilerOptions":{"outDir":"./dist"}}')
    write(
        tmp_path,
        "Dockerfile",
        'FROM node:24 AS build\nWORKDIR /app\nRUN npm run build\nFROM node:24\nENV NODE_ENV=production PORT=4000\nWORKDIR /app/server\nCOPY --from=build /app/client/dist /app/public\nEXPOSE 4000\nCMD ["node", "dist/index.js"]\n',
    )
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  app:\n    build: .\n    ports: ['127.0.0.1:${APP_PORT:-8080}:4000']\n    environment:\n      SESSION_SECRET: ${SESSION_SECRET:?required}\n      MONGO_URI: mongodb://user:password@mongo:27017/data\n    volumes: [uploads:/data/uploads]\n    depends_on: [mongo]\n  mongo:\n    image: mongo:7\n    volumes: [mongo-data:/data/db]\nvolumes:\n  uploads:\n  mongo-data:\n",
    )
    return tmp_path


def facts(bundle, key):
    return [item for item in bundle["facts"] if item["key"] == key]


def test_combined_app_and_scoped_ports(combined):
    bundle = prepare_context(combined)
    assert bundle["componentRoots"] == [".", "client", "server"]
    assert [
        (candidate["role"], candidate["root"], candidate["componentRoots"])
        for candidate in bundle["deploymentCandidates"]
    ] == [("web_api", ".", ["client", "server"])]
    ports = {(item["value"], item["scope"], item["component"]) for item in facts(bundle, "runtime.port")}
    assert ports == {
        (4000, "container", "server"),
        (8080, "host_mapping", "server"),
        (5173, "development", "client"),
    }
    assert any(
        item["value"] == "0.0.0.0" and item["scope"] == "production" for item in facts(bundle, "runtime.host")
    )
    assert any(item["value"] == "/app/server" for item in facts(bundle, "docker.workdir"))
    assert any(
        item["value"] == "node dist/index.js" and item["scope"] == "container"
        for item in facts(bundle, "start.command")
    )
    assert any(
        item["value"] == {"name": "mongo", "engine": "mongodb"}
        for item in facts(bundle, "dependency.database")
    )
    assert {item["value"]["name"] for item in facts(bundle, "dependency.volume")} == {"uploads", "mongo-data"}


def test_router_mounts_arrays_and_source_js_to_ts(combined):
    bundle = prepare_context(combined)
    paths = {item["path"] for item in bundle["selectedFiles"]}
    assert "server/src/routes/auth.routes.ts" in paths
    routes = {(item["value"]["method"], item["value"]["path"]) for item in facts(bundle, "api.route")}
    assert {
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/me"),
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/api/health"),
    } <= routes
    assert {item["value"] for item in facts(bundle, "healthcheck.path")} == {
        "/health/live",
        "/health/ready",
        "/api/health",
    }
    assert [item["value"] for item in facts(bundle, "frontend.connection")] == [{"baseUrl": "/api"}]
    assert not facts(bundle, "authentication")


def test_byte_identical_repeated_preprocessing_and_digests(combined):
    first = prepare_context(combined)
    second = prepare_context(combined)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["contextHash"] == digest(
        {key: value for key, value in first.items() if key != "contextHash"}
    )
    manifest = {item["path"]: item for item in first["manifest"]}
    for item in first["evidence"]:
        assert item["sourceDigest"] == manifest[item["path"]]["digest"]
        assert item["contentDigest"] == hashlib.sha256(item["text"].encode()).hexdigest()
    assert str(combined) not in canonical_bytes(first).decode()


def test_snapshot_detects_uncommitted_source_changes(combined):
    first = prepare_context(combined)
    write(
        combined,
        "server/src/app.ts",
        (combined / "server/src/app.ts").read_text() + "\napp.get('/added', handler);\n",
    )
    second = prepare_context(combined)
    assert first["source"]["snapshotId"] != second["source"]["snapshotId"]
    assert first["contextHash"] != second["contextHash"]


def test_independent_workspaces_remain_two_units(tmp_path):
    package(tmp_path, "", {"workspaces": ["web", "api"]})
    package(tmp_path, "web", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    package(tmp_path, "api", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "api/src/index.js",
        "import express from 'express'; const app = express(); app.listen(3000);",
    )
    bundle = prepare_context(tmp_path)
    assert {(item["root"], item["role"]) for item in bundle["deploymentCandidates"]} == {
        ("api", "api"),
        ("web", "static"),
    }
    assert any(
        item["key"] == "build.command" and item["value"] == "none" and item["component"] == "api"
        for item in bundle["facts"]
    )


def test_static_only_does_not_invent_node_server(tmp_path):
    package(
        tmp_path,
        "",
        {
            "devDependencies": {"vite": "1"},
            "scripts": {"build": "vite build", "dev": "vite --port 4387 --host 127.0.0.1"},
        },
    )
    bundle = prepare_context(tmp_path)
    assert bundle["deploymentCandidates"][0]["role"] == "static"
    assert not facts(bundle, "start.command")
    assert [item["value"] for item in facts(bundle, "output.directory")] == ["dist"]
    assert all(item["scope"] == "development" for item in facts(bundle, "runtime.port"))


def test_static_nginx_compose_hosting_preserves_static_role(tmp_path):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(
        tmp_path,
        "ops/compose.yaml",
        "services:\n  website:\n    image: nginx:1\n    entrypoint: [nginx]\n    command: [-g, 'daemon off;']\n    ports: ['127.0.0.1:4387:8080']\n    healthcheck:\n      test: [CMD, wget, 'http://127.0.0.1:8080/healthz']\n  tunnel:\n    image: cloudflare/cloudflared:latest\n    command: tunnel run\n",
    )
    bundle = prepare_context(tmp_path)
    assert [(item["role"], item["root"]) for item in bundle["deploymentCandidates"]] == [("static", ".")]
    assert {item["value"] for item in facts(bundle, "healthcheck.path")} == {"/healthz"}
    assert {(item["value"], item["scope"]) for item in facts(bundle, "runtime.port")} == {
        (8080, "container"),
        (4387, "host_mapping"),
    }
    runtime_facts = {(item["value"], item["scope"]) for item in facts(bundle, "runtime.name")}
    assert runtime_facts == {("node", "source"), ("nginx", "container")}
    result = static_analysis(bundle)
    assert result["services"][0]["runtime"]["value"] == "nginx"
    assert result["services"][0]["runtime"]["scope"] == "container"
    assert result["status"] == "complete"


def test_compose_independent_build_contexts(tmp_path):
    package(tmp_path, "web", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    package(tmp_path, "api", {"dependencies": {"express": "1"}, "scripts": {"start": "node index.js"}})
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  website:\n    build: ./web\n  backend:\n    build: ./api\n    ports: ['3000:3000']\n  database:\n    image: postgres:16\n",
    )
    bundle = prepare_context(tmp_path)
    assert {(item["root"], item["role"]) for item in bundle["deploymentCandidates"]} == {
        ("web", "static"),
        ("api", "api"),
    }


def test_docker_final_stage_controls_runtime_port(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "1"}, "scripts": {"start": "node index.js"}})
    write(
        tmp_path,
        "Dockerfile",
        'FROM node:24 AS dev\nENV PORT=5173\nEXPOSE 5173\nFROM node:24\nENV PORT=4000\nEXPOSE 4000\nCMD ["node", "index.js"]\n',
    )
    bundle = prepare_context(tmp_path)
    assert {item["value"] for item in facts(bundle, "runtime.port")} == {4000}


def test_comments_and_strings_cannot_invent_routes_or_imports(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express';\nconst app = express();\n// app.get('/fake-comment', handler);\nconst fake = \"app.get('/fake-string', handler)\";\n// import nonexistent from './ghost.js';\napp.post('/real', handler);\napp.listen(3000);\n",
    )
    bundle = prepare_context(tmp_path)
    assert [item["value"] for item in facts(bundle, "api.route")] == [{"method": "POST", "path": "/real"}]
    assert not bundle["unresolved"]


def test_non_express_get_calls_do_not_become_api_routes(tmp_path):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(tmp_path, "src/api.js", "import axios from 'axios';\naxios.get('/not-a-server-route');\n")
    bundle = prepare_context(tmp_path)
    assert not facts(bundle, "api.route")


def test_nested_router_mount_and_array_cartesian_product(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express'; import outer from './outer.js'; const app = express(); app.use(['/v1', '/v2'], outer); app.listen(3000);",
    )
    write(
        tmp_path,
        "src/outer.js",
        "import {Router} from 'express'; import inner from './inner.js'; const outer = Router(); outer.use('/api', inner); export default outer;",
    )
    write(
        tmp_path,
        "src/inner.js",
        "import {Router} from 'express'; const inner = Router(); inner.get(['/a', '/b'], handler); export default inner;",
    )
    bundle = prepare_context(tmp_path)
    assert {item["value"]["path"] for item in facts(bundle, "api.route")} == {
        "/v1/api/a",
        "/v1/api/b",
        "/v2/api/a",
        "/v2/api/b",
    }


def test_commonjs_routes_and_route_chaining(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "const express = require('express'); const app = express(); const router = require('./router'); app.use('/api', router); app.listen(3000);",
    )
    write(
        tmp_path,
        "src/router.js",
        "const express = require('express'); const router = express.Router(); router.route('/thing').get(handler).post(handler); module.exports = router;",
    )
    bundle = prepare_context(tmp_path)
    routes = {(item["value"]["method"], item["value"]["path"]) for item in facts(bundle, "api.route")}
    assert ("GET", "/api/thing") in routes
    assert ("POST", "/api/thing") in routes


def test_dynamic_routes_remain_unresolved(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express'; const app = express(); app.get(computeRoute(), handler); app.listen(3000);",
    )
    bundle = prepare_context(tmp_path)
    assert not facts(bundle, "api.route")
    assert any(item["key"] == "runtime.httpRoutes" for item in bundle["unresolved"])


def test_dynamic_router_mount_remains_unresolved(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express'; import router from './router.js'; const app = express(); app.use(process.env.PREFIX, router); app.listen(3000);",
    )
    write(
        tmp_path,
        "src/router.js",
        "import {Router} from 'express'; const router=Router(); router.get('/x', handler); export default router;",
    )
    bundle = prepare_context(tmp_path)
    assert not facts(bundle, "api.route")
    assert any(item["key"] == "runtime.httpRoutes" for item in bundle["unresolved"])


def test_typescript_alias_and_js_suffix_reference(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node dist/index.js"}})
    write(tmp_path, "tsconfig.json", '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}')
    write(tmp_path, "src/index.ts", "import {app} from '@/app.js'; app.listen(3000);")
    write(
        tmp_path,
        "src/app.ts",
        "import express from 'express'; export const app=express(); app.get('/alias', handler);",
    )
    bundle = prepare_context(tmp_path)
    assert any(item["value"]["path"] == "/alias" for item in facts(bundle, "api.route"))
    assert not bundle["unresolved"]


@pytest.mark.parametrize("alias", ["'@': './src'", "'@': fileURLToPath(new URL('./src', import.meta.url))"])
def test_vite_alias_without_tsconfig(tmp_path, alias):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(tmp_path, "vite.config.js", "export default { resolve: { alias: {" + alias + "} } };")
    write(tmp_path, "src/api.js", "import {base} from '@/endpoint'; fetch(base);")
    write(tmp_path, "src/endpoint.js", "export const base='/api';")
    bundle = prepare_context(tmp_path)
    assert "src/endpoint.js" in {item["path"] for item in bundle["selectedFiles"]}
    assert not bundle["unresolved"]


def test_vite_dynamic_output_is_unknown(tmp_path):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(tmp_path, "vite.config.js", "export default {build: {outDir: process.env.OUTPUT}};")
    bundle = prepare_context(tmp_path)
    assert not facts(bundle, "output.directory")
    assert any(item["key"] == "output.directory" for item in bundle["unresolved"])


def test_env_member_subscript_and_schema_keys(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(tmp_path, "src/index.js", "const key=process.env['SESSION_KEY']; const port=process.env.PORT;")
    write(
        tmp_path,
        "src/config/env.ts",
        "const config=z.object({PUBLIC_URL: z.string(), SESSION_SECRET: z.string()}).parse(process.env);",
    )
    write(tmp_path, ".env.example", "OTHER_KEY=super_sensitive_example\nPORT=4000\n")
    bundle = prepare_context(tmp_path)
    assert {item["value"] for item in facts(bundle, "environment.key")} == {
        "SESSION_KEY",
        "PORT",
        "PUBLIC_URL",
        "SESSION_SECRET",
        "OTHER_KEY",
    }
    assert "super_sensitive_example" not in canonical_bytes(bundle).decode()


def test_unknown_local_import_recorded(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(tmp_path, "src/index.js", "import app from './missing.js';")
    bundle = prepare_context(tmp_path)
    assert bundle["coverage"]["unresolvedReferences"][0]["reference"] == "./missing.js"
    assert any(item["key"] == "local_reference" for item in bundle["unresolved"])


def test_missing_configured_alias_reference_is_unresolved(tmp_path):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(tmp_path, "tsconfig.json", '{"compilerOptions":{"paths":{"@/*":["src/*"]}}}')
    write(tmp_path, "src/api.ts", "import missing from '@/missing.ts';")
    bundle = prepare_context(tmp_path)
    assert any(item.get("reference") == "@/missing.ts" for item in bundle["unresolved"])


def test_dynamic_frontend_connection_is_unresolved(tmp_path):
    package(tmp_path, "", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}})
    write(tmp_path, "src/api.ts", "fetch(import.meta.env.VITE_BACKEND_URL);")
    bundle = prepare_context(tmp_path)
    assert not facts(bundle, "frontend.connection")
    assert any(item["key"] == "frontend.connection" for item in bundle["unresolved"])


def test_save_bundle_artifacts(combined, tmp_path):
    bundle = prepare_context(combined)
    out = tmp_path / "output"
    save_bundle(bundle, out)
    assert {item.name for item in out.iterdir()} == {
        "manifest.json",
        "context.json",
        "evidence.jsonl",
        "model-input.json",
    }
    assert json.loads((out / "manifest.json").read_text()) == bundle["manifest"]
    assert [json.loads(line) for line in (out / "evidence.jsonl").read_text().splitlines()] == bundle[
        "evidence"
    ]
    model = json.loads((out / "model-input.json").read_text())
    assert all(
        item["path"] in {selected["path"] for selected in bundle["selectedFiles"]}
        for item in model["manifest"]
    )


@pytest.mark.parametrize("package_data", [{"scripts": []}, {"dependencies": []}, {"workspaces": "client"}])
def test_malformed_package_shapes_are_structured_errors(tmp_path, package_data):
    package(tmp_path, "", package_data)
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path)
    assert error.value.code == "SOURCE_PARSE_INVALID"


def test_malformed_manifest_is_structured_error(tmp_path):
    write(tmp_path, "package.json", "invalid json")
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path)
    assert error.value.code == "SOURCE_PARSE_INVALID"


def test_unknown_profile_rejected(tmp_path):
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path, profile="execute_everything")
    assert error.value.code == "PROFILE_UNSUPPORTED"


def test_unsupported_non_node_source_is_explicit(tmp_path):
    write(tmp_path, "main.py", "print('hello')")
    bundle = prepare_context(tmp_path)
    assert not bundle["deploymentCandidates"]
    assert not bundle["componentRoots"]
    assert any(item["key"] == "deployment.support" for item in bundle["unresolved"])


def test_python_docker_observations_have_valid_component_and_unsupported_result(tmp_path):
    write(tmp_path, "main.py", "print('unsupported Python application')\n")
    write(tmp_path, "Dockerfile", 'FROM python:3.11\nWORKDIR /app\nEXPOSE 8000\nCMD ["python", "main.py"]\n')
    bundle = prepare_context(tmp_path)
    assert bundle["componentRoots"] == ["."]
    assert all(item.get("component", ".") in bundle["componentRoots"] for item in bundle["facts"])
    assert static_analysis(bundle)["status"] == "unsupported"


def test_node_source_without_manifest_is_unsupported_valid_context(tmp_path):
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express'; const app=express(); app.get('/hello', handler); app.listen(3000);",
    )
    bundle = prepare_context(tmp_path)
    assert bundle["componentRoots"] == ["."]
    assert facts(bundle, "api.route")
    assert static_analysis(bundle)["status"] == "unsupported"


def test_test_fixture_manifests_and_configs_do_not_declare_deployment_apps(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(tmp_path, "src/index.js", "import express from 'express'; const app=express(); app.listen(3000);")
    package(tmp_path, "tests/fixtures/invalid", {"scripts": []})
    package(
        tmp_path, "fixtures/fake-web", {"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}}
    )
    write(tmp_path, "fixtures/fake-web/vite.config.js", "export default {server: {port: 9999}};")
    write(tmp_path, "tests/fixtures/invalid/compose.yaml", "services: {fake: {image: nginx}}")
    bundle = prepare_context(tmp_path)
    assert bundle["componentRoots"] == ["."]
    assert [(item["role"], item["componentRoots"]) for item in bundle["deploymentCandidates"]] == [
        ("api", ["."])
    ]
    assert not any(item["path"].startswith(("tests/", "fixtures/")) for item in bundle["selectedFiles"])
    assert any(
        item["path"] == "tests/fixtures/invalid/package.json" and item["eligible"]
        for item in bundle["manifest"]
    )
    assert static_analysis(bundle)["status"] == "complete"


def test_python_root_with_nested_node_fixture_stays_unsupported(tmp_path):
    write(tmp_path, "main.py", "print('Python root')\n")
    package(
        tmp_path,
        "fixtures/node-app",
        {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}},
    )
    write(
        tmp_path,
        "fixtures/node-app/src/index.js",
        "import express from 'express'; const app=express(); app.listen(3000);",
    )
    bundle = prepare_context(tmp_path)
    assert not bundle["deploymentCandidates"]
    assert not bundle["componentRoots"]
    assert static_analysis(bundle)["status"] == "unsupported"


def test_direct_imported_fixture_data_remains_available_as_evidence(tmp_path):
    package(tmp_path, "", {"dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}})
    write(
        tmp_path,
        "src/index.js",
        "import {port} from '../fixtures/settings.js'; import express from 'express'; const app=express(); app.listen(3000);",
    )
    write(tmp_path, "fixtures/settings.js", "export const port=3000;")
    bundle = prepare_context(tmp_path)
    assert "fixtures/settings.js" in {item["path"] for item in bundle["selectedFiles"]}
    assert not bundle["coverage"]["unresolvedReferences"]
