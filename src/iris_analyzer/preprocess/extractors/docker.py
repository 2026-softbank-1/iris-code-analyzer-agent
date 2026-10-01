"""Docker and Compose execution units, scope-specific ports and dependencies."""

from __future__ import annotations

import copy
import json
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import yaml

from iris_analyzer.contracts import digest

from ..observations import Observations
from ..selector import Selection, component_of
from .execution import safe_repository_path


def extract_docker(selection: Selection, observations: Observations, components: dict) -> list[dict]:
    dockerfiles = {}
    docker_targets: dict[str, set[str]] = {}
    for path in selection.ordered():
        if PurePosixPath(path).name.lower().startswith("dockerfile"):
            dockerfiles[path] = _dockerfile(path, selection, observations, components)
    observations.current_candidate_id = None
    candidates = []
    covered = set()
    compose_execution_settings = {}
    for path in selection.ordered():
        name = PurePosixPath(path).name.lower()
        if name not in {
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        } and not name.startswith(("compose.", "docker-compose.")):
            continue
        text = selection.snapshot.files[path].decode("utf-8-sig")
        try:
            document = yaml.safe_load(text)
            tree = yaml.compose(text)
        except yaml.YAMLError:
            observations.unknown("compose", "Compose YAML is malformed", path=path)
            continue
        services = document.get("services", {}) if isinstance(document, dict) else {}
        if not isinstance(services, dict):
            observations.unknown("compose", "Compose services must be a mapping", path=path)
            continue
        observations.current_condition = (
            None
            if name in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
            else f"when Compose file {path} is selected"
        )
        nodes = _yaml_mapping(_yaml_mapping(tree).get("services"))
        database_services = {}
        for service_name, service in sorted(services.items()):
            if not isinstance(service, dict):
                continue
            engine = _database_engine(service_name, service, dockerfiles)
            if engine:
                database_services[service_name] = engine
        for service_name, service in sorted(services.items()):
            if not isinstance(service, dict):
                continue
            service_node = nodes.get(service_name)
            service_range = _yaml_lines(service_node)
            service_evidence = observations.snippet(path, *service_range)
            service_fields = _yaml_mapping(service_node)
            engine = database_services.get(service_name)
            image = str(service.get("image", ""))
            adjunct = bool(re.search(r"(?:^|/)(?:cloudflared|traefik|caddy|haproxy)(?:[:@]|$)", image))
            build = service.get("build")
            context = (
                build
                if isinstance(build, str)
                else build.get("context", ".")
                if isinstance(build, dict)
                else None
            )
            root = safe_repository_path(posixpath.dirname(path), context)
            if root == "" or root == ".":
                root = "."
            if isinstance(build, (str, dict)) and root is None:
                observations.unknown(
                    "deployment.root",
                    "Compose build context is dynamic, remote or leaves repository",
                    path=path,
                    service=service_name,
                )
                continue
            if root is None:
                deployable = [item for item, info in components.items() if info["role"]]
                root = deployable[0] if len(deployable) == 1 else "."
            component_roots = [
                item
                for item, info in components.items()
                if info["role"] and (root == "." or item == root or item.startswith(root + "/"))
            ]
            if not component_roots and root in components:
                component_roots = [root]
            docker_path = None
            if isinstance(build, (str, dict)):
                docker_name = (
                    build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile"
                )
                docker_path = safe_repository_path(root, docker_name)
                if docker_path is None:
                    observations.unknown(
                        "deployment.dockerfile",
                        "Compose Dockerfile path is dynamic, remote or leaves repository",
                        path=path,
                        service=service_name,
                    )
                    continue
            docker = dockerfiles.get(docker_path, {})
            if docker.get("component") in components and docker["component"] != ".":
                participating = {docker["component"]} | set(docker.get("copiedComponents", []))
                component_roots = sorted(
                    item for item in participating if item in components and components[item]["role"]
                )
            elif len(component_roots) > 1 and not engine and not adjunct:
                observations.unknown(
                    "deployment.components",
                    "Docker execution component is ambiguous within a shared build context",
                    path=path,
                    service=service_name,
                )
            roles = {components[item]["role"] for item in component_roots if item in components}
            role = (
                "web_api"
                if "static" in roles and roles & {"api", "server"}
                else "static"
                if roles == {"static"}
                or docker.get("imageRuntime") == "nginx"
                or re.search(r"(?:^|/)nginx(?:[:@]|$)", image)
                else "api"
                if "api" in roles
                else "server"
            )
            runtime_component = _runtime_component(
                component_roots, components, dockerfiles.get(docker_path, {})
            )
            if engine:
                observations.current_candidate_id = None
                owner = candidates[0]["root"] if candidates else "."
                observations.fact(
                    "dependency.database",
                    {"name": service_name, "engine": engine},
                    owner,
                    "container",
                    [service_evidence],
                )
                runtime_component = owner
            elif not adjunct:
                candidate_evidence = [service_evidence]
                if docker_path in dockerfiles:
                    candidate_evidence.extend(dockerfiles[docker_path]["evidenceIds"])
                for component in component_roots:
                    candidate_evidence.extend(components[component]["evidenceIds"])
                candidate = {
                    "candidateId": "svc-" + digest({"service": service_name, "root": root})[:16],
                    "root": root,
                    "role": role,
                    "componentRoots": sorted(component_roots),
                    "evidenceIds": sorted(set(candidate_evidence)),
                }
                execution = {
                    key: service[key]
                    for key in (
                        "build",
                        "ports",
                        "expose",
                        "entrypoint",
                        "command",
                        "working_dir",
                        "environment",
                    )
                    if key in service
                }
                previous_execution = compose_execution_settings.get(candidate["candidateId"])
                if previous_execution is not None and execution != previous_execution:
                    observations.unknown(
                        "deployment.compose_variant",
                        "Alternative Compose files redefine application execution settings; select or explicitly merge a deployment configuration",
                        path=path,
                        service=service_name,
                    )
                compose_execution_settings[candidate["candidateId"]] = execution
                observations.current_candidate_id = candidate["candidateId"]
                if docker_path in dockerfiles:
                    docker_targets.setdefault(docker_path, set()).add(candidate["candidateId"])
                existing = next(
                    (item for item in candidates if item["candidateId"] == candidate["candidateId"]), None
                )
                if existing is not None:
                    existing["evidenceIds"] = sorted(set(existing["evidenceIds"] + candidate["evidenceIds"]))
                else:
                    candidates.append(candidate)
                covered.update(component_roots)
                image_runtime = image.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0]
                if image_runtime in {"nginx", "node", "python", "bun", "deno", "httpd"}:
                    image_evidence = (
                        observations.snippet(path, *_yaml_lines(service_fields["image"]))
                        if "image" in service_fields
                        else service_evidence
                    )
                    observations.fact(
                        "runtime.name", image_runtime, runtime_component, "container", [image_evidence]
                    )
                if role == "static" and re.search(r"(?:^|/)nginx(?:[:@]|$)", image):
                    observations.fact(
                        "hosting.runtime", "nginx", runtime_component, "container", [service_evidence]
                    )
            else:
                observations.current_candidate_id = None
            # Environment VALUES do not become model facts. Only variable names
            # and demonstrably non-secret numeric PORT execution settings do.
            environment = service.get("environment", {})
            if isinstance(environment, list):
                environment = {
                    item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else None
                    for item in environment
                    if isinstance(item, str)
                }
            env_fields = _yaml_mapping(service_fields.get("environment"))
            if isinstance(environment, dict):
                for key, value in sorted(environment.items()):
                    env_evidence = (
                        observations.snippet(path, *_yaml_lines(env_fields.get(str(key))))
                        if str(key) in env_fields
                        else service_evidence
                    )
                    observations.fact(
                        "environment.key", str(key), runtime_component, "container", [env_evidence]
                    )
                    for variable in _variables(value):
                        observations.fact(
                            "environment.key", variable, runtime_component, "source", [env_evidence]
                        )
                    if str(key) == "PORT":
                        port = _numeric_default(value)
                        if port is not None:
                            observations.fact(
                                "runtime.port", port, runtime_component, "container", [env_evidence]
                            )
            ports_evidence = (
                observations.snippet(path, *_yaml_lines(service_fields["ports"]))
                if "ports" in service_fields
                else service_evidence
            )
            for port in service.get("ports", []) or []:
                parsed = _compose_port(port)
                if parsed:
                    target, published, host = parsed
                    observations.fact(
                        "runtime.port", target, runtime_component, "container", [ports_evidence]
                    )
                    if published is not None:
                        observations.fact(
                            "runtime.port", published, runtime_component, "host_mapping", [ports_evidence]
                        )
                    if host:
                        observations.fact(
                            "runtime.host", host, runtime_component, "host_mapping", [ports_evidence]
                        )
                else:
                    observations.unknown(
                        "runtime.port",
                        "Compose port mapping cannot be resolved statically",
                        path=path,
                        component=runtime_component,
                        evidenceIds=[ports_evidence],
                    )
                for variable in _variables(port):
                    observations.fact(
                        "environment.key", variable, runtime_component, "source", [ports_evidence]
                    )
            for exposed in service.get("expose", []) or []:
                port = _numeric_default(str(exposed).split("/")[0])
                if port is not None:
                    observations.fact(
                        "runtime.port", port, runtime_component, "container", [service_evidence]
                    )
            for volume in service.get("volumes", []) or []:
                parsed = _volume(volume)
                if parsed:
                    source_name, mount_path, volume_type = parsed
                    observations.fact(
                        "dependency.volume",
                        {"name": source_name, "mountPath": mount_path, "type": volume_type},
                        runtime_component,
                        "container",
                        [service_evidence],
                    )
            dependencies = service.get("depends_on", {})
            dependency_names = (
                dependencies.keys()
                if isinstance(dependencies, dict)
                else dependencies
                if isinstance(dependencies, list)
                else []
            )
            for dependency in sorted(dependency_names):
                observations.relation("depends_on", runtime_component, str(dependency), [service_evidence])
            for command_key in ("entrypoint", "command"):
                if command_key in service and not engine and not adjunct:
                    command = _command(service[command_key])
                    if command:
                        # ENTRYPOINT+CMD form one executable command.
                        full = " ".join(
                            filter(
                                None, [_command(service.get("entrypoint")), _command(service.get("command"))]
                            )
                        )
                        observations.fact(
                            "start.command", full, runtime_component, "container", [service_evidence]
                        )
                        break
            workdir = service.get("working_dir")
            if isinstance(workdir, str):
                observations.fact(
                    "docker.workdir", workdir, runtime_component, "container", [service_evidence]
                )
            healthcheck = service.get("healthcheck", {})
            if isinstance(healthcheck, dict):
                test = _command(healthcheck.get("test"))
                for url in re.findall(r"https?://[^\s'\"\]]+", test or ""):
                    parsed_url = urlsplit(url)
                    observations.fact(
                        "healthcheck.path",
                        parsed_url.path or "/",
                        runtime_component,
                        "container",
                        [service_evidence],
                    )
    observations.current_candidate_id = None
    observations.current_condition = None
    if not candidates:
        # A Dockerfile combines modules only when its actual static-file COPY
        # and app static-serving relation support that composition.
        served = {
            (relation["from"], relation["to"])
            for relation in observations.relations
            if relation["type"] == "serves_static"
        }
        for path, docker in sorted(dockerfiles.items()):
            if docker.get("database"):
                continue
            root = PurePosixPath(path).parent.as_posix()
            possible = [
                item
                for item, info in components.items()
                if info["role"] and (root == "." or item == root or item.startswith(root + "/"))
            ]
            if docker.get("staticCopies") and any(pair in served for pair in itertools_pairs(possible)):
                role = "web_api"
                evidence = docker["evidenceIds"] + [
                    identifier
                    for component in possible
                    for identifier in components[component]["evidenceIds"]
                ]
                candidate = {
                    "candidateId": "svc-" + digest({"dockerfile": path, "root": root})[:16],
                    "root": root,
                    "role": role,
                    "componentRoots": sorted(possible),
                    "evidenceIds": sorted(set(evidence)),
                }
                candidates.append(candidate)
                docker_targets.setdefault(path, set()).add(candidate["candidateId"])
                covered.update(possible)
    for root, info in sorted(components.items()):
        if info["role"] is not None and root not in covered:
            candidates.append(
                {
                    "candidateId": "svc-" + digest({"component": root})[:16],
                    "root": root,
                    "role": info["role"],
                    "componentRoots": [root],
                    "evidenceIds": info["evidenceIds"],
                }
            )
    # Docker facts are collected before Compose target identities exist. Bind
    # them now, preserving one copy per explicit deployment target. Shared build
    # context alone never grants another image's runtime observations.
    replacement = []
    for fact in observations.facts:
        provisional = fact.get("candidateId", "")
        if not provisional.startswith("docker-"):
            replacement.append(fact)
            continue
        docker_path = next(
            (item for item in dockerfiles if provisional == dockerfiles[item]["provisionalId"]), None
        )
        target_ids = docker_targets.get(docker_path, set())
        if not target_ids and docker_path is not None:
            component = dockerfiles[docker_path]["component"]
            target_ids = {
                candidate["candidateId"]
                for candidate in candidates
                if component in candidate["componentRoots"]
            }
        if target_ids:
            for target_id in sorted(target_ids):
                target_fact = copy.deepcopy(fact)
                target_fact["candidateId"] = target_id
                replacement.append(target_fact)
        else:
            value = dict(fact)
            value.pop("candidateId", None)
            replacement.append(value)
    observations.facts = replacement
    return sorted(candidates, key=lambda item: (item["root"], item["candidateId"]))


