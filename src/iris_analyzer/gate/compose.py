"""Compose services as build units or image-only dependencies.

Reuses the preprocess Docker extractor's port/command/YAML-position helpers so
the gate and the full analyzer read Compose the same way. Environment values
are inspected only to classify connection targets; they are never returned.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

import yaml

from ..preprocess.extractors.docker import _command, _compose_port, _yaml_lines, _yaml_mapping
from ..preprocess.extractors.execution import safe_repository_path
from .initdb import volume_mounts
from .links import line_of_key
from .scan import RepositoryScan, parent
from .sources import COMPOSE_STANDARD, env_required, image_name, port_number, url_engine, url_hosts

_DATABASE_IMAGES = (
    ("postgres", "postgres"),
    ("postgis/postgis", "postgres"),
    ("timescale/timescaledb", "postgres"),
    ("pgvector/pgvector", "postgres"),
    ("redis", "redis"),
    ("valkey/valkey", "redis"),
    ("redis/redis-stack", "redis"),
    ("eqalpha/keydb", "redis"),
    ("mysql", "mysql"),
    ("mariadb", "mysql"),
    ("mongo", "mongodb"),
)
_INFRA_IMAGES = re.compile(
    r"^(?:[\w.-]+/)?(rabbitmq|kafka|cp-kafka|zookeeper|elasticsearch|opensearch|minio|memcached|nats|"
    r"localstack|clickhouse-server|clickhouse|influxdb|cassandra|neo4j|meilisearch|typesense|qdrant|"
    r"chromadb|chroma|weaviate|milvus|etcd|consul|vault|keycloak|temporal)$"
)
_ADJUNCT_IMAGES = re.compile(
    r"(cloudflared|traefik|caddy|nginx-proxy|haproxy|adminer|pgadmin|mailhog|mailpit|watchtower|portainer|"
    r"redis-commander|redisinsight|mongo-express|phpmyadmin|prometheus|grafana|loki|promtail|jaeger|"
    r"otel|opentelemetry|certbot|ngrok|dozzle|cadvisor|node-exporter)"
)


@dataclass
class ComposeService:
    name: str
    file: str
    line: int
    build: bool
    image: str | None = None
    root: str | None = None
    dockerfile: str | None = None
    problem: str | None = None
    target: str | None = None
    build_args: list[str] = field(default_factory=list)
    ports: list[tuple[int, int]] = field(default_factory=list)
    env: list[dict] = field(default_factory=list)
    env_engines: dict[str, str] = field(default_factory=dict)
    # Runtime environment values (key -> raw value) and their lines. Internal to the
    # gate: used to find link targets, never serialized.
    env_values: dict[str, str] = field(default_factory=dict)
    env_lines: dict[str, int] = field(default_factory=dict)
    hosts: set[str] = field(default_factory=set)
    depends_on: list[str] = field(default_factory=list)
    command: str | None = None
    volumes: list[tuple[str, str]] = field(default_factory=list)
    # env_file paths (repository relative). Only their key names are ever read.
    env_files: list[str] = field(default_factory=list)


def image_engine(image: str | None) -> str | None:
    if not image:
        return None
    name = image_name(image)
    if _ADJUNCT_IMAGES.search(name):
        return None
    for marker, engine in _DATABASE_IMAGES:
        if name == marker or name.endswith("/" + marker):
            return engine
    if _INFRA_IMAGES.match(name):
        return "other"
    return None


def is_adjunct(image: str | None) -> bool:
    return bool(image) and bool(_ADJUNCT_IMAGES.search(image_name(str(image))))


def database_engine_hint(service: ComposeService, dockerfile_image: str | None) -> str | None:
    """Engine of a build service that actually runs a database (e.g. ``Dockerfile.mongo``)."""
    engine = image_engine(dockerfile_image)
    if engine and engine != "other":
        return engine
    match = re.match(r"^['\"]?(mongod|redis-server|postgres|mysqld)\b", service.command or "")
    if match:
        return {"mongod": "mongodb", "redis-server": "redis", "postgres": "postgres", "mysqld": "mysql"}[
            match[1]
        ]
    variant = posixpath.basename(service.dockerfile or "").lower()
    for marker, engine in (
        ("mongo", "mongodb"),
        ("postgres", "postgres"),
        ("redis", "redis"),
        ("mysql", "mysql"),
    ):
        if variant.endswith("." + marker) or variant.startswith(marker + "."):
            return engine
    return None


def compose_order(scan: RepositoryScan, paths: list[str]) -> list[str]:
    """Scope-root standard files first, then other files nearest the scope first."""

    def key(path: str) -> tuple:
        directory = parent(path)
        name = path.rsplit("/", 1)[-1].lower()
        standard = COMPOSE_STANDARD.index(name) if name in COMPOSE_STANDARD else len(COMPOSE_STANDARD)
        return (directory != scan.scope, directory.count("/"), standard, path)

    return sorted(paths, key=key)


def load_compose(scan: RepositoryScan, path: str) -> tuple[list[ComposeService], str | None]:
    text = scan.read(path)
    if text is None:
        return [], "compose_unreadable"
    try:
        document = yaml.safe_load(text)
        tree = yaml.compose(text)
    except yaml.YAMLError:
        return [], "compose_invalid"
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return [], None
    services_node = _yaml_mapping(tree).get("services")
    nodes = _yaml_mapping(services_node)
    key_lines = (
        {key.value: key.start_mark.line + 1 for key, _ in services_node.value}
        if isinstance(services_node, yaml.MappingNode)
        else {}
    )
    base = parent(path)
    result = []
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        name = str(name)
        node = nodes.get(name)
        fields = _yaml_mapping(node)
        image = service.get("image")
        build = service.get("build")
        item = ComposeService(
            name=name,
            file=path,
            line=key_lines.get(name, 1),
            build=build is not None,
            image=str(image) if isinstance(image, (str, int, float)) else None,
        )
        if build is not None:
            _build(item, build, base, scan)
        if "ports" in fields:
            line = _yaml_lines(fields["ports"])[0]
            for value in service.get("ports") or []:
                parsed = _compose_port(value)
                if parsed:
                    item.ports.append((parsed[0], line))
        if "expose" in fields:
            line = _yaml_lines(fields["expose"])[0]
            for value in service.get("expose") or []:
                port = port_number(str(value).split("/")[0])
                if port is not None:
                    item.ports.append((port, line))
        _environment(item, service, fields, text)
        dependencies = service.get("depends_on")
        if isinstance(dependencies, dict):
            item.depends_on = [str(key) for key in dependencies]
        elif isinstance(dependencies, list):
            item.depends_on = [str(key) for key in dependencies if isinstance(key, (str, int))]
        command = " ".join(
            filter(None, [_command(service.get("entrypoint")), _command(service.get("command"))])
        )
        item.command = command or None
        item.volumes = volume_mounts(service.get("volumes"))
        item.env_files = _env_files(service.get("env_file"), base)
        result.append(item)
    return result, None


def _env_files(value: object, base: str) -> list[str]:
    entries = value if isinstance(value, list) else [value]
    result = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("path")
        if isinstance(entry, str) and entry and "$" not in entry:
            path = safe_repository_path(base, entry)
            if path:
                result.append(path)
    return result


def _build(item: ComposeService, build: object, base: str, scan: RepositoryScan) -> None:
    if isinstance(build, str):
        context, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        context = build.get("context", ".")
        dockerfile = build.get("dockerfile", "Dockerfile")
        if "dockerfile_inline" in build:
            item.problem = "dockerfile_inline"
            return
        args = build.get("args")
        if isinstance(args, list):
            args = {str(entry).split("=", 1)[0]: (str(entry).split("=", 1) + [None])[1] for entry in args}
        target = build.get("target")
        if isinstance(target, (str, int, float)) and str(target).strip():
            item.target = str(target).strip()
        if isinstance(args, dict):
            item.build_args = sorted({str(key) for key in args})
            for key, value in args.items():
                item.env.append(
                    {"key": str(key), "stage": "build", "required": value is None or env_required("", value)}
                )
    else:
        item.problem = "build_context_invalid"
        return
    if isinstance(context, str) and re.match(r"^(?:[a-z]+://|git@)", context):
        item.problem = "build_context_remote"
        return
    root = safe_repository_path(base, context if context not in ("", None) else ".")
    if root is None:
        item.problem = "build_context_invalid"
        return
    item.root = "." if root in ("", ".") else root
    if not isinstance(dockerfile, str) or not dockerfile or dockerfile.startswith("/") or "$" in dockerfile:
        item.problem = "dockerfile_path_invalid"
        return
    normalized = posixpath.normpath(dockerfile)
    if normalized == ".." or normalized.startswith("../"):
        item.problem = "dockerfile_outside_context"
        return
    item.dockerfile = normalized


def _environment(item: ComposeService, service: dict, fields: dict, text: str) -> None:
    environment = service.get("environment")
    if isinstance(environment, list):
        environment = {
            str(entry).split("=", 1)[0]: (str(entry).split("=", 1)[1] if "=" in str(entry) else None)
            for entry in environment
        }
    if not isinstance(environment, dict):
        return
    line = _yaml_lines(fields["environment"])[0] if "environment" in fields else item.line
    for key, value in environment.items():
        key = str(key)
        env_port = port_number(value) if key == "PORT" else None
        if env_port is not None:
            item.ports.append((env_port, line))
        item.env.append({"key": key, "stage": "runtime", "required": env_required(key, value)})
        if value is not None:
            item.env_values[key] = str(value)
        item.env_lines[key] = line_of_key(text, line, key)
        engine = url_engine(value)
        if engine:
            item.env_engines[key] = engine
        item.hosts.update(url_hosts(value))
