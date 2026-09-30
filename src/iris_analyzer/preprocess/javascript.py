"""Small AST helpers shared by reference selection and supported extractors.

The parser reads JavaScript/TypeScript syntax only. It never imports a module,
evaluates a configuration expression or calls a package manager.
"""

from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import PurePosixPath

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

UNKNOWN = object()


@lru_cache(maxsize=3)
def parser(flavor: str) -> Parser:
    if flavor == "tsx":
        language = Language(tree_sitter_typescript.language_tsx())
    elif flavor == "ts":
        language = Language(tree_sitter_typescript.language_typescript())
    else:
        language = Language(tree_sitter_javascript.language())
    return Parser(language)


class Source:
    def __init__(self, path: str, raw: bytes):
        self.path = path
        self.raw = raw
        suffix = PurePosixPath(path).suffix.lower()
        self.tree = parser(
            "tsx" if suffix == ".tsx" else "ts" if suffix in {".ts", ".mts", ".cts"} else "js"
        ).parse(raw)
        self.constants: dict[str, object] = {}
        self.declarations = {}
        for node in self.walk():
            if node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
                if name is not None and name.type == "identifier" and value is not None:
                    self.declarations[self.text(name)] = value
                    resolved = self.value(value)
                    if resolved is not UNKNOWN:
                        self.constants[self.text(name)] = resolved

    def text(self, node) -> str:
        return self.raw[node.start_byte : node.end_byte].decode("utf-8-sig")

    def walk(self, node=None):
        stack = [node or self.tree.root_node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.named_children))

    def value(self, node, seen: frozenset[str] = frozenset()):
        if node is None:
            return UNKNOWN
        if node.type in {"string", "template_string"}:
            if any(child.type == "template_substitution" for child in node.named_children):
                return UNKNOWN
            source = self.text(node)
            if source[0] == "`":
                return source[1:-1]
            try:
                return json.loads(source) if source.startswith('"') else ast.literal_eval(source)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                return UNKNOWN
        if node.type == "number":
            try:
                numeric = self.text(node).replace("_", "")
                return (
                    float(numeric)
                    if "." in numeric
                    else int(numeric, 0)
                    if numeric.startswith(("0x", "0o", "0b"))
                    else int(numeric)
                )
            except ValueError:
                return UNKNOWN
        if node.type in {"true", "false"}:
            return node.type == "true"
        if node.type == "null":
            return None
        if node.type == "identifier":
            name = self.text(node)
            if name in self.constants:
                return self.constants[name]
            if name in self.declarations and name not in seen:
                return self.value(self.declarations[name], seen | {name})
            return UNKNOWN
        if node.type == "array":
            values = [self.value(child, seen) for child in node.named_children if child.type != "comment"]
            return UNKNOWN if any(item is UNKNOWN for item in values) else values
        if node.type == "object":
            result = {}
            for child in node.named_children:
                if child.type != "pair":
                    continue
                key = child.child_by_field_name("key")
                key_value = self.value(key, seen) if key.type == "string" else self.text(key)
                result[key_value] = self.value(child.child_by_field_name("value"), seen)
            return result
        if (
            node.type in {"parenthesized_expression", "as_expression", "non_null_expression"}
            and node.named_children
        ):
            return self.value(node.named_children[0], seen)
        return UNKNOWN

    def imports(self) -> list[dict]:
        result = []
        for node in self.walk():
            if node.type == "import_statement":
                source = node.child_by_field_name("source")
                target = self.value(source)
                if not isinstance(target, str):
                    continue
                bindings = {}
                for child in node.named_children:
                    if child.type != "import_clause":
                        continue
                    for item in child.named_children:
                        if item.type == "identifier":
                            bindings[self.text(item)] = "default"
                        elif item.type == "namespace_import":
                            names = [part for part in item.named_children if part.type == "identifier"]
                            if names:
                                bindings[self.text(names[-1])] = "*"
                        elif item.type == "named_imports":
                            for specifier in item.named_children:
                                if specifier.type != "import_specifier":
                                    continue
                                original = specifier.child_by_field_name("name")
                                alias = specifier.child_by_field_name("alias")
                                bindings[self.text(alias or original)] = self.text(original)
                result.append({"target": target, "bindings": bindings, "node": node})
            elif node.type == "export_statement":
                target = self.value(node.child_by_field_name("source"))
                if isinstance(target, str):
                    result.append({"target": target, "bindings": {}, "node": node})
            elif node.type == "call_expression":
                function = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                if function is None or arguments is None or self.text(function) not in {"require", "import"}:
                    continue
                target = self.value(arguments.named_children[0]) if arguments.named_children else UNKNOWN
                if isinstance(target, str):
                    bindings = {}
                    parent = node.parent
                    if parent is not None and parent.type == "variable_declarator":
                        name = parent.child_by_field_name("name")
                        if name is not None and name.type == "identifier":
                            bindings[self.text(name)] = "default"
                    result.append({"target": target, "bindings": bindings, "node": node})
        return result

    def calls(self):
        for node in self.walk():
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and arguments is not None:
                yield node, function, [child for child in arguments.named_children if child.type != "comment"]


def member(source: Source, node) -> tuple[str, str] | None:
    if node.type != "member_expression":
        return None
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    if obj is None or prop is None:
        return None
    return source.text(obj), source.text(prop)


def lines(node) -> tuple[int, int]:
    return node.start_point.row + 1, node.end_point.row + 1
