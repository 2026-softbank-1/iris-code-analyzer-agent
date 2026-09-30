"""Express receiver identities, router mounts and literal path composition."""

from __future__ import annotations

import itertools
from pathlib import PurePosixPath

from ..javascript import lines, member
from ..observations import Observations
from ..selector import Selection, component_of
from ..snapshot import SOURCE_EXTENSIONS

METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "all"}


def extract_express(selection: Selection, observations: Observations) -> None:
    receivers: dict[tuple[str, str], dict] = {}
    exports: dict[tuple[str, str], tuple[str, str]] = {}
    bindings = {}
    sources = {}
    for path in selection.ordered():
        if PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        source = selection.source(path)
        sources[path] = source
        imports = source.imports()
        factories = set()
        router_factories = set()
        for reference in imports:
            if reference["target"] == "express":
                for local, original in reference["bindings"].items():
                    if original in {"default", "*"}:
                        factories.add(local)
                    elif original == "Router":
                        router_factories.add(local)
            target_path = selection.resolve(path, reference["target"])
            if target_path:
                for local, original in reference["bindings"].items():
                    bindings[(path, local)] = (target_path, original)
        # CommonJS factory `const express = require('express')`.
        for local, value in source.declarations.items():
            if value.type != "call_expression":
                continue
            function = value.child_by_field_name("function")
            arguments = value.child_by_field_name("arguments")
            if (
                function is not None
                and source.text(function) == "require"
                and arguments is not None
                and arguments.named_children
                and source.value(arguments.named_children[0]) == "express"
            ):
                factories.add(local)
        for local, value in source.declarations.items():
            if value.type != "call_expression":
                continue
            function = value.child_by_field_name("function")
            if function is None:
                continue
            function_name = source.text(function)
            role = (
                "app"
                if function_name in factories
                else "router"
                if function_name in router_factories
                or any(function_name == name + ".Router" for name in factories)
                else None
            )
            if role:
                identifier = (path, local)
                receivers[identifier] = {
                    "role": role,
                    "routes": [],
                    "mounts": [],
                    "evidence": observations.snippet(path, *lines(value)),
                }
                declaration_parent = value.parent
                while declaration_parent is not None and declaration_parent.type not in {
                    "program",
                    "export_statement",
                }:
                    declaration_parent = declaration_parent.parent
                if declaration_parent is not None and declaration_parent.type == "export_statement":
                    exports[(path, local)] = identifier
        for node in source.walk():
            if node.type == "export_statement":
                text = source.text(node)
                value = node.child_by_field_name("value")
                if text.startswith("export default ") and value is not None and value.type == "identifier":
                    exports[(path, "default")] = (path, source.text(value))
                # Named `export { router as routes }` aliases.
                for child in node.named_children:
                    if child.type == "export_clause":
                        for specifier in child.named_children:
                            original = specifier.child_by_field_name("name")
                            alias = specifier.child_by_field_name("alias")
                            if original is not None:
                                exports[(path, source.text(alias or original))] = (
                                    path,
                                    source.text(original),
                                )
            elif node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is not None and right is not None and right.type == "identifier":
                    if source.text(left) == "module.exports":
                        exports[(path, "default")] = (path, source.text(right))
                    elif source.text(left).startswith("exports."):
                        exports[(path, source.text(left)[8:])] = (path, source.text(right))

    def identity(path: str, local: str):
        local_id = (path, local)
        if local_id in receivers:
            return local_id
        if local_id in bindings:
            target_path, exported = bindings[local_id]
            target = exports.get((target_path, exported))
            if target in receivers:
                return target
        return None

    for path, source in sources.items():
        component = component_of(path, selection.roots)
        for node, function, args in source.calls():
            pair = member(source, function)
            if pair is None:
                continue
            receiver_name, method = pair
            receiver = identity(path, receiver_name)
            route_arg = None
            # Express chained router.route('/path').get(handler).
            receiver_node = function.child_by_field_name("object")
            chained_call = receiver_node
            while receiver is None and chained_call is not None and chained_call.type == "call_expression":
                chained_fn = chained_call.child_by_field_name("function")
                chained_args = chained_call.child_by_field_name("arguments")
                chained_pair = member(source, chained_fn) if chained_fn is not None else None
                if (
                    chained_pair
                    and chained_pair[1] == "route"
                    and chained_args is not None
                    and chained_args.named_children
                ):
                    receiver = identity(path, chained_pair[0])
                    route_arg = chained_args.named_children[0]
                    break
                chained_call = (
                    chained_fn.child_by_field_name("object")
                    if chained_fn is not None and chained_fn.type == "member_expression"
                    else None
                )
            if receiver is None:
                continue
            evidence = observations.snippet(path, *lines(route_arg or args[0] if args else node))
            if method in METHODS:
                route_arg = route_arg or (args[0] if args else None)
                paths = _literal_paths(source.value(route_arg))
                if paths is None:
                    observations.unknown(
                        "runtime.httpRoutes",
                        "Express route path cannot be resolved statically",
                        path=path,
                        component=component,
                        evidenceIds=[evidence],
                    )
                else:
                    receivers[receiver]["routes"].append(
                        {"method": method.upper(), "paths": paths, "evidence": evidence}
                    )
            elif method == "use":
                if not args:
                    continue
                prefix = _literal_paths(source.value(args[0]))
                mounted = []
                for argument in args[1:] if prefix is not None else args:
                    # Middleware arrays may contain imported routers.
                    nodes = argument.named_children if argument.type == "array" else [argument]
                    for handler in nodes:
                        if handler.type == "identifier":
                            target = identity(path, source.text(handler))
                            if target is not None:
                                mounted.append(target)
                if mounted:
                    if (
                        prefix is None
                        and args[0].type == "identifier"
                        and identity(path, source.text(args[0])) is not None
                    ):
                        prefix = [""]
                    if prefix is None:
                        observations.unknown(
                            "runtime.httpRoutes",
                            "Express router mount prefix is dynamic",
                            path=path,
                            component=component,
                            evidenceIds=[evidence],
                        )
                    else:
                        for target in mounted:
                            receivers[receiver]["mounts"].append(
                                {"target": target, "prefixes": prefix, "evidence": evidence}
                            )
            elif method == "listen":
                if args:
                    port = source.value(args[0])
                    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
                        observations.fact(
                            "runtime.port",
                            port,
                            component,
                            "container",
                            [evidence, receivers[receiver]["evidence"]],
                        )
                    if len(args) > 1:
                        host = source.value(args[1])
                        if isinstance(host, str):
                            observations.fact(
                                "runtime.host",
                                host,
                                component,
                                "production",
                                [
                                    observations.snippet(path, *lines(args[1])),
                                    receivers[receiver]["evidence"],
                                ],
                            )
            elif method == "static":
                pass
            if method == "use" and any(".static(" in source.text(argument) for argument in args):
                # Only an actual express.static AST call establishes this link.
                for argument in args:
                    if argument.type != "call_expression":
                        continue
                    fn = argument.child_by_field_name("function")
                    if fn is None or not any(
                        source.text(fn) == name + ".static" for name in _express_factory_names(source)
                    ):
                        continue
                    frontends = [
                        root
                        for root, (_, package) in selection.packages.items()
                        if "vite" in {**package.get("dependencies", {}), **package.get("devDependencies", {})}
                    ]
                    for frontend in frontends:
                        observations.relation(
                            "serves_static",
                            component,
                            frontend,
                            [observations.snippet(path, *lines(argument)), receivers[receiver]["evidence"]],
                        )

    def traverse(receiver, prefixes: list[str], evidence_ids: list[str], seen: frozenset[tuple[str, str]]):
        if receiver in seen:
            observations.unknown(
                "runtime.httpRoutes",
                "Express router mount cycle needs code review",
                path=receiver[0],
                component=component_of(receiver[0], selection.roots),
            )
            return
        item = receivers[receiver]
        component = component_of(receiver[0], selection.roots)
        for route in item["routes"]:
            for prefix, route_path in itertools.product(prefixes, route["paths"]):
                path = _join_path(prefix, route_path)
                ids = evidence_ids + [route["evidence"], item["evidence"]]
                observations.fact(
                    "api.route", {"method": route["method"], "path": path}, component, "source", ids
                )
                if route["method"] == "GET" and ("health" in path or path in {"/livez", "/readyz"}):
                    observations.fact("healthcheck.path", path, component, "production", ids)
        for mount in item["mounts"]:
            next_prefixes = [
                _join_path(prefix, path) for prefix, path in itertools.product(prefixes, mount["prefixes"])
            ]
            traverse(
                mount["target"],
                next_prefixes,
                evidence_ids + [mount["evidence"], item["evidence"]],
                seen | {receiver},
            )

    for receiver, item in sorted(receivers.items()):
        if item["role"] == "app":
            traverse(receiver, [""], [], frozenset())


def _literal_paths(value) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    return None


def _join_path(prefix: str, path: str) -> str:
    combined = prefix.rstrip("/") + "/" + path.lstrip("/")
    return combined or "/"


def _express_factory_names(source) -> set[str]:
    return {
        local
        for reference in source.imports()
        if reference["target"] == "express"
        for local, original in reference["bindings"].items()
        if original in {"default", "*"}
    } | {
        local
        for local, value in source.declarations.items()
        if source.text(value).startswith("require(") and "express" in source.text(value)
    }
