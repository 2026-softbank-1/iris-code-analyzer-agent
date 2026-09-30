"""Node manifests, workspaces, Vite build/dev configuration and environment."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from ..javascript import lines, member
from ..observations import Observations
from ..selector import Selection, component_of
from ..snapshot import is_env_example


def extract_node(selection: Selection, observations: Observations) -> dict[str, dict]:
    components = {}
    for root, (path, package) in sorted(selection.packages.items()):
        evidence = observations.snippet(path)
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        scripts = package.get("scripts", {})
        framework = "express" if "express" in dependencies else "vite" if "vite" in dependencies else None
        role = (
            "api"
            if framework == "express"
            else "static"
            if framework == "vite"
            else "server"
            if "start" in scripts
            else None
        )
        components[root] = {
            "role": role,
            "evidenceIds": [evidence],
            "framework": framework,
            "package": package,
        }
        observations.fact("runtime.name", "node", root, "source", [evidence])
        if isinstance(package.get("name"), str):
            observations.fact("package.name", package["name"], root, "source", [evidence])
        if framework:
            observations.fact("framework", framework, root, "source", [evidence])
        workspaces = package.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        if isinstance(workspaces, list) and workspaces:
            observations.fact("node.workspaces", workspaces, root, "source", [evidence])
        if isinstance(scripts.get("build"), str):
            observations.fact("build.command", scripts["build"], root, "source", [evidence])
        elif role is not None:
            observations.fact("build.command", "none", root, "source", [evidence])
        if isinstance(scripts.get("start"), str):
            observations.fact("start.command", scripts["start"], root, "source", [evidence])
        if framework == "vite":
            vite_config = [
                item
                for item in selection.selected
                if component_of(item, selection.roots) == root
                and PurePosixPath(item).name.startswith("vite.config.")
            ]
            default_evidence = [evidence]
            output = "dist"
            for config_path in sorted(vite_config):
                source = selection.source(config_path)
                for node in source.walk():
                    if node.type != "pair":
                        continue
                    key_node = node.child_by_field_name("key")
                    key = source.text(key_node).strip("\"'")
                    value_node = node.child_by_field_name("value")
                    value = source.value(value_node)
                    ancestor_keys = _ancestor_keys(source, node)
                    if key == "outDir" and "build" in ancestor_keys:
                        if isinstance(value, str):
                            output = value
                            default_evidence = [observations.snippet(config_path, *lines(node))]
                        else:
                            output = None
                            observations.unknown(
                                "output.directory",
                                "Vite output directory uses a dynamic expression",
                                component=root,
                                path=config_path,
                            )
                    elif key == "port" and any(item in ancestor_keys for item in {"server", "preview"}):
                        if isinstance(value, int) and 0 < value < 65536:
                            observations.fact(
                                "runtime.port",
                                value,
                                root,
                                "development",
                                [observations.snippet(config_path, *lines(node))],
                            )
                    elif key == "host" and "server" in ancestor_keys and isinstance(value, str):
                        observations.fact(
                            "runtime.host",
                            value,
                            root,
                            "development",
                            [observations.snippet(config_path, *lines(node))],
                        )
                    elif key == "target" and "proxy" in ancestor_keys and isinstance(value, str):
                        observations.fact(
                            "frontend.proxy",
                            {"target": value},
                            root,
                            "development",
                            [observations.snippet(config_path, *lines(node))],
                        )
            if output is not None:
                observations.fact("output.directory", output, root, "production", default_evidence)
            for name in ("dev", "preview"):
                command = scripts.get(name, "")
                if not isinstance(command, str):
                    continue
                match = re.search(r"(?:^|\s)--port(?:=|\s+)(\d+)(?:\s|$)", command)
                if match and 0 < int(match[1]) < 65536:
                    observations.fact("runtime.port", int(match[1]), root, "development", [evidence])
                match = re.search(r"(?:^|\s)--host(?:=|\s+)([^\s]+)", command)
                if match:
                    observations.fact("runtime.host", match[1], root, "development", [evidence])
        for engine in (
            "mongoose",
            "mongodb",
            "pg",
            "postgres",
            "mysql",
            "mysql2",
            "better-sqlite3",
            "redis",
            "ioredis",
        ):
            if engine in dependencies:
                name = (
                    "mongodb"
                    if engine in {"mongoose", "mongodb"}
                    else "postgresql"
                    if engine in {"pg", "postgres"}
                    else "mysql"
                    if engine.startswith("mysql")
                    else "sqlite"
                    if "sqlite" in engine
                    else "redis"
                )
                observations.fact(
                    "dependency.database", {"name": name, "engine": name}, root, "source", [evidence]
                )
    for path in selection.ordered():
        kind = selection.snapshot.files[path]
        root = component_of(path, selection.roots)
        if is_env_example(path):
            text = kind.decode("utf-8-sig")
            for line_number, line in enumerate(text.splitlines(), 1):
                match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                if match:
                    observations.fact(
                        "environment.key",
                        match[1],
                        root,
                        "source",
                        [observations.snippet(path, line_number, line_number)],
                    )
        elif PurePosixPath(path).suffix.lower() in {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".mts",
            ".cts",
        }:
            source = selection.source(path)
            for node in source.walk():
                if node.type == "member_expression":
                    pair = member(source, node)
                    if pair and pair[0] in {"process.env", "import.meta.env"}:
                        observations.fact(
                            "environment.key",
                            pair[1],
                            root,
                            "source",
                            [observations.snippet(path, *lines(node))],
                        )
                elif node.type == "subscript_expression":
                    obj = node.child_by_field_name("object")
                    index = node.child_by_field_name("index")
                    if obj is not None and source.text(obj) in {"process.env", "import.meta.env"}:
                        key = source.value(index)
                        if isinstance(key, str):
                            observations.fact(
                                "environment.key",
                                key,
                                root,
                                "source",
                                [observations.snippet(path, *lines(node))],
                            )
            # Zod env schemas are explicit environment key declarations. Only
            # a z.object(...).parse(process.env) chain supplies this meaning.
            for node, function, args in source.calls():
                pair = member(source, function)
                if not pair or pair[1] != "parse" or not args or source.text(args[0]) != "process.env":
                    continue
                receiver = function.child_by_field_name("object")
                if receiver is None or receiver.type != "call_expression":
                    continue
                inner_fn = receiver.child_by_field_name("function")
                inner_args = receiver.child_by_field_name("arguments")
                if (
                    inner_fn is None
                    or source.text(inner_fn) != "z.object"
                    or inner_args is None
                    or not inner_args.named_children
                ):
                    continue
                env_object = inner_args.named_children[0]
                for prop in env_object.named_children:
                    if prop.type == "pair":
                        key = source.text(prop.child_by_field_name("key")).strip("\"'")
                        observations.fact(
                            "environment.key", key, root, "source", [observations.snippet(path, *lines(prop))]
                        )
        elif PurePosixPath(path).name in {
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
        }:
            evidence = observations.snippet(path, 1, min(12, len(kind.splitlines())))
            name = (
                "npm"
                if "package" in path or "shrinkwrap" in path
                else "pnpm"
                if "pnpm" in path
                else "yarn"
                if "yarn" in path
                else "bun"
            )
            observations.fact("package.manager", name, root, "source", [evidence])
        elif PurePosixPath(path).name.startswith("tsconfig"):
            try:
                config = json.loads(kind)
            except (ValueError, UnicodeDecodeError):
                continue
            output = config.get("compilerOptions", {}).get("outDir")
            if isinstance(output, str):
                observations.fact(
                    "output.directory",
                    output.removeprefix("./"),
                    root,
                    "production",
                    [observations.snippet(path)],
                )
    return components


def _ancestor_keys(source, node) -> list[str]:
    keys = []
    current = node.parent
    while current is not None:
        if current.type == "pair":
            key = current.child_by_field_name("key")
            if key is not None:
                keys.append(source.text(key).strip("\"'"))
        current = current.parent
    return keys
