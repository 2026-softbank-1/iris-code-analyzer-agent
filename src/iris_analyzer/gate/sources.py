"""Small, deterministic readers for Dockerfiles, runtime manifests and workspaces."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from .scan import RepositoryScan, join, relative_to, within

# Railpack-recognised manifests (directory-level). index.html and Staticfile
# are weak signals: they only count at the requested scope root.
RUNTIME_MANIFESTS = {
    "package.json": "node",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "Pipfile": "python",
    "setup.py": "python",
    "go.mod": "go",
    "Gemfile": "ruby",
    "Cargo.toml": "rust",
    "composer.json": "php",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "mix.exs": "elixir",
    "deno.json": "deno",
    "deno.jsonc": "deno",
}
WEAK_MANIFESTS = {"index.html": "static", "Staticfile": "static"}
WORKSPACE_FILES = ("pnpm-workspace.yaml", "lerna.json", "turbo.json", "nx.json")
ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env", "sample.env")
COMPOSE_STANDARD = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_DEV_VARIANTS = {"dev", "development", "local", "test", "tests", "ci", "debug", "e2e", "devcontainer"}
_NODE_SERVER_FRAMEWORKS = {
    "express",
    "fastify",
    "koa",
    "@nestjs/core",
    "hono",
    "@hapi/hapi",
    "restify",
    "@adonisjs/core",
    "socket.io",
}
_NODE_WEB_FRAMEWORKS = {
    "next",
    "nuxt",
    "astro",
    "@sveltejs/kit",
    "react-scripts",
    "@remix-run/dev",
    "@angular/core",
    "gatsby",
    "vite",
}
_PYTHON_WEB = re.compile(r"^(django|flask|fastapi|starlette|uvicorn|gunicorn|sanic|aiohttp|tornado)\b", re.I)
_DEPENDENCY_PACKAGES = {
    "node": {
        "pg": "postgres",
        "postgres": "postgres",
        "pg-promise": "postgres",
        "@neondatabase/serverless": "postgres",
        "redis": "redis",
        "ioredis": "redis",
        "bullmq": "redis",
        "bull": "redis",
        "mongoose": "mongodb",
        "mongodb": "mongodb",
        "mysql": "mysql",
        "mysql2": "mysql",
    },
    "python": {
        "psycopg": "postgres",
        "psycopg2": "postgres",
        "psycopg2-binary": "postgres",
        "asyncpg": "postgres",
        "redis": "redis",
        "celery": "redis",
        "rq": "redis",
        "pymongo": "mongodb",
        "motor": "mongodb",
        "mongoengine": "mongodb",
        "pymysql": "mysql",
        "mysqlclient": "mysql",
        "aiomysql": "mysql",
    },
}
_URL_ENGINES = (
    ("postgres", "postgres"),
    ("redis", "redis"),
    ("rediss", "redis"),
    ("mongodb", "mongodb"),
    ("mongodb+srv", "mongodb"),
    ("mysql", "mysql"),
    ("mariadb", "mysql"),
)
_SECRET_KEY = re.compile(r"(SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE|API_KEY|ACCESS_KEY|CREDENTIAL)", re.I)
_CONNECTION_KEY = re.compile(
    r"(DATABASE_URL|DB_URL|REDIS_URL|MONGO(DB)?_(URI|URL)|_DSN$|AMQP_URL|BROKER_URL)", re.I
)
_SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".py")
_PORT_PATTERNS = (
    re.compile(r"process\.env\.PORT\s*(?:\|\||\?\?)\s*['\"]?(\d{2,5})"),
    re.compile(r"\.listen\(\s*(\d{2,5})\s*[,)]"),
    re.compile(r"(?:uvicorn\.run|app\.run|web\.run_app)\([^)]*port\s*=\s*(\d{2,5})"),
    re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]PORT['\"]\s*,\s*['\"]?(\d{2,5})"),
)
_COMMAND_PORT = re.compile(r"(?:--port[= ]|-p |--bind[= ](?:\S*:)|PORT=)(\d{2,5})\b")


def is_dockerfile(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".dockerignore"):
        return False
    return lower == "dockerfile" or lower.startswith("dockerfile.") or lower.endswith(".dockerfile")


def dockerfile_variant(name: str) -> str | None:
    """Variant label of ``Dockerfile.<x>`` / ``<x>.Dockerfile``; None for plain Dockerfile."""
    lower = name.lower()
    if lower == "dockerfile":
        return None
    if lower.startswith("dockerfile."):
        return name[len("dockerfile.") :]
    return name[: -len(".dockerfile")]


def is_dev_dockerfile(name: str) -> bool:
    variant = dockerfile_variant(name)
    return variant is not None and variant.lower() in _DEV_VARIANTS


def is_compose_file(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".yaml", ".yml")) and (
        lower in COMPOSE_STANDARD or lower.startswith(("compose.", "docker-compose."))
    )


def port_number(value: object) -> int | None:
    text = str(value).strip().strip("'\"")
    match = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-?(\d+)\}", text)
    if match:
        text = match[1]
    if text.isdigit() and 0 < int(text) < 65536:
        return int(text)
    return None


def line_of(text: str | None, needle: str) -> int:
    if text:
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line:
                return number
    return 1


@dataclass
class Dockerfile:
    path: str
    stages: list[str] = field(default_factory=list)
    final_image: str | None = None
    exposed: list[tuple[int, int]] = field(default_factory=list)
    env_port: tuple[int, int] | None = None
    command: str | None = None
    copy_sources: list[str] = field(default_factory=list)
    parsed: bool = True


def image_name(image: str) -> str:
    """``registry/org/postgres:16@sha`` -> ``postgres``; keeps ``org/name`` for non-library images."""
    value = image.split("@", 1)[0]
    last = value.rsplit("/", 1)
    tail = last[-1].split(":", 1)[0]
    if len(last) == 2:
        prefix = last[0].rsplit("/", 1)[-1]
        if prefix not in {"library", "docker.io", "index.docker.io"} and "." not in prefix:
            return f"{prefix}/{tail}".lower()
    return tail.lower()


def parse_dockerfile(scan: RepositoryScan, path: str) -> Dockerfile:
    result = Dockerfile(path=path)
    text = scan.read(path)
    if text is None:
        result.parsed = False
        return result
    instructions: list[tuple[str, str, int]] = []
    value, start = "", 1
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not value and (not stripped or stripped.startswith("#")):
            continue
        if value and stripped.startswith("#"):
            continue
        if not value:
            start = number
        value += (" " if value else "") + stripped.rstrip("\\").rstrip()
        if stripped.endswith("\\"):
            continue
        match = re.match(r"([A-Za-z]+)\s+(.*)", value)
        if match:
            instructions.append((match[1].upper(), match[2].strip(), start))
        value = ""
    final_start = max((index for index, item in enumerate(instructions) if item[0] == "FROM"), default=0)
    entrypoint = None
    for index, (name, argument, number) in enumerate(instructions):
        final = index >= final_start
        if name == "FROM":
            parts = [part for part in argument.split() if not part.startswith("--")]
            if parts:
                result.stages.append(parts[0])
                if final:
                    result.final_image = parts[0]
        elif name in {"COPY", "ADD"}:
            parts = argument.split()
            if any(part.startswith("--from") for part in parts):
                continue
            sources = [part for part in parts if not part.startswith("--")][:-1]
            result.copy_sources.extend(source.strip("\"'[],") for source in sources)
        elif not final:
            continue
        elif name == "EXPOSE":
            for token in argument.split():
                port = port_number(token.split("/")[0])
                if port is not None:
                    result.exposed.append((port, number))
        elif name == "ENV":
            match = re.search(r"(?:^|\s)PORT(?:=|\s+)['\"]?(\d{2,5})\b", argument)
            if match and port_number(match[1]) is not None:
                result.env_port = (int(match[1]), number)
        elif name in {"CMD", "ENTRYPOINT"}:
            text_value = _command_text(argument)
            if name == "ENTRYPOINT":
                entrypoint = text_value
            else:
                result.command = " ".join(filter(None, [entrypoint, text_value]))
    if result.command is None and entrypoint:
        result.command = entrypoint
    return result


def _command_text(value: str) -> str | None:
    if value.startswith("["):
        try:
            items = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(items, list):
            return " ".join(shlex.quote(str(item)) for item in items)
    return value


def command_port(command: str | None) -> int | None:
    if not command:
        return None
    match = _COMMAND_PORT.search(command)
    return port_number(match[1]) if match else None


def dockerfile_copies(docker: Dockerfile, relative: str) -> bool:
    """True when a non-stage COPY/ADD source names ``relative`` (a path under the build context)."""
    for source in docker.copy_sources:
        normalized = source.lstrip("./") if source not in {".", "./"} else "."
        normalized = normalized.rstrip("/")
        if normalized == ".":
            continue
        if normalized == relative or normalized.startswith(relative + "/"):
            return True
    return False


def load_package(scan: RepositoryScan, path: str) -> dict | None:
    text = scan.read(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def node_dependencies(package: dict) -> dict:
    return {**_mapping(package.get("devDependencies")), **_mapping(package.get("dependencies"))}


def node_is_app(scan: RepositoryScan, directory: str, package: dict) -> bool:
    """Executable app heuristic: start script, a server framework, or a web framework with an entry."""
    scripts = _mapping(package.get("scripts"))
    dependencies = node_dependencies(package)
    if isinstance(scripts.get("start"), str):
        return True
    if any(name in dependencies for name in _NODE_SERVER_FRAMEWORKS) and not package.get("exports"):
        return bool(scripts) or isinstance(package.get("main"), str)
    frameworks = [name for name in _NODE_WEB_FRAMEWORKS if name in dependencies]
    if not frameworks or not ({"dev", "build"} & set(scripts)):
        return False
    if frameworks == ["vite"]:
        # Library-mode Vite packages have no HTML entry.
        return "index.html" in scan.names(directory)
    return True


def node_role(package: dict) -> str | None:
    dependencies = node_dependencies(package)
    if any(name in dependencies for name in _NODE_SERVER_FRAMEWORKS):
        return "api"
    if any(name in dependencies for name in _NODE_WEB_FRAMEWORKS):
        return "web"
    return None


def python_requirements(scan: RepositoryScan, directory: str) -> list[tuple[str, str, int]]:
    """(package, manifest path, line) from requirements*.txt / pyproject / Pipfile, best effort."""
    rows: list[tuple[str, str, int]] = []
    for name in ("requirements.txt", "pyproject.toml", "Pipfile"):
        path = join(directory, name)
        text = scan.read(path) if name in scan.names(directory) else None
        if not text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip().strip("\"',")
            if not stripped or stripped.startswith(("#", "[", "-")):
                continue
            match = re.match(r"([A-Za-z0-9_.\-]+)", stripped)
            if match:
                rows.append((match[1].lower().replace("_", "-"), path, number))
    return rows


def python_role(scan: RepositoryScan, directory: str) -> str | None:
    return (
        "api" if any(_PYTHON_WEB.match(name) for name, _, _ in python_requirements(scan, directory)) else None
    )


def inferred_dependencies(scan: RepositoryScan, directory: str) -> list[tuple[str, str, int]]:
    """(engine, evidence path, line) from client libraries declared by the unit's manifests."""
    found: dict[str, tuple[str, str, int]] = {}
    names = scan.names(directory)
    if "package.json" in names:
        path = join(directory, "package.json")
        package = load_package(scan, path) or {}
        text = scan.read(path)
        for dependency in sorted(_mapping(package.get("dependencies"))):
            engine = _DEPENDENCY_PACKAGES["node"].get(dependency)
            if engine and engine not in found:
                found[engine] = (engine, path, line_of(text, f'"{dependency}"'))
    for dependency, path, number in python_requirements(scan, directory):
        engine = _DEPENDENCY_PACKAGES["python"].get(dependency.replace(".", "-"))
        if engine and engine not in found:
            found[engine] = (engine, path, number)
    return [found[key] for key in sorted(found)]