def _dockerfile(path: str, selection: Selection, observations: Observations, components: dict) -> dict:
    text = selection.snapshot.files[path].decode("utf-8-sig")
    instructions = []
    start = 1
    value = ""
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not value and (not stripped or stripped.startswith("#")):
            continue
        if not value:
            start = number
        value += (" " if value else "") + stripped.rstrip("\\").rstrip()
        if stripped.endswith("\\"):
            continue
        match = re.match(r"([A-Za-z]+)\s+(.*)", value)
        if match:
            instructions.append((match[1].upper(), match[2], start, number))
        value = ""
    stages = [index for index, instruction in enumerate(instructions) if instruction[0] == "FROM"]
    final = instructions[stages[-1] :] if stages else instructions
    workdir = next((value for name, value, _, _ in reversed(final) if name == "WORKDIR"), None)
    matching = [
        component
        for component in components
        if component != "." and workdir and workdir.rstrip("/").endswith("/" + component)
    ]
    runtime_component = max(matching, key=len) if matching else component_of(path, selection.roots)
    provisional = "docker-" + digest({"path": path})[:16]
    observations.current_candidate_id = provisional
    record = {
        "component": runtime_component,
        "workdir": workdir,
        "evidenceIds": [],
        "staticCopies": [],
        "copiedComponents": [],
        "database": False,
        "provisionalId": provisional,
        "imageRuntime": None,
    }
    entrypoint = next(
        (_command_text(value) for name, value, _, _ in reversed(final) if name == "ENTRYPOINT"), None
    )
    for name, value, start, end in final:
        evidence = observations.snippet(path, start, end)
        record["evidenceIds"].append(evidence)
        if name == "FROM":
            image = value.split()[0]
            record["database"] = bool(
                re.search(r"(?:^|/)(mongo|postgres|mysql|mariadb|redis)(?:[:@]|$)", image)
            )
            if re.search(r"(?:^|/)nginx(?:[:@]|$)", image):
                record["imageRuntime"] = "nginx"
            if image.startswith("node:"):
                observations.fact("runtime.name", "node", runtime_component, "container", [evidence])
                observations.fact("runtime.image", image, runtime_component, "container", [evidence])
        elif name == "WORKDIR":
            observations.fact("docker.workdir", value, runtime_component, "container", [evidence])
        elif name == "ENV":
            try:
                parts = shlex.split(value)
            except ValueError:
                parts = []
            if parts and "=" not in parts[0] and len(parts) > 1:
                parts = [parts[0] + "=" + " ".join(parts[1:])]
            for part in parts:
                key, separator, env_value = part.partition("=")
                if separator:
                    observations.fact("environment.key", key, runtime_component, "container", [evidence])
                    if key == "PORT" and env_value.isdigit() and 0 < int(env_value) < 65536:
                        observations.fact(
                            "runtime.port", int(env_value), runtime_component, "container", [evidence]
                        )
        elif name == "EXPOSE":
            for port in value.split():
                number = port.split("/")[0]
                if number.isdigit() and 0 < int(number) < 65536:
                    observations.fact("runtime.port", int(number), runtime_component, "container", [evidence])
                else:
                    observations.unknown(
                        "runtime.port",
                        "Docker EXPOSE uses an unresolved expression",
                        path=path,
                        component=runtime_component,
                        evidenceIds=[evidence],
                    )
        elif name == "CMD":
            command = _command_text(value)
            if command:
                observations.fact(
                    "start.command",
                    " ".join(filter(None, [entrypoint, command])),
                    runtime_component,
                    "container",
                    [evidence],
                )
        elif name == "COPY":
            for component in components:
                if component != "." and (
                    "/" + component + "/" in value
                    or re.search(r"(?:^|\s)" + re.escape(component) + r"(?:/|\s)", value)
                ):
                    record["copiedComponents"].append(component)
                    if "/" + component + "/dist" in value and components[component]["role"] == "static":
                        record["staticCopies"].append(component)
        elif name == "HEALTHCHECK":
            for url in re.findall(r"https?://[^\s'\"\]]+", value):
                observations.fact(
                    "healthcheck.path", urlsplit(url).path or "/", runtime_component, "container", [evidence]
                )
    # Build-stage commands are separate from the final executable stage.
    for name, value, start, end in instructions:
        if name == "RUN" and re.match(r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build(?:\s|$)", value):
            observations.fact(
                "docker.build.command",
                value,
                component_of(path, selection.roots),
                "source",
                [observations.snippet(path, start, end)],
            )
    if entrypoint and not any(name == "CMD" for name, _, _, _ in final):
        evidence = next(
            observations.snippet(path, start, end)
            for name, _, start, end in reversed(final)
            if name == "ENTRYPOINT"
        )
        observations.fact("start.command", entrypoint, runtime_component, "container", [evidence])
    observations.current_candidate_id = None
    return record


def _runtime_component(roots: list[str], components: dict, docker: dict) -> str:
    if docker.get("component") in roots:
        return docker["component"]
    servers = [root for root in roots if components[root]["role"] in {"api", "server"}]
    return servers[0] if len(servers) == 1 else roots[0] if len(roots) == 1 else "."


def _database_engine(name: str, service: dict, dockerfiles: dict) -> str | None:
    image = str(service.get("image", ""))
    command = _command(service.get("command")) or ""
    for marker, engine in (
        ("mongo", "mongodb"),
        ("postgres", "postgresql"),
        ("mariadb", "mysql"),
        ("mysql", "mysql"),
        ("redis", "redis"),
    ):
        if (
            re.search(r"(?:^|[/_-])" + marker + r"(?:[:@/_-]|$)", image)
            or command.startswith("mongod")
            and marker == "mongo"
            or name.lower() == marker
        ):
            return engine
    build = service.get("build")
    docker_name = build.get("dockerfile", "") if isinstance(build, dict) else ""
    if "mongo" in docker_name.lower():
        return "mongodb"
    return None


def _numeric_default(value) -> int | None:
    text = str(value)
    match = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-?(\d+)\}", text)
    if match:
        text = match[1]
    if text.isdigit() and 0 < int(text) < 65536:
        return int(text)
    return None


