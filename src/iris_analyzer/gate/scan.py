"""Bounded, symlink-free inventory of a pinned checkout for the analysis gate.

The gate never installs, builds or executes target code. It lists regular
files under the requested scope (pruning dependency, build-output, test,
example and hidden directories) and reads only the small configuration files
it needs, each with a byte cap.
"""

from __future__ import annotations

import os
import stat
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Contract 1 exclusion list plus a few dependency/cache directories that are
# never deployment sources. Hidden directories are excluded separately.
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".next",
        "coverage",
        "test",
        "tests",
        "__tests__",
        "fixtures",
        "examples",
        "example",
        "docs",
        ".github",
        "venv",
        "__pycache__",
        "bower_components",
    }
)
MAX_ENTRIES = 60_000
MAX_DEPTH = 10
MAX_READ_BYTES = 512 * 1024


def excluded_directory(name: str) -> bool:
    return name.startswith(".") or name in EXCLUDED_DIRECTORIES


def join(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def parent(path: str) -> str:
    value = PurePosixPath(path).parent.as_posix()
    return value if value else "."


def within(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def relative_to(path: str, root: str) -> str:
    if root == ".":
        return path
    if path == root:
        return "."
    return path[len(root) + 1 :]


@dataclass
class RepositoryScan:
    """Regular files (repository-relative POSIX paths) grouped by directory."""

    repo_root: Path
    scope: str
    directories: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False
    oversized: list[str] = field(default_factory=list)
    _cache: dict[str, str | None] = field(default_factory=dict)

    def names(self, directory: str) -> list[str]:
        return self.directories.get(directory, [])

    def exists(self, path: str) -> bool:
        return PurePosixPath(path).name in self.names(parent(path))

    def files(self):
        for directory in sorted(self.directories):
            for name in self.directories[directory]:
                yield join(directory, name)

    def read(self, path: str) -> str | None:
        """Read a listed regular file without following links, capped in size."""
        if path in self._cache:
            return self._cache[path]
        text = None
        if self.exists(path):
            target = self.repo_root.joinpath(*PurePosixPath(path).parts)
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags)
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise OSError("not a regular file")
                    data = handle.read(MAX_READ_BYTES + 1)
                if len(data) > MAX_READ_BYTES:
                    self.oversized.append(path)
                else:
                    text = data.decode("utf-8-sig", errors="replace")
            except OSError:
                text = None
        self._cache[path] = text
        return text


def scan_repository(repo_root: Path, scope: str) -> RepositoryScan:
    """Walk ``scope`` breadth-first without following symlinks."""
    result = RepositoryScan(repo_root=repo_root, scope=scope)
    pending = deque([(scope, 0)])
    entries = 0
    while pending:
        directory, depth = pending.popleft()
        location = repo_root if directory == "." else repo_root.joinpath(*directory.split("/"))
        try:
            iterator = os.scandir(location)
        except OSError:
            continue
        names: list[str] = []
        children: list[str] = []
        with iterator:
            for entry in iterator:
                entries += 1
                if entries > MAX_ENTRIES:
                    result.truncated = True
                    break
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not excluded_directory(entry.name):
                            children.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
        result.directories[directory] = sorted(names)
        if result.truncated:
            break
        if depth >= MAX_DEPTH:
            if children:
                result.truncated = True
            continue
        pending.extend((join(directory, child), depth + 1) for child in sorted(children))
    return result
