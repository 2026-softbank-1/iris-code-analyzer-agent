"""Restricted lexical proof for direct Express routes and immutable strings.

No module is imported or expression evaluated. Any rebinding, mutation, nested
scope or unsupported expression affecting a proof leaves it deferred.
"""

from pathlib import PurePosixPath

from ...preprocess.javascript import UNKNOWN, Source, member
from ...preprocess.selector import component_of
from ...preprocess.snapshot import SOURCE_EXTENSIONS

_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "all"}


def _cites_call(field, immutable, path, function, argument):
    required = set(range(function.start_point.row + 1, function.end_point.row + 2))
    required.update(range(argument.start_point.row + 1, argument.end_point.row + 2))
    return all(immutable.cited(field["evidenceIds"], path, line, line) for line in required)


def _top_level(node):
    parent = node.parent
    allowed = {
        "variable_declarator",
        "lexical_declaration",
        "variable_declaration",
        "expression_statement",
        "export_statement",
        "parenthesized_expression",
    }
    while parent is not None and parent.type != "program":
        if parent.type not in allowed:
            return False
        parent = parent.parent
    return parent is not None and parent.type == "program"


_SIMPLE_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def _literal_string(source, node):
    """Decode only a bounded, explicit subset of JavaScript string escapes.

    Python literal decoding has different identity-escape semantics, and the
    legacy template helper preserves escape spellings. Neither is proof of a
    JavaScript value. Unicode/hex/octal/identity escapes and continuations are
    deliberately deferred until a JavaScript-specific decoder supports them.
    """
    if any(child.type == "template_substitution" for child in node.named_children):
        return UNKNOWN
    raw = source.text(node)
    # JavaScript normalizes literal template CR/CRLF line endings. Defer this
    # form rather than certify the source's raw carriage-return bytes as value.
    if node.type == "template_string" and "\r" in raw:
        return UNKNOWN
    if len(raw) < 2 or raw[0] not in {"'", '"', "`"} or raw[-1] != raw[0]:
        return UNKNOWN
    content, decoded, index = raw[1:-1], [], 0
    while index < len(content):
        char = content[index]
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        index += 1
        if index >= len(content) or content[index] not in _SIMPLE_ESCAPES:
            return UNKNOWN
        decoded.append(_SIMPLE_ESCAPES[content[index]])
        index += 1
    return "".join(decoded)


class LexicalProof:
    def __init__(self, path: str, raw: bytes):
        self.source = Source(path, raw)
        self.declarations: dict[str, list] = {}
        self.imports: dict[str, list] = {}
        self.unsafe: set[str] = set()
        for node in self.source.walk():
            if node.type == "variable_declarator":
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name is not None and name.type == "identifier":
                    key = self.source.text(name)
                    self.declarations.setdefault(key, []).append((node, value))
                    if not _top_level(node) or not self.source.text(node.parent).lstrip().startswith(
                        "const "
                    ):
                        self.unsafe.add(key)
                elif name is not None:
                    self.unsafe.update(
                        self.source.text(n)
                        for n in self.source.walk(name)
                        if n.type in {"identifier", "shorthand_property_identifier_pattern"}
                    )
            elif node.type in {
                "assignment_expression",
                "augmented_assignment_expression",
                "update_expression",
            }:
                left = node.child_by_field_name("left") or node.child_by_field_name("argument")
                if left is not None:
                    self.unsafe.update(
                        self.source.text(n) for n in self.source.walk(left) if n.type == "identifier"
                    )
            elif node.type in {
                "formal_parameters",
                "required_parameter",
                "optional_parameter",
                "catch_clause",
            }:
                self.unsafe.update(
                    self.source.text(n) for n in self.source.walk(node) if n.type == "identifier"
                )
            elif node.type == "arrow_function":
                parameter = node.child_by_field_name("parameter")
                if parameter is not None and parameter.type == "identifier":
                    self.unsafe.add(self.source.text(parameter))
            elif node.type in {"function_declaration", "class_declaration"}:
                name = node.child_by_field_name("name")
                if name is not None:
                    self.unsafe.add(self.source.text(name))
        for reference in self.source.imports():
            for local in reference["bindings"]:
                self.imports.setdefault(local, []).append(reference)
        for name, definitions in self.declarations.items():
            if len(definitions) != 1:
                self.unsafe.add(name)
        if self.source.tree.root_node.has_error:
            self.unsafe.update(self.declarations)
        if any(self.source.text(function) in {"eval", "Function"} for _, function, _ in self.source.calls()):
            self.unsafe.update(self.declarations)

    def declaration(self, name: str, before: int):
        entries = self.declarations.get(name, [])
        if name in self.unsafe or len(entries) != 1 or entries[0][0].end_byte >= before:
            return None
        return entries[0][1]

    def constant(self, node, before: int, seen=frozenset()):
        if node is None:
            return UNKNOWN
        if node.type in {"string", "template_string"}:
            return _literal_string(self.source, node)
        if node.type == "number":
            return self.source.value(node)
        if node.type == "identifier":
            name = self.source.text(node)
            if name in seen:
                return UNKNOWN
            return self.constant(self.declaration(name, before), before, seen | {name})
        if node.type == "parenthesized_expression" and len(node.named_children) == 1:
            return self.constant(node.named_children[0], before, seen)
        if node.type == "binary_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            operator = node.child_by_field_name("operator")
            if operator is not None and self.source.text(operator) == "+":
                a, b = self.constant(left, before, seen), self.constant(right, before, seen)
                if isinstance(a, str) and isinstance(b, str) and len(a) + len(b) <= 8192:
                    return a + b
        return UNKNOWN

    def express_factory(self, name: str, before: int) -> bool:
        if name in self.unsafe:
            return False
        refs = self.imports.get(name, [])
        if len(refs) != 1 or refs[0]["target"] != "express" or refs[0]["bindings"].get(name) != "default":
            return False
        ref = refs[0]
        if ref["node"].type == "import_statement":
            return _top_level(ref["node"]) and name not in self.declarations
        declaration = self.declaration(name, before)
        return (
            declaration is not None
            and self.source.text(declaration.child_by_field_name("function")) == "require"
            and "require" not in self.declarations
            and "require" not in self.unsafe
        )

    def receiver(self, name: str, before: int) -> bool:
        # const protects the binding, not the Express object's methods. Escape
        # through aliases or function arguments permits mutation we cannot prove.
        for node in self.source.walk():
            if node.type == "variable_declarator":
                assigned = node.child_by_field_name("value")
                if (
                    assigned is not None
                    and assigned.type == "identifier"
                    and self.source.text(assigned) == name
                ):
                    return False
        for _, _, args in self.source.calls():
            if any(
                any(
                    child.type == "identifier" and self.source.text(child) == name
                    for child in self.source.walk(arg)
                )
                for arg in args
            ):
                return False
        value = self.declaration(name, before)
        if value is None or value.type != "call_expression":
            return False
        function = value.child_by_field_name("function")
        args = value.child_by_field_name("arguments")
        return (
            function is not None
            and function.type == "identifier"
            and args is not None
            and not args.named_children
            and self.express_factory(self.source.text(function), value.start_byte)
        )