def url_engine(value: object) -> str | None:
    match = re.match(r"\s*([a-z][a-z0-9+.-]*)://", str(value or ""))
    if not match:
        return None
    scheme = match[1].split("+", 1)[0] if match[1] != "mongodb+srv" else match[1]
    for prefix, engine in _URL_ENGINES:
        if scheme == prefix or scheme.startswith(prefix):
            return engine
    return None


def url_hosts(value: object) -> set[str]:
    return set(re.findall(r"(?:@|//)([A-Za-z0-9_.-]+)(?=[:/?]|$)", str(value or "")))


def env_required(key: str, value: object) -> bool:
    """Whether the platform user must supply the variable (never echoes values)."""
    text = "" if value is None else str(value)
    if not text.strip():
        return True
    if re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?\?[^}]*)?\}", text):
        return True
    if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", text.strip()):
        return True
    return bool(_CONNECTION_KEY.search(key) or _SECRET_KEY.search(key))


def env_example_keys(scan: RepositoryScan, directory: str) -> list[tuple[str, bool, str | None, str, int]]:
    """(key, required, url engine, path, line) from env example files; values are not returned."""
    rows = []
    for name in ENV_EXAMPLES:
        if name not in scan.names(directory):
            continue
        path = join(directory, name)
        for number, line in enumerate((scan.read(path) or "").splitlines(), 1):
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if not match:
                continue
            value = match[2].strip().strip("\"'")
            required = not value or bool(_CONNECTION_KEY.search(match[1]) or _SECRET_KEY.search(match[1]))
            rows.append((match[1], required, url_engine(value), path, number))
        break
    return rows


