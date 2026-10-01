"""Supplemental build and environment ownership facts from immutable source.

This module only inspects captured bytes. Commands are declarations, never
executed; install defaults and standalone Docker contexts are marked policy.
Environment values never leave this module.
"""

from __future__ import annotations

import copy
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import yaml

from iris_analyzer.contracts import digest

from ..javascript import UNKNOWN, member
from ..observations import Observations
from ..selector import Selection, component_of
from ..snapshot import SOURCE_EXTENSIONS, is_env_example

_LOCKS = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_repository_path(base: str, value: object) -> str | None:
    """Resolve declared POSIX paths without allowing host/remote/dynamic paths."""
    if (
        not isinstance(value, str)
        or not value
        or any(c in value for c in ("\\", "\x00", "$", ":"))
        or value.startswith("/")
    ):
        return None
    result = posixpath.normpath(posixpath.join(base, value))
    return None if result == ".." or result.startswith(("../", "/")) else result


def _condition(path: str) -> str | None:
    return (
        None
        if PurePosixPath(path).name.lower()
        in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
        else f"when Compose file {path} is selected"
    )


def _compose(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name.endswith((".yaml", ".yml")) and name.startswith(("compose.", "docker-compose."))


def _env(
    observations: Observations,
    key: str,
    component: str,
    evidence: str,
    *,
    service: str | None = None,
    phase: str = "runtime",
    origin: str = "source",
    required: bool | None = None,
    condition: str | None = None,
) -> None:
    if not _ENV_KEY.fullmatch(key):
        return
    observations.fact(
        "environment.consumer",
        {
            "key": key,
            "component": component,
            "serviceName": service,
            "phase": phase,
            "origin": origin,
            "required": required,
            "condition": condition,
        },
        "." if component.startswith("compose:") else component,
        "source",
        [evidence],
        condition,
    )


def _package(selection: Selection, observations: Observations, root: str) -> tuple[dict, list[str]]:
    path, package = selection.packages[root]
    evidence = [observations.snippet(path)]
    locks = sorted(
        p
        for p in selection.selected
        if PurePosixPath(p).parent.as_posix() == root and PurePosixPath(p).name in _LOCKS
    )
    managers = {_LOCKS[PurePosixPath(p).name] for p in locks}
    declared = package.get("packageManager")
    match = re.fullmatch(r"(npm|pnpm|yarn|bun)@(.+)", declared) if isinstance(declared, str) else None
    manager = match[1] if match else next(iter(managers)) if len(managers) == 1 else None
    version = match[2] if match else None
    unresolved = []
    if len(managers) > 1 or manager is not None and managers and managers != {manager}:
        manager = None
        unresolved.append("Conflicting package manager declarations/lockfiles require selection.")
    # npm is only a policy fallback; no package-manager declaration is invented.
    install = (
        {
            "npm": "npm ci --ignore-scripts",
            "pnpm": "pnpm install --frozen-lockfile --ignore-scripts",
            "yarn": "yarn install --immutable",
            "bun": "bun install --frozen-lockfile --ignore-scripts",
        }.get(manager)
        if locks
        else None
    )
    if manager == "yarn" and (version is None or version.startswith("1.")):
        install = (
            "yarn install --frozen-lockfile --ignore-scripts"
            if version and version.startswith("1.")
            else None
        )
    scripts = package.get("scripts", {})
    command = f"{manager or 'npm'} run build" if isinstance(scripts.get("build"), str) else None
    for lock in locks:
        evidence.append(observations.snippet(lock, 1, 6))
    outputs = sorted(
        {
            fact["value"]
            for fact in observations.facts
            if fact["key"] == "output.directory"
            and fact["component"] == root
            and isinstance(fact["value"], str)
        }
    )
    return {
        "packageManager": manager,
        "packageManagerVersion": version,
        "lockfiles": locks,
        "installCommand": install,
        "installCommandBasis": "policy" if install else "unknown",
        "buildCommand": command,
        "buildCommandBasis": "policy" if command else "unknown",
        "buildWorkingDirectory": root if command else None,
        "outputPaths": outputs,
        "unresolved": unresolved,
    }, evidence


def _empty_target(root: str) -> dict:
    return {
        "component": root,
        "serviceName": None,
        "composePath": None,
        "contextPath": root,
        "contextBasis": "package_directory",
        "dockerfilePath": None,
        "target": None,
        "status": "detected",
        "condition": None,
        "steps": [],
        "runtimeWorkingDirectory": None,
        "packageManager": None,
        "packageManagerVersion": None,
        "lockfiles": [],
        "installCommand": None,
        "installCommandBasis": "unknown",
        "buildCommand": None,
        "buildCommandBasis": "unknown",
        "buildWorkingDirectory": None,
        "outputPaths": [],
        "unresolved": [],
    }


def _docker(selection: Selection, observations: Observations, path: str) -> tuple[dict, list[str]]:
    root = PurePosixPath(path).parent.as_posix()
    target = _empty_target(root)
    target.update(
        dockerfilePath=path,
        contextBasis="root_default_policy" if root == "." else "unresolved",
        contextPath="." if root == "." else None,
    )
    if root != ".":
        target["unresolved"].append(
            "A Dockerfile location does not declare its build context; select a context or Compose build."
        )
    text = selection.snapshot.files[path].decode("utf-8-sig")
    if root in selection.packages and "package.json" in text:
        package, package_evidence = _package(selection, observations, root)
        target.update(package)
    else:
        package_evidence = []
    evidence = list(package_evidence)
    value, start, index, stage, workdir = "", 1, -1, None, None
    inherited = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not value and (not stripped or stripped.startswith("#")):
            continue
        if not value:
            start = line_no
        value += (" " if value else "") + stripped.removesuffix("\\").rstrip()
        if stripped.endswith("\\"):
            continue
        match = re.fullmatch(r"([A-Za-z]+)\s+(.*)", value)
        value = ""
        if not match:
            continue
        name, argument = match[1].upper(), match[2]
        ev = observations.snippet(path, start, line_no)
        evidence.append(ev)
        if name == "FROM":
            if stage is not None:
                inherited[stage.lower()] = workdir
                inherited[str(index)] = workdir
            parsed = re.fullmatch(r"(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", argument, re.I)
            index += 1
            stage = parsed[2] or f"stage-{index + 1}" if parsed else f"stage-{index + 1}"
            workdir = inherited.get(parsed[1].lower()) if parsed else None
        elif name == "WORKDIR":
            if "$" in argument or "\\" in argument:
                workdir = None
            elif argument.startswith("/"):
                workdir = posixpath.normpath(argument)
            elif workdir:
                workdir = posixpath.normpath(posixpath.join(workdir, argument))
            else:
                workdir = None
        elif name == "RUN":
            phase = (
                "build"
                if re.search(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build(?:\s|$)", argument)
                else "install"
                if re.search(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:ci|install)(?:\s|$)", argument)
                else "other"
            )
            # Observations applies normal masking to command values.
            target["steps"].append(
                {
                    "phase": phase,
                    "command": argument,
                    "workingDirectory": workdir,
                    "stage": stage,
                    "evidenceIds": [ev],
                }
            )
        elif name in {"ENV", "ARG"}:
            try:
                parts = shlex.split(argument)
            except ValueError:
                parts = []
            for part in parts:
                key = part.split("=", 1)[0]
                _env(
                    observations,
                    key,
                    root,
                    ev,
                    phase="build" if name == "ARG" else "unknown",
                    origin="dockerfile",
                    required=False if "=" in part else None,
                )
    target["runtimeWorkingDirectory"] = workdir
    target["buildCommand"] = None
    target["buildCommandBasis"] = "unknown"
    target["buildWorkingDirectory"] = None
    builds = [step for step in target["steps"] if step["phase"] == "build"]
    if builds:
        target["buildCommand"] = builds[0]["command"] if len(builds) == 1 else None
        target["buildCommandBasis"] = "declared" if len(builds) == 1 else "unknown"
        target["buildWorkingDirectory"] = (
            builds[0]["workingDirectory"] if len({s["workingDirectory"] for s in builds}) == 1 else None
        )
    target["status"] = "needs_input" if target["unresolved"] else "detected"
    return target, evidence


def extract_execution(selection: Selection, observations: Observations) -> None:
    """Attach safe supplemental facts without changing analysis-result v1."""
    observations.current_candidate_id = None
    observations.current_condition = None
    targets = []
    dockerfiles = {}
    for path in selection.ordered():
        if PurePosixPath(path).name.lower().startswith("dockerfile"):
            dockerfiles[path] = _docker(selection, observations, path)
    for root in sorted(selection.packages):
        value = _empty_target(root)
        package, evidence = _package(selection, observations, root)
        value.update(package)
        value["status"] = "needs_input" if value["unresolved"] else "detected"
        targets.append((value, evidence))
    referenced = set()
    for path in selection.ordered():
        if not _compose(path):
            continue
        try:
            doc = yaml.safe_load(selection.snapshot.files[path])
        except yaml.YAMLError:
            continue
        services = doc.get("services", {}) if isinstance(doc, dict) else {}
        if not isinstance(services, dict):
            continue
        evidence = observations.snippet(path)
        for name, service in sorted(services.items()):
            if not isinstance(name, str) or not isinstance(service, dict):
                continue
            build = service.get("build")
            context = safe_repository_path(
                posixpath.dirname(path),
                build
                if isinstance(build, str)
                else build.get("context", ".")
                if isinstance(build, dict)
                else None,
            )
            docker_path = (
                safe_repository_path(
                    context,
                    build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile",
                )
                if context is not None
                else None
            )
            # Image-only services and database containers never inherit the app
            # component just because a Compose file is at repository root.
            image = str(service.get("image", ""))
            database = bool(
                re.search(
                    r"(?:mongo|postgres|mariadb|mysql|redis)", image + " " + str(docker_path or ""), re.I
                )
            )
            component = "compose:" + name if context is None or database else context
            condition = _condition(path)
            environment = service.get("environment", {})
            if isinstance(environment, list):
                environment = {
                    part.split("=", 1)[0]: part.split("=", 1)[1] if "=" in part else None
                    for part in environment
                    if isinstance(part, str)
                }
            if isinstance(environment, dict):
                for key, value in sorted(environment.items(), key=lambda pair: str(pair[0])):
                    text = str(value) if value is not None else ""
                    required = (
                        True
                        if re.search(r"\$\{\w+:?\?", text)
                        else False
                        if value is not None and ("$" not in text or re.search(r"\$\{\w+:?-", text))
                        else None
                    )
                    _env(
                        observations,
                        str(key),
                        component,
                        evidence,
                        service=name,
                        origin="compose",
                        required=required,
                        condition=condition,
                    )
                    # Literal URI connections expose endpoint metadata only, not
                    # credentials or query strings. Interpolated hosts unknown.
                    if isinstance(value, str) and "://" in value:
                        try:
                            parsed = urlsplit(re.sub(r"\$\{[^}]*\}", "INTERPOLATED", value))
                            host = parsed.hostname
                            port = parsed.port
                        except ValueError:
                            continue
                        if host in services and "interpolated" not in host.lower():
                            observations.fact(
                                "deployment.connection",
                                {
                                    "fromComponent": component,
                                    "fromService": name,
                                    "toService": host,
                                    "protocol": parsed.scheme,
                                    "port": port,
                                    "environmentKey": str(key),
                                    "condition": condition,
                                },
                                "." if component.startswith("compose:") else component,
                                "container",
                                [evidence],
                                condition,
                            )
            if not isinstance(build, (str, dict)):
                continue
            target, docker_evidence = copy.deepcopy(
                dockerfiles.get(docker_path, (_empty_target(component), []))
            )
            target.update(
                component=component,
                serviceName=name,
                composePath=path,
                contextPath=context,
                contextBasis="compose",
                dockerfilePath=docker_path,
                target=build.get("target")
                if isinstance(build, dict) and isinstance(build.get("target"), str)
                else None,
                condition=condition,
            )
            target["unresolved"] = [
                item for item in target["unresolved"] if not item.startswith("A Dockerfile location")
            ]
            if context is None or docker_path is None:
                target["unresolved"].append(
                    "Compose build paths are remote, dynamic or leave the immutable repository."
                )
            elif docker_path not in dockerfiles:
                target["unresolved"].append(
                    "The selected Dockerfile is not present in supplied immutable source."
                )
            if target["target"]:
                target["unresolved"].append(
                    "An explicit Docker stage target requires stage-specific runtime verification."
                )
            target["status"] = "needs_input" if target["unresolved"] else "detected"
            targets.append((target, [evidence, *docker_evidence]))
            referenced.add(docker_path)
    targets.extend(value for path, value in sorted(dockerfiles.items()) if path not in referenced)
    for target, evidence in targets:
        target["id"] = (
            "build-"
            + digest(
                {
                    key: target[key]
                    for key in ("component", "composePath", "serviceName", "dockerfilePath", "target")
                }
            )[:16]
        )
        observations.fact(
            "deployment.build_target",
            target,
            "." if target["component"].startswith("compose:") else target["component"],
            "source",
            evidence,
            target["condition"],
        )
    for path in selection.ordered():
        component = component_of(path, selection.roots)
        if is_env_example(path):
            for number, line in enumerate(selection.snapshot.files[path].decode("utf-8-sig").splitlines(), 1):
                match = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=", line)
                if match:
                    _env(
                        observations,
                        match[1],
                        component,
                        observations.snippet(path, number, number),
                        phase="unknown",
                        origin="example",
                    )
        elif PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS:
            source = selection.source(path)
            for node in source.walk():
                pair = member(source, node) if node.type == "member_expression" else None
                if node.type == "subscript_expression":
                    obj, index = node.child_by_field_name("object"), node.child_by_field_name("index")
                    pair = (source.text(obj), source.value(index)) if obj is not None else None
                if pair and pair[0] in {"process.env", "import.meta.env"} and isinstance(pair[1], str):
                    parent = node.parent
                    optional = (
                        parent is not None
                        and parent.type == "binary_expression"
                        and parent.child_by_field_name("left") == node
                        and source.text(parent.child_by_field_name("operator")) in {"??", "||"}
                        and source.value(parent.child_by_field_name("right")) not in (None, UNKNOWN)
                    )
                    _env(
                        observations,
                        pair[1],
                        component,
                        observations.snippet(path, node.start_point.row + 1, node.end_point.row + 1),
                        phase="build" if pair[0] == "import.meta.env" else "runtime",
                        required=False if optional else None,
                    )
            for node, function, args in source.calls():
                pair = member(source, function)
                if not pair or pair[1] != "parse" or not args or source.text(args[0]) != "process.env":
                    continue
                receiver = function.child_by_field_name("object")
                if receiver is None or receiver.type != "call_expression":
                    continue
                inner_fn, inner_args = (
                    receiver.child_by_field_name("function"),
                    receiver.child_by_field_name("arguments"),
                )
                if (
                    inner_fn is None
                    or source.text(inner_fn) != "z.object"
                    or inner_args is None
                    or not inner_args.named_children
                ):
                    continue
                for prop in inner_args.named_children[0].named_children:
                    if prop.type == "pair":
                        key = source.text(prop.child_by_field_name("key")).strip("\"'")
                        value = source.text(prop.child_by_field_name("value"))
                        _env(
                            observations,
                            key,
                            component,
                            observations.snippet(path, prop.start_point.row + 1, prop.end_point.row + 1),
                            required=not any(
                                token in value for token in (".optional(", ".default(", ".catch(")
                            ),
                        )
