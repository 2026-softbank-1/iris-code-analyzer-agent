"""Bounded syntax-only database client use detection; never evaluates source.

Only imported client constructors/connection factories with configuration are
accepted. Unsupported wrappers remain unconfirmed. A use is not a successful
connection and never authorizes provisioning.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from ..preprocess.javascript import Source
from .scan import RepositoryScan, relative_to, within

_CLIENTS = {
    "pg": ("postgres", {"Pool", "Client"}),
    "postgres": ("postgres", {"default"}),
    "mongoose": ("mongodb", {"connect", "createConnection"}),
    "mongodb": ("mongodb", {"MongoClient"}),
    "redis": ("redis", {"createClient", "from_url", "Redis"}),
    "ioredis": ("redis", {"default", "Redis"}),
    "mysql": ("mysql", {"createConnection", "createPool"}),
    "mysql2": ("mysql", {"createConnection", "createPool"}),
    "mysql2/promise": ("mysql", {"createConnection", "createPool"}),
    "better-sqlite3": ("sqlite", {"default"}),
    "sqlite3": ("sqlite", {"Database"}),
    "psycopg": ("postgres", {"connect"}),
    "psycopg2": ("postgres", {"connect"}),
    "asyncpg": ("postgres", {"connect", "create_pool"}),
    "pymongo": ("mongodb", {"MongoClient"}),
    "motor.motor_asyncio": ("mongodb", {"AsyncIOMotorClient"}),
    "redis.asyncio": ("redis", {"from_url", "Redis"}),
    "pymysql": ("mysql", {"connect"}),
    "sqlite3-python": ("sqlite", {"connect"}),
}
_JS_ENV = re.compile(r"\bprocess\.env(?:\.([A-Za-z_]\w*)|\[['\"]([A-Za-z_]\w*)['\"]\])")


@dataclass(frozen=True)
class DatabaseUse:
    engine: str
    path: str
    line: int
    keys: tuple[str, ...]
    # Used internally for host classification; never included in the wire result.
    url: str | None = None
    in_memory: bool = False


def _js(path: str, text: str) -> list[DatabaseUse]:
    source = Source(path, text.encode())
    bindings: dict[str, tuple[str, set[str], str]] = {}
    imported_declarations = set()
    for entry in source.imports():
        spec = _CLIENTS.get(entry["target"])
        if spec:
            for local, original in entry["bindings"].items():
                bindings[local] = (*spec, original)
        # Source.imports intentionally leaves CommonJS destructuring unresolved.
        node = entry["node"]
        if spec and node.parent and node.parent.type == "variable_declarator":
            imported_declarations.add(node.parent.start_byte)
            name = node.parent.child_by_field_name("name")
            if name and name.type == "object_pattern":
                for item in name.named_children:
                    if item.type == "shorthand_property_identifier_pattern":
                        bindings[source.text(item)] = (*spec, source.text(item))
    # Reject bindings shadowed or reassigned anywhere, rather than guessing scope.
    process_shadowed = False
    for node in source.walk():
        targets = []
        if node.type in {"assignment_expression", "augmented_assignment_expression"}:
            targets = [node.child_by_field_name("left")]
        elif node.type in {"formal_parameters", "catch_clause"}:
            targets = list(source.walk(node))
        elif node.type == "variable_declarator" and node.start_byte not in imported_declarations:
            name = node.child_by_field_name("name")
            targets = list(source.walk(name)) if name else []
        elif node.type in {"function_declaration", "class_declaration"}:
            targets = [node.child_by_field_name("name")]
        for target in targets:
            if target and target.type in {"identifier", "shorthand_property_identifier_pattern"}:
                bindings.pop(source.text(target), None)
                process_shadowed |= source.text(target) == "process"
    found = []
    for node in source.walk():
        if node.type not in {"call_expression", "new_expression"}:
            continue
        function = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        args = node.child_by_field_name("arguments")
        if function is None or args is None or not args.named_children:
            continue
        parts = source.text(function).split(".")
        spec = bindings.get(parts[0])
        if spec is None or len(parts) > 2:
            continue
        engine, methods, original = spec
        method = parts[-1] if len(parts) == 2 else original
        if method not in methods:
            continue
        keys = set()
        if not process_shadowed:
            for expression in source.walk(args):
                if expression.type in {"member_expression", "subscript_expression"}:
                    match = _JS_ENV.fullmatch(source.text(expression))
                    if match:
                        keys.add(match[1] or match[2])
        keys = tuple(sorted(keys))
        values = [source.value(child) for child in args.named_children]
        url = next((value for value in values if isinstance(value, str) and "://" in value), None)
        for value in values:
            if isinstance(value, dict):
                url = next(
                    (
                        value[k]
                        for k in ("connectionString", "url", "uri")
                        if isinstance(value.get(k), str) and "://" in value[k]
                    ),
                    url,
                )
        in_memory = engine == "sqlite" and any(value == ":memory:" for value in values)
        found.append(DatabaseUse(engine, path, node.start_point.row + 1, keys, url, in_memory))
    return found


def _python(path: str, text: str) -> list[DatabaseUse]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    bindings: dict[str, tuple[str, set[str], str]] = {}
    os_available = any(
        isinstance(n, ast.Import) and any(a.name == "os" and a.asname in {None, "os"} for a in n.names)
        for n in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = "sqlite3-python" if alias.name == "sqlite3" else alias.name
                if module in _CLIENTS:
                    bindings[alias.asname or alias.name] = (*_CLIENTS[module], "*")
        elif isinstance(node, ast.ImportFrom):
            module = "sqlite3-python" if node.module == "sqlite3" else node.module
            if module in _CLIENTS:
                for alias in node.names:
                    bindings[alias.asname or alias.name] = (*_CLIENTS[module], alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.pop(node.id, None)
            os_available &= node.id != "os"
        elif isinstance(node, ast.arg):
            bindings.pop(node.arg, None)
            os_available &= node.arg != "os"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.pop(node.name, None)
            os_available &= node.name != "os"
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not (node.args or node.keywords):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.value.id
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name))
            else None
        )
        spec = bindings.get(name or "")
        if spec is None:
            continue
        engine, methods, original = spec
        if (func.attr if isinstance(func, ast.Attribute) else original) not in methods:
            continue
        keys = set()
        for child in ast.walk(node):
            if not os_available:
                break
            if isinstance(child, ast.Call) and child.args and isinstance(child.args[0], ast.Constant):
                if ast.unparse(child.func) in {"os.getenv", "os.environ.get"}:
                    keys.add(child.args[0].value)
            elif isinstance(child, ast.Subscript) and ast.unparse(child.value) == "os.environ":
                if isinstance(child.slice, ast.Constant):
                    keys.add(child.slice.value)
        url = next(
            (
                c.value
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and "://" in c.value
            ),
            None,
        )
        in_memory = engine == "sqlite" and any(
            isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value == ":memory:"
            for child in node.args + [kw.value for kw in node.keywords]
        )
        found.append(
            DatabaseUse(
                engine,
                path,
                node.lineno,
                tuple(sorted(k for k in keys if isinstance(k, str))),
                url,
                in_memory,
            )
        )
    return found


def database_uses(scan: RepositoryScan, directory: str, excluded: list[str]) -> list[DatabaseUse]:
    found = []
    checked = 0
    for path in scan.files():
        if not within(path, directory) or any(within(path, root) for root in excluded):
            continue
        if len(relative_to(path, directory).split("/")) > 6:
            continue
        if not path.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".py")):
            continue
        checked += 1
        if checked > 300:
            break
        text = scan.read(path)
        if text:
            found.extend(_python(path, text) if path.endswith(".py") else _js(path, text))
    return found