def _compose_port(value) -> tuple[int, int | None, str | None] | None:
    if isinstance(value, dict):
        target = _numeric_default(value.get("target"))
        published = _numeric_default(value.get("published"))
        host = value.get("host_ip")
        return (target, published, str(host) if host else None) if target is not None else None
    text = str(value).split("/")[0]
    # Replace interpolation with its declared numeric default before splitting.
    text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-?(\d+)\}", lambda m: m[2], text)
    if "$" in text:
        return None
    # Bracketed IPv6 host address does not delimit port segments.
    host = None
    if text.startswith("["):
        closing = text.find("]:")
        if closing < 0:
            return None
        host, text = text[1:closing], text[closing + 2 :]
    parts = text.rsplit(":", 2)
    if len(parts) == 3:
        host, published, target = parts
    elif len(parts) == 2:
        published, target = parts
    else:
        published, target = None, parts[0]
    target_port = _numeric_default(target)
    return (
        (target_port, _numeric_default(published) if published else None, host)
        if target_port is not None
        else None
    )


def _variables(value) -> list[str]:
    return sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", str(value))))


def _volume(value) -> tuple[str, str, str] | None:
    if isinstance(value, dict) and isinstance(value.get("target"), str):
        return str(value.get("source", "anonymous")), value["target"], str(value.get("type", "volume"))
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) == 1 and parts[0].startswith("/"):
        return "anonymous", parts[0], "volume"
    if len(parts) > 1:
        return parts[0], parts[1], "bind" if parts[0].startswith((".", "/")) else "volume"
    return None


def _command(value) -> str | None:
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return " ".join(shlex.quote(str(item)) for item in value)
    return value if isinstance(value, str) else None


def _command_text(value: str) -> str | None:
    if value.startswith("["):
        try:
            return _command(json.loads(value))
        except json.JSONDecodeError:
            return None
    return value


def _yaml_mapping(node) -> dict:
    if isinstance(node, yaml.MappingNode):
        return {key.value: value for key, value in node.value}
    return {}


def _yaml_lines(node) -> tuple[int, int]:
    return (
        (node.start_mark.line + 1, max(node.start_mark.line + 1, node.end_mark.line))
        if node is not None
        else (1, 1)
    )


def itertools_pairs(items):
    return {(a, b) for a in items for b in items if a != b}