def source_port(scan: RepositoryScan, directory: str, limit: int = 120) -> tuple[int, str, int] | None:
    """First literal port in a bounded set of source files under ``directory`` (excluded dirs pruned)."""
    checked = 0
    for path in scan.files():
        if not within(path, directory) or not path.endswith(_SOURCE_SUFFIXES):
            continue
        depth = len(PurePosixPath(relative_to(path, directory)).parts)
        if depth > 4:
            continue
        checked += 1
        if checked > limit:
            break
        text = scan.read(path)
        if not text:
            continue
        for pattern in _PORT_PATTERNS:
            match = pattern.search(text)
            if match and port_number(match[1]) is not None:
                return int(match[1]), path, text.count("\n", 0, match.start()) + 1
    return None


def script_port(package: dict) -> int | None:
    scripts = _mapping(package.get("scripts"))
    for name in ("start", "serve"):
        command = scripts.get(name)
        if isinstance(command, str):
            port = command_port(command)
            if port is not None:
                return port
    return None


def workspace_patterns(scan: RepositoryScan, directory: str) -> tuple[list[str], list[str]]:
    """(member glob patterns, declaring manifest paths) for npm/yarn/pnpm/lerna workspaces."""
    patterns: list[str] = []
    declared: list[str] = []
    names = scan.names(directory)
    if "package.json" in names:
        package = load_package(scan, join(directory, "package.json")) or {}
        workspaces = package.get("workspaces")
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        if isinstance(workspaces, list) and workspaces:
            patterns.extend(item for item in workspaces if isinstance(item, str))
            declared.append(join(directory, "package.json"))
    if "pnpm-workspace.yaml" in names:
        try:
            document = yaml.safe_load(scan.read(join(directory, "pnpm-workspace.yaml")) or "")
        except yaml.YAMLError:
            document = None
        packages = document.get("packages") if isinstance(document, dict) else None
        if isinstance(packages, list):
            patterns.extend(item for item in packages if isinstance(item, str))
        declared.append(join(directory, "pnpm-workspace.yaml"))
    if "lerna.json" in names:
        lerna = load_package(scan, join(directory, "lerna.json")) or {}
        packages = lerna.get("packages")
        if isinstance(packages, list):
            patterns.extend(item for item in packages if isinstance(item, str))
        declared.append(join(directory, "lerna.json"))
    for name in ("turbo.json", "nx.json"):
        if name in names:
            declared.append(join(directory, name))
    return patterns, declared


