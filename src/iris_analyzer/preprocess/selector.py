"""Deterministic deployment-focused file priorities and local references."""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from pathlib import PurePosixPath

import yaml

from iris_analyzer.contracts import AnalyzerError

from .javascript import Source
from .snapshot import SOURCE_EXTENSIONS, Snapshot, file_kind, is_env_example


def load_json(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def component_of(path: str, roots: list[str]) -> str:
    matches = [root for root in roots if root != "." and (path == root or path.startswith(root + "/"))]
    return max(matches, key=len) if matches else "."


class Selection:
    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.packages = {}
        for path, raw in snapshot.files.items():
            if _initial_excluded(path):
                continue
            if PurePosixPath(path).name == "package.json":
                package = load_json(raw)
                if package is None:
                    raise AnalyzerError(
                        "SOURCE_PARSE_INVALID", f"Package manifest is not a JSON object: {path}"
                    )
                for key in ("scripts", "dependencies", "devDependencies", "engines"):
                    if key in package and not isinstance(package[key], dict):
                        raise AnalyzerError(
                            "SOURCE_PARSE_INVALID", f"Package {key} must be an object: {path}"
                        )
                if "workspaces" in package and not isinstance(package["workspaces"], (list, dict)):
                    raise AnalyzerError(
                        "SOURCE_PARSE_INVALID", f"Package workspaces must be an array or object: {path}"
                    )
                self.packages[PurePosixPath(path).parent.as_posix()] = (path, package)
        self.roots = sorted(self.packages) or ["."]
        self.aliases: list[tuple[str, str, str]] = []
        for path, raw in snapshot.files.items():
            if _initial_excluded(path) or not PurePosixPath(path).name.startswith("tsconfig"):
                continue
            # JSONC comments are stripped lexically before the standard parser.
            text = raw.decode("utf-8-sig")
            try:
                config = json.loads(re.sub(r",\s*([}\]])", r"\1", _strip_json_comments(text)))
            except json.JSONDecodeError:
                continue
            if not isinstance(config, dict):
                continue
            options = config.get("compilerOptions", {})
            if not isinstance(options, dict):
                continue
            root = PurePosixPath(path).parent.as_posix()
            base = posixpath.normpath(posixpath.join(root, options.get("baseUrl", ".")))
            for alias, destinations in options.get("paths", {}).items():
                if isinstance(destinations, list):
                    for destination in destinations:
                        if isinstance(destination, str):
                            self.aliases.append((root, alias, posixpath.join(base, destination)))
        for path, raw in snapshot.files.items():
            if _initial_excluded(path) or not PurePosixPath(path).name.startswith("vite.config."):
                continue
            source = Source(path, raw)
            root = PurePosixPath(path).parent.as_posix()
            for node in source.walk():
                if node.type != "pair":
                    continue
                parent = node.parent
                enclosing = parent.parent if parent is not None else None
                key = (
                    enclosing.child_by_field_name("key")
                    if enclosing is not None and enclosing.type == "pair"
                    else None
                )
                if key is None or source.text(key).strip("\"'") != "alias":
                    continue
                alias_key = node.child_by_field_name("key")
                alias = source.text(alias_key).strip("\"'")
                target_node = node.child_by_field_name("value")
                destination = source.value(target_node)
                if not isinstance(destination, str) and target_node is not None:
                    # Vite's documented fileURLToPath(new URL('./src',
                    # import.meta.url)) pattern is inspectable without execution.
                    for descendant in source.walk(target_node):
                        if descendant.type == "new_expression":
                            constructor = descendant.child_by_field_name("constructor")
                            args = descendant.child_by_field_name("arguments")
                            if (
                                constructor is not None
                                and source.text(constructor) == "URL"
                                and args is not None
                                and args.named_children
                            ):
                                candidate = source.value(args.named_children[0])
                                if isinstance(candidate, str) and candidate.startswith("."):
                                    destination = candidate
                if isinstance(destination, str) and not destination.startswith("/"):
                    mapped = posixpath.normpath(posixpath.join(root, destination))
                    self.aliases.extend(
                        [(root, alias, mapped), (root, alias.rstrip("/") + "/*", mapped.rstrip("/") + "/*")]
                    )
        self.sources: dict[str, Source] = {}
        self.selected: dict[str, dict] = {}
        self.unresolved: list[dict] = []

    def source(self, path: str) -> Source:
        if path not in self.sources:
            self.sources[path] = Source(path, self.snapshot.files[path])
        return self.sources[path]

    def resolve(self, origin: str, target: str) -> str | None:
        candidates = []
        if target.startswith("."):
            candidates.append(posixpath.normpath(posixpath.join(posixpath.dirname(origin), target)))
        else:
            for root, alias, destination in self.aliases:
                if root != "." and not origin.startswith(root + "/"):
                    continue
                if "*" in alias:
                    prefix, suffix = alias.split("*", 1)
                    if target.startswith(prefix) and target.endswith(suffix):
                        variable = target[len(prefix) : len(target) - len(suffix) if suffix else None]
                        candidates.append(destination.replace("*", variable))
                elif target == alias:
                    candidates.append(destination)
        for candidate in candidates:
            candidate = posixpath.normpath(candidate)
            if candidate == ".." or candidate.startswith("../") or candidate.startswith("/"):
                continue
            attempts = [candidate]
            suffix = PurePosixPath(candidate).suffix
            if suffix in {".js", ".mjs", ".cjs", ".jsx"}:
                without = candidate[: -len(suffix)]
                attempts.extend(without + replacement for replacement in (".ts", ".tsx", ".mts", ".cts"))
            if not suffix:
                attempts.extend(
                    candidate + extension
                    for extension in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")
                )
                attempts.extend(
                    posixpath.join(candidate, "index" + extension)
                    for extension in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
                )
            for attempt in attempts:
                if attempt in self.snapshot.files:
                    return attempt
        return None

    def add(self, path: str, priority: int, role: str, reason: str) -> None:
        if path not in self.snapshot.files:
            return
        old = self.selected.get(path)
        if old is None or priority < old["priority"]:
            self.selected[path] = {"priority": priority, "role": role, "selectionReason": reason}

    def run(self, requested: list[str] | None = None) -> "Selection":
        for path in sorted(self.snapshot.files):
            if _initial_excluded(path):
                continue
            name = PurePosixPath(path).name.lower()
            kind = file_kind(path)
            if kind in {"manifest", "lockfile"}:
                self.add(path, 1, kind, "Package/workspace and dependency metadata")
            elif kind in {"container", "build_config"}:
                self.add(path, 2, kind, "Container or framework execution configuration")
            elif is_env_example(path):
                self.add(path, 3, "environment", "Environment variable names; all example values masked")
            elif kind == "source" and not _test_source(path):
                # Framework entry points and explicit deployment-focused modules.
                stem = PurePosixPath(path).stem.lower()
                if stem in {"index", "main", "app", "server", "env"} or re.search(
                    r"(^|[./_-])(routes?|router|config|middleware)([./_-]|$)", path.lower()
                ):
                    self.add(path, 3, "entry_or_router", "Entry point, runtime configuration or router")
                elif re.search(
                    r"(^|[./_-])(api|db|database|client|connection|storage)([./_-]|$)", path.lower()
                ):
                    self.add(path, 4, "connection", "Frontend API or database/storage connection")
            elif name.startswith("readme") and PurePosixPath(path).parent.as_posix() in self.roots:
                self.add(path, 5, "documentation", "Package execution documentation")
        for root, (path, package) in self.packages.items():
            for command in package.get("scripts", {}).values():
                if not isinstance(command, str):
                    continue
                for target in re.findall(
                    r"(?:^|\s)([\w./@-]+\.(?:[cm]?js|[cm]?ts|tsx|jsx))(?:\s|$)", command
                ):
                    resolved = self.resolve(path, "./" + target)
                    if resolved and not _test_source(resolved):
                        self.add(resolved, 3, "entry", "File referenced by a package script")
                    elif target.startswith("dist/"):
                        resolved = self.resolve(path, "./src/" + target[5:])
                        if resolved and not _test_source(resolved):
                            self.add(resolved, 3, "entry", "Source corresponding to compiled runtime entry")
        for path in requested or []:
            self.add(path, 3, "requested", "Explicit checked model file request")
        processed = set()
        while True:
            remaining = [path for path in self.selected if path not in processed]
            if not remaining:
                break
            for path in sorted(remaining):
                processed.add(path)
                if PurePosixPath(path).suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                source = self.source(path)
                for reference in source.imports():
                    target = reference["target"]
                    resolved = self.resolve(path, target)
                    if resolved:
                        if file_kind(resolved) in {"source", "manifest", "build_config"}:
                            self.add(resolved, 5, "reference", f"Local module referenced by {path}")
                    elif (
                        target.startswith(".")
                        or any(fnmatch.fnmatchcase(target, alias) for _, alias, _ in self.aliases)
                    ) and PurePosixPath(target).suffix.lower() in SOURCE_EXTENSIONS | {"", ".json"}:
                        self.unresolved.append(
                            {
                                "key": "local_reference",
                                "path": path,
                                "reference": target,
                                "reason": "Local import could not be resolved in the immutable eligible snapshot",
                            }
                        )
        # pnpm workspace roots are useful even without root package workspaces.
        for path, raw in self.snapshot.files.items():
            if not _initial_excluded(path) and PurePosixPath(path).name == "pnpm-workspace.yaml":
                try:
                    data = yaml.safe_load(raw)
                    patterns = data.get("packages", []) if isinstance(data, dict) else []
                    for root in self.packages:
                        if any(
                            isinstance(pattern, str) and fnmatch.fnmatchcase(root, pattern)
                            for pattern in patterns
                        ):
                            self.add(self.packages[root][0], 1, "manifest", "Declared pnpm workspace package")
                except yaml.YAMLError:
                    self.unresolved.append(
                        {"key": "workspace", "path": path, "reason": "Workspace YAML is malformed"}
                    )
        self.unresolved.sort(key=lambda item: (item.get("path", ""), item.get("reference", ""), item["key"]))
        return self

    def ordered(self) -> list[str]:
        return sorted(self.selected, key=lambda path: (self.selected[path]["priority"], path))


def _test_source(path: str) -> bool:
    return _initial_excluded(path) or bool(re.search(r"\.(?:test|spec)\.[^.]+$", path))


def _initial_excluded(path: str) -> bool:
    """Test data stays in inventory but cannot declare deployment components.

    The rule applies to paths relative to the requested root. An explicitly
    analyzed fixture root has ordinary package/config paths. Direct local
    imports and validated requests can still select nested test data as evidence.
    """
    return any(
        part in {"test", "tests", "__tests__", "fixture", "fixtures", "__fixtures__"}
        for part in PurePosixPath(path).parts[:-1]
    )


def _strip_json_comments(text: str) -> str:
    # Preserve strings (including URLs) rather than deleting // indiscriminately.
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: m[0] if m[0].startswith('"') else " " * len(m[0]), text)