def verify_route(field: dict, immutable, roots: list[str]) -> tuple[str, str, list[str]]:
    value = field["value"]
    if (
        not isinstance(value, dict)
        or set(value) - {"method", "path", "component"}
        or field["scope"] != "source"
    ):
        return "rejected", "EVIDENCE_PREDICATE_MISMATCH", []
    if not isinstance(value.get("path"), str) or value.get("method", "").lower() not in _METHODS:
        return "rejected", "EVIDENCE_PREDICATE_MISMATCH", []
    unresolved = False
    inspected = []
    for path in immutable.paths(field["evidenceIds"]):
        if PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        if path not in immutable.files:
            unresolved = True
            continue
        if value.get("component", component_of(path, roots)) != component_of(path, roots):
            continue
        inspected.append(path)
        proof = LexicalProof(path, immutable.files[path])
        for node, function, args in proof.source.calls():
            pair = member(proof.source, function)
            if not pair or pair[1].lower() != value["method"].lower() or not args:
                continue
            if not _cites_call(field, immutable, path, function, args[0]):
                continue
            if not _top_level(node) or not proof.receiver(pair[0], node.start_byte):
                unresolved = True
                continue
            route = proof.constant(args[0], node.start_byte)
            if route is UNKNOWN:
                unresolved = True
            elif route == value["path"]:
                return "supported", "SOURCE_RELATION_VERIFIED", inspected
    return (
        ("deferred", "LEXICAL_RELATION_UNRESOLVED", inspected)
        if unresolved
        else ("rejected", "EVIDENCE_PREDICATE_MISMATCH", inspected)
    )


def unique_route_anchor(field: dict, immutable) -> bool:
    """A line-only unresolved locator must not stand for multiple registrations."""
    count = 0
    for path in immutable.paths(field["evidenceIds"]):
        if path not in immutable.files or PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        source = Source(path, immutable.files[path])
        for _, function, args in source.calls():
            pair = member(source, function)
            if (
                pair
                and pair[1].lower() in _METHODS
                and args
                and _cites_call(field, immutable, path, function, args[0])
            ):
                count += 1
    return count == 1


def verify_listener(field: dict, immutable, *, paths: list[str] | None = None) -> tuple[str, str, list[str]]:
    """Confirm the receiver and literal of a source listen declaration."""
    inspected = []
    unresolved = False
    for path in paths or immutable.paths(field["evidenceIds"]):
        if PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        if path not in immutable.files:
            unresolved = True
            continue
        inspected.append(path)
        proof = LexicalProof(path, immutable.files[path])
        for node, function, args in proof.source.calls():
            pair = member(proof.source, function)
            if not pair or pair[1] != "listen" or not args:
                continue
            if not _cites_call(field, immutable, path, function, args[0]):
                continue
            if not _top_level(node) or not proof.receiver(pair[0], node.start_byte):
                unresolved = True
                continue
            value = proof.constant(args[0], node.start_byte)
            if value is UNKNOWN:
                unresolved = True
            elif type(value) is int and value == field["value"]:
                return "supported", "SOURCE_RELATION_VERIFIED", inspected
    return (
        ("deferred", "LEXICAL_RELATION_UNRESOLVED", inspected)
        if unresolved
        else ("rejected", "EVIDENCE_PREDICATE_MISMATCH", inspected)
    )


def suspicious_listener(immutable, path: str, methods=frozenset({"listen"})) -> bool:
    """Flag definite lexical/receiver uncertainty in legacy extractor output."""
    proof = LexicalProof(path, immutable.files[path])
    for node, function, _ in proof.source.calls():
        pair = member(proof.source, function)
        if (
            pair
            and pair[1] in methods
            and (
                pair[0] in proof.unsafe
                or not _top_level(node)
                or (pair[0] in proof.declarations and not proof.receiver(pair[0], node.start_byte))
            )
        ):
            return True
    return False