def _glob_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip().lstrip("./").rstrip("/")
    out = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out += ".*"
            index += 2
            if pattern.startswith("/", index):
                out = out[:-2] + "(?:.*/)?"
                index += 1
        elif pattern[index] == "*":
            out += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(pattern[index])
            index += 1
    return re.compile(out + r"\Z")


def workspace_members(scan: RepositoryScan, root: str, patterns: list[str]) -> list[str]:
    positive = [_glob_regex(item) for item in patterns if item and not item.startswith("!")]
    negative = [_glob_regex(item[1:]) for item in patterns if item.startswith("!")]
    members = []
    for directory, names in sorted(scan.directories.items()):
        if directory == root or not within(directory, root) or "package.json" not in names:
            continue
        relative = relative_to(directory, root)
        if any(regex.match(relative) for regex in positive) and not any(
            regex.match(relative) for regex in negative
        ):
            members.append(directory)
    return members


def procfile_processes(scan: RepositoryScan, directory: str) -> list[tuple[str, str, int]]:
    if "Procfile" not in scan.names(directory):
        return []
    rows = []
    for number, line in enumerate((scan.read(join(directory, "Procfile")) or "").splitlines(), 1):
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.+)$", line.strip())
        if match and match[1] != "release":
            rows.append((match[1], match[2].strip(), number))
    return rows


def manifest_runtime(name: str) -> str | None:
    return RUNTIME_MANIFESTS.get(name)
