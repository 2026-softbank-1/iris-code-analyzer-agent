"""Frontend request origins and literal environment references."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ..javascript import UNKNOWN, lines, member
from ..observations import Observations
from ..selector import Selection, component_of
from ..snapshot import SOURCE_EXTENSIONS


def extract_connections(selection: Selection, observations: Observations) -> None:
    for path in selection.ordered():
        if PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        source = selection.source(path)
        component = component_of(path, selection.roots)
        for node, function, args in source.calls():
            function_name = source.text(function)
            pair = member(source, function)
            url_arg = args[0] if args else None
            if function_name == "fetch" and url_arg is not None:
                evidence_ids = [observations.snippet(path, *lines(url_arg))]
                value = _reference_value(selection, observations, source, url_arg, evidence_ids)
                if value is UNKNOWN and url_arg.type == "template_string":
                    # Stable base origin `${API_BASE}${endpoint}`: resolve only
                    # its first static/constant prefix, never execute expression.
                    prefix = ""
                    for child in url_arg.named_children:
                        if child.type == "string_fragment":
                            prefix += source.text(child)
                        elif child.type == "template_substitution":
                            expression = child.named_children[0] if child.named_children else None
                            fragment = _reference_value(
                                selection, observations, source, expression, evidence_ids
                            )
                            if isinstance(fragment, str):
                                prefix += fragment
                                if (
                                    expression is not None
                                    and expression.type == "identifier"
                                    and source.text(expression) in source.declarations
                                ):
                                    evidence_ids.append(
                                        observations.snippet(
                                            path, *lines(source.declarations[source.text(expression)])
                                        )
                                    )
                                    # The named base is the connection origin;
                                    # following literal endpoint text is a path.
                                    break
                            else:
                                break
                    value = prefix if prefix else UNKNOWN
                if isinstance(value, str) and (
                    value.startswith("/") or value.startswith(("https://", "http://"))
                ):
                    # Only URL origins/relative bases, never URI credentials.
                    parsed = urlsplit(value)
                    if parsed.username is not None or parsed.password is not None:
                        observations.unknown(
                            "frontend.connection",
                            "Frontend URL includes credentials; masked source requires review",
                            component=component,
                            path=path,
                            evidenceIds=evidence_ids,
                        )
                        continue
                    observations.fact(
                        "frontend.connection", {"baseUrl": value}, component, "source", evidence_ids
                    )
                else:
                    observations.unknown(
                        "frontend.connection",
                        "Frontend request URL cannot be resolved statically",
                        component=component,
                        path=path,
                        evidenceIds=evidence_ids,
                    )
            elif pair and pair[1] == "create" and pair[0] == "axios" and args:
                value = source.value(args[0])
                if isinstance(value, dict) and isinstance(value.get("baseURL"), str):
                    parsed = urlsplit(value["baseURL"])
                    if parsed.username is not None or parsed.password is not None:
                        observations.unknown(
                            "frontend.connection",
                            "Frontend URL includes credentials; masked source requires review",
                            component=component,
                            path=path,
                        )
                    else:
                        observations.fact(
                            "frontend.connection",
                            {"baseUrl": value["baseURL"]},
                            component,
                            "source",
                            [observations.snippet(path, *lines(args[0]))],
                        )


def _reference_value(selection, observations, source, node, evidence_ids, seen=frozenset()):
    value = source.value(node)
    if value is not UNKNOWN or node is None or node.type != "identifier":
        return value
    identifier = source.text(node)
    marker = (source.path, identifier)
    if marker in seen:
        return UNKNOWN
    for reference in source.imports():
        if identifier not in reference["bindings"]:
            continue
        target_path = selection.resolve(source.path, reference["target"])
        if target_path is None:
            continue
        target = selection.source(target_path)
        export_name = reference["bindings"][identifier]
        declaration = target.declarations.get(export_name)
        if export_name == "default":
            declaration = next(
                (
                    candidate.child_by_field_name("value")
                    for candidate in target.walk()
                    if candidate.type == "export_statement"
                    and target.text(candidate).startswith("export default ")
                ),
                None,
            )
        if declaration is not None:
            value = _reference_value(
                selection, observations, target, declaration, evidence_ids, seen | {marker}
            )
            if value is not UNKNOWN:
                evidence_ids.extend(
                    [
                        observations.snippet(source.path, *lines(reference["node"])),
                        observations.snippet(target_path, *lines(declaration)),
                    ]
                )
                return value
    return UNKNOWN
